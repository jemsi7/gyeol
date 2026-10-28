"""Server-verified Sign in with Apple accounts and opaque app sessions."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .app_service import AppClientError
from .apple_identity import (
    AppleIdentityError,
    AppleIdentityProvider,
    TokenCipher,
)
from .config import Settings
from .feed import build_feed_item
from .recommendation import (
    RANKING_VERSION,
    ArticleRecommendationFeature,
    Daypart,
    RecommendationCandidate,
    RecommendationSurface,
    daypart_for_hour,
    hydrate_recommendation_profile,
    rank_recommendations,
)
from .recommendation_cursor import (
    InvalidRecommendationCursor,
    RecommendationCursor,
    decode_recommendation_cursor,
    encode_recommendation_cursor,
    recommendation_session_scope,
)


log = logging.getLogger("gdelt.account")
SEOUL = ZoneInfo("Asia/Seoul")


class AccountRepository(Protocol):
    async def upsert_apple_account_and_create_session(self, **kwargs: Any) -> dict: ...
    async def current_privacy_consent(self, **kwargs: Any) -> dict | None: ...
    async def account_for_session(self, token_hash: bytes) -> dict | None: ...
    async def delete_account_session(self, token_hash: bytes) -> bool: ...
    async def delete_account(self, account_id: UUID) -> bool: ...
    async def upsert_article_feedback(self, **kwargs: Any) -> dict | None: ...
    async def append_account_activity_events(
        self, account_id: UUID, events: list[dict]
    ) -> dict[str, int]: ...
    async def get_account_reading_state(self, account_id: UUID) -> dict: ...
    async def patch_account_reading_state(self, account_id: UUID, **kwargs: Any) -> dict: ...
    async def account_saved_article_maps(self, account_id: UUID) -> list[dict]: ...
    async def record_article_list_impressions(self, **kwargs: Any) -> int: ...
    async def upsert_push_registration(self, **kwargs: Any) -> dict: ...
    async def update_push_preferences(self, **kwargs: Any) -> dict | None: ...
    async def delete_push_registration(self, **kwargs: Any) -> bool: ...
    async def timeline_subscriptions(self, account_id: UUID) -> list[dict]: ...
    async def upsert_timeline_subscription(self, **kwargs: Any) -> dict | None: ...
    async def delete_timeline_subscription(self, **kwargs: Any) -> bool: ...
    async def recommendation_snapshot_at(self) -> datetime: ...
    async def recommendation_signal_rows(self, account_id: UUID, **kwargs: Any) -> list[dict]: ...
    async def recommendation_feedback_rows(self, account_id: UUID, **kwargs: Any) -> list[dict]: ...
    async def recommendation_saved_rows(self, account_id: UUID, **kwargs: Any) -> list[dict]: ...
    async def recommendation_exposure_rows(self, account_id: UUID, **kwargs: Any) -> list[dict]: ...
    async def record_recommendation_exposures(self, account_id: UUID, **kwargs: Any) -> int: ...
    async def recommendation_article_features(self, article_ids: list[str]) -> list[dict]: ...
    async def recommendation_candidates(self, **kwargs: Any) -> list[dict]: ...
    async def recommendation_feed_rows(self, article_ids: list[str]) -> list[dict]: ...
    async def has_current_account_consent(
        self, account_id: UUID, policy_version: str
    ) -> bool: ...
    async def link_account_privacy_consent(
        self, account_id: UUID, consent_id: UUID, policy_version: str
    ) -> dict | None: ...


class AccountService:
    ARTICLE_FEEDBACK_REASONS = frozenset(
        {
            "not_relevant",
            "too_simple",
            "too_difficult",
            "seen_similar",
            "distrust_source",
            "other",
        }
    )

    def __init__(
        self,
        settings: Settings,
        repository: AccountRepository,
        provider: AppleIdentityProvider | None,
        cipher: TokenCipher | None,
    ):
        self.settings = settings
        self.repository = repository
        self.provider = provider
        self.cipher = cipher

    async def sign_in(
        self,
        *,
        identity_token: str,
        authorization_code: str,
        nonce: str,
        given_name: str | None,
        family_name: str | None,
        privacy_consent_id: UUID,
    ) -> dict:
        provider, cipher = self._configured_dependencies()
        consent = await self.repository.current_privacy_consent(
            consent_id=privacy_consent_id,
            policy_version=self.settings.privacy_policy_version,
        )
        if consent is None:
            raise AppClientError(422, "privacy_consent_required")
        try:
            presented = await provider.verify_identity_token(identity_token, nonce)
            tokens = await provider.exchange_code(authorization_code)
            exchanged = await provider.verify_identity_token(tokens.identity_token, nonce)
        except AppleIdentityError as exc:
            status = 503 if exc.unavailable else 401
            raise AppClientError(status, exc.code) from exc
        if not secrets.compare_digest(presented.subject, exchanged.subject):
            raise AppClientError(401, "apple_identity_subject_mismatch")

        email = presented.email if presented.email_verified else None
        encrypted_refresh = cipher.encrypt(tokens.refresh_token)
        session_plaintext = secrets.token_bytes(32)
        session_token = _base64url_encode(session_plaintext)
        try:
            row = await self.repository.upsert_apple_account_and_create_session(
                apple_subject=presented.subject,
                email=email,
                given_name=_optional_name(given_name),
                family_name=_optional_name(family_name),
                encrypted_refresh_token=encrypted_refresh,
                token_hash=hashlib.sha256(session_plaintext).digest(),
                ttl_days=self.settings.account_session_ttl_days,
                privacy_consent_id=privacy_consent_id,
                privacy_policy_version=self.settings.privacy_policy_version,
            )
        except LookupError as exc:
            raise AppClientError(422, "privacy_consent_required") from exc
        return {
            "session_token": session_token,
            "expires_at": row["session_expires_at"],
            "account": _profile(row),
        }

    async def me(self, bearer_token: str) -> dict:
        _, row = await self._authenticated_row(bearer_token)
        return _profile(row)

    async def logout(self, bearer_token: str) -> None:
        token_hash, _ = await self._authenticated_row(bearer_token)
        deleted = await self.repository.delete_account_session(token_hash)
        if not deleted:
            raise AppClientError(401, "invalid_account_session")

    async def delete(self, bearer_token: str) -> dict:
        _, row = await self._authenticated_row(bearer_token)
        provider, cipher = self._configured_dependencies()
        revoked = False
        try:
            refresh_token = cipher.decrypt(row["apple_refresh_token"])
            revoked = await provider.revoke(refresh_token)
        except AppleIdentityError:
            revoked = False
        deleted = await self.repository.delete_account(UUID(str(row["id"])))
        if not deleted:
            raise AppClientError(500, "account_deletion_failed")
        return {"deleted": True, "apple_token_revoked": revoked}

    async def submit_article_feedback(
        self,
        *,
        bearer_token: str,
        article_id: str,
        reason: str,
    ) -> dict:
        if not article_id or len(article_id) > 128:
            raise AppClientError(422, "invalid_article_feedback")
        if reason not in self.ARTICLE_FEEDBACK_REASONS:
            raise AppClientError(422, "invalid_article_feedback")
        _, account = await self._authenticated_row(bearer_token)
        row = await self.repository.upsert_article_feedback(
            account_id=UUID(str(account["id"])),
            article_id=article_id,
            reason=reason,
        )
        if row is None:
            raise AppClientError(404, "article_not_found")
        return {
            "article_id": row["article_id"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def submit_activity_events(
        self,
        *,
        bearer_token: str,
        events: list[Mapping[str, Any]],
    ) -> dict:
        _, account = await self._authenticated_row(bearer_token)
        try:
            result = await self.repository.append_account_activity_events(
                UUID(str(account["id"])),
                [dict(event) for event in events],
            )
        except ValueError as exc:
            raise AppClientError(422, "invalid_account_activity") from exc
        except Exception as exc:
            raise AppClientError(503, "account_activity_unavailable") from exc
        accepted = int(result.get("accepted", -1))
        duplicates = int(result.get("duplicates", -1))
        if accepted < 0 or duplicates < 0 or accepted + duplicates != len(events):
            raise AppClientError(503, "account_activity_unavailable")
        return {"accepted": accepted, "duplicates": duplicates}

    async def recommendations(
        self,
        *,
        bearer_token: str,
        request: Mapping[str, Any],
        request_base_url: str,
    ) -> dict:
        started = monotonic()
        surface_value = str(
            getattr(request.get("surface"), "value", request.get("surface", ""))
        )
        try:
            async with asyncio.timeout(1.2):
                response = await self._recommendations(
                    bearer_token=bearer_token,
                    request=request,
                    request_base_url=request_base_url,
                )
        except TimeoutError as exc:
            log.warning(
                "account recommendation unavailable surface=%s reason=timeout",
                surface_value or "invalid",
            )
            raise AppClientError(503, "recommendation_unavailable") from exc
        except AppClientError:
            raise
        except Exception as exc:
            log.warning(
                "account recommendation unavailable surface=%s reason=repository",
                surface_value or "invalid",
            )
            raise AppClientError(503, "recommendation_unavailable") from exc
        log.info(
            "account recommendation served surface=%s strategy=%s latency_ms=%d "
            "pool=%d selected=%d fallback=false",
            response["surface"],
            response["strategy"],
            round((monotonic() - started) * 1000),
            response.pop("_candidate_pool_size"),
            len(response["items"]),
        )
        return response

    async def _recommendations(
        self,
        *,
        bearer_token: str,
        request: Mapping[str, Any],
        request_base_url: str,
    ) -> dict:
        token_hash, account = await self._authenticated_row(bearer_token)
        account_id = UUID(str(account["id"]))
        if not await self.repository.has_current_account_consent(
            account_id, self.settings.privacy_policy_version
        ):
            raise AppClientError(409, "privacy_consent_required")

        try:
            surface = RecommendationSurface(
                getattr(request["surface"], "value", request["surface"])
            )
            cursor = decode_recommendation_cursor(request.get("cursor"))
        except (KeyError, ValueError, InvalidRecommendationCursor) as exc:
            raise AppClientError(400, "invalid_recommendation_cursor") from exc

        context_article_id = request.get("context_article_id")
        if cursor is None:
            snapshot_at = await self.repository.recommendation_snapshot_at()
            current_daypart = _request_daypart(request["time_context"])
        else:
            snapshot_at = cursor.snapshot_at
            current_daypart = cursor.daypart
            expected_scope = recommendation_session_scope(
                token_hash,
                snapshot_at=snapshot_at,
                ranking_version=RANKING_VERSION,
            )
            if (
                cursor.surface is not surface
                or cursor.ranking_version != RANKING_VERSION
                or cursor.session_scope != expected_scope
                or cursor.context_article_id != context_article_id
            ):
                raise AppClientError(400, "invalid_recommendation_cursor")
            database_now = await self.repository.recommendation_snapshot_at()
            if snapshot_at > database_now + timedelta(seconds=5):
                raise AppClientError(400, "invalid_recommendation_cursor")

        signal_rows, feedback_rows, saved_rows, exposure_rows = await asyncio.gather(
            self.repository.recommendation_signal_rows(
                account_id,
                snapshot_at=snapshot_at,
                since=snapshot_at - timedelta(days=180),
                limit=500,
            ),
            self.repository.recommendation_feedback_rows(account_id, limit=200),
            self.repository.recommendation_saved_rows(account_id, limit=100),
            self.repository.recommendation_exposure_rows(
                account_id,
                snapshot_at=snapshot_at,
                continuation=cursor is not None,
                limit=500,
            ),
        )
        source_article_ids = [
            str(row["article_id"])
            for row in [*signal_rows, *feedback_rows, *saved_rows]
            if row.get("article_id")
        ]
        if context_article_id:
            source_article_ids.append(str(context_article_id))
        feature_rows = await self.repository.recommendation_article_features(
            source_article_ids
        )
        features = {
            feature.article_id: feature
            for row in feature_rows
            if (feature := _article_feature_from_row(row)) is not None
        }
        context_feature = (
            features.get(str(context_article_id)) if context_article_id else None
        )
        if (
            surface is RecommendationSurface.ARTICLE_BOUNDARY
            and context_feature is None
        ):
            raise AppClientError(404, "article_not_found")

        profile = hydrate_recommendation_profile(
            events=signal_rows,
            feedback=feedback_rows,
            article_features=features,
            snapshot_at=snapshot_at,
            current_daypart=current_daypart,
            saved=saved_rows,
        )
        request_excludes = frozenset(
            str(value) for value in request.get("exclude_article_ids", [])
        )
        latest_initial_request_id = (
            exposure_rows[0].get("request_id")
            if cursor is None and exposure_rows
            else None
        )
        exposure_excludes = frozenset(
            str(row["article_id"])
            for row in exposure_rows
            if row.get("article_id")
            and (
                cursor is not None
                or row.get("request_id") != latest_initial_request_id
                or int(row.get("position", 0)) >= 2
            )
        )
        if request_excludes or exposure_excludes:
            profile = replace(
                profile,
                excluded_article_ids=(
                    profile.excluded_article_ids
                    | request_excludes
                    | exposure_excludes
                ),
            )
        candidate_rows = await self.repository.recommendation_candidates(
            profile_terms=list(profile.global_terms)[:128],
            short_terms=list(profile.short_terms)[:128],
            saved_terms=list(profile.saved_terms)[:128],
            context_terms=(
                list(context_feature.terms)[:64] if context_feature else []
            ),
            snapshot_at=snapshot_at,
            editorial_day=snapshot_at.astimezone(SEOUL).date(),
        )
        candidates = [
            RecommendationCandidate(
                feature,
                frozenset(str(value) for value in row.get("candidate_sources", [])),
            )
            for row in candidate_rows
            if (feature := _article_feature_from_row(row)) is not None
        ]
        page = rank_recommendations(
            profile=profile,
            candidates=candidates,
            surface=surface,
            context=context_feature,
            generated_at=snapshot_at,
            limit=int(request["limit"]),
            after=cursor.sort_key if cursor else None,
            rank_seed=hashlib.sha256(
                token_hash + snapshot_at.isoformat().encode("ascii")
            ).digest(),
        )
        selected_ids = [item.candidate.article_id for item in page.items]
        feed_rows = await self.repository.recommendation_feed_rows(selected_ids)
        try:
            items = [
                item
                for row in feed_rows
                if (
                    item := build_feed_item(
                        row,
                        settings=self.settings,
                        request_base_url=request_base_url,
                    )
                )
                is not None
            ]
        except ValueError as exc:
            raise AppClientError(503, "invalid_public_base_url") from exc

        candidate_by_id = {
            candidate.article_id: candidate for candidate in candidates
        }
        page_kind = (
            "boundary"
            if surface is RecommendationSurface.ARTICLE_BOUNDARY
            else ("pagination" if cursor is not None else "initial")
        )
        recorded_exposures = await self.repository.record_recommendation_exposures(
            account_id,
            request_id=uuid4(),
            ranking_version=RANKING_VERSION,
            surface=surface.value,
            page_kind=page_kind,
            served_at=snapshot_at,
            items=[
                {
                    "article_id": str(item["article_id"]),
                    "position": position,
                    "lane": _candidate_lane(
                        candidate_by_id.get(str(item["article_id"]))
                    ),
                }
                for position, item in enumerate(items)
            ],
        )
        if recorded_exposures != len(items):
            raise RuntimeError("recommendation exposure count mismatch")

        next_cursor = None
        if (
            surface is RecommendationSurface.HOME
            and page.next_sort_key is not None
        ):
            next_cursor = encode_recommendation_cursor(
                RecommendationCursor(
                    surface=surface,
                    snapshot_at=snapshot_at,
                    session_scope=recommendation_session_scope(
                        token_hash,
                        snapshot_at=snapshot_at,
                        ranking_version=RANKING_VERSION,
                    ),
                    last_score_micros=page.next_sort_key.score_micros,
                    last_published_at=page.next_sort_key.published_at,
                    last_article_id=page.next_sort_key.article_id,
                    daypart=current_daypart,
                    context_article_id=(
                        str(context_article_id) if context_article_id else None
                    ),
                )
            )
        return {
            "format_version": 1,
            "surface": surface.value,
            "strategy": page.strategy,
            "ranking_version": RANKING_VERSION,
            "generated_at": snapshot_at,
            "items": items,
            "next_cursor": next_cursor,
            "_candidate_pool_size": len(candidates),
        }

    async def link_privacy_consent(
        self, *, bearer_token: str, consent_id: UUID
    ) -> dict:
        _, account = await self._authenticated_row(bearer_token)
        row = await self.repository.link_account_privacy_consent(
            UUID(str(account["id"])),
            consent_id,
            self.settings.privacy_policy_version,
        )
        if row is None:
            raise AppClientError(422, "privacy_consent_required")
        return dict(row)

    async def reading_state(
        self, *, bearer_token: str, request_base_url: str
    ) -> dict:
        _, account = await self._authenticated_row(bearer_token)
        account_id = UUID(str(account["id"]))
        state = await self.repository.get_account_reading_state(account_id)
        return await self._reading_state_response(
            account_id=account_id,
            state=state,
            request_base_url=request_base_url,
        )

    async def patch_reading_state(
        self,
        *,
        bearer_token: str,
        patch: Mapping[str, Any],
        request_base_url: str,
    ) -> dict:
        _, account = await self._authenticated_row(bearer_token)
        account_id = UUID(str(account["id"]))
        try:
            state = await self.repository.patch_account_reading_state(
                account_id,
                saved_changes=[dict(value) for value in patch.get("saved_changes", [])],
                progress_changes=[
                    dict(value) for value in patch.get("progress_changes", [])
                ],
                settings=(
                    dict(patch["settings"])
                    if patch.get("settings") is not None
                    else None
                ),
                update_last_article=bool(patch.get("update_last_article", False)),
                last_article_id=patch.get("last_article_id"),
            )
        except LookupError as exc:
            raise AppClientError(401, "invalid_account_session") from exc
        except ValueError as exc:
            raise AppClientError(422, "invalid_reading_state") from exc
        return await self._reading_state_response(
            account_id=account_id,
            state=state,
            request_base_url=request_base_url,
        )

    async def _reading_state_response(
        self, *, account_id: UUID, state: Mapping[str, Any], request_base_url: str
    ) -> dict:
        rows = await self.repository.account_saved_article_maps(account_id)
        saved_articles: list[dict] = []
        try:
            for row in rows:
                article = build_feed_item(
                    row,
                    settings=self.settings,
                    request_base_url=request_base_url,
                )
                if article is not None:
                    saved_articles.append(
                        {"saved_at": row["saved_at"], "article": article}
                    )
        except ValueError as exc:
            raise AppClientError(503, "invalid_public_base_url") from exc
        saved_articles = saved_articles[:100]
        try:
            recorded_exposures = await self.repository.record_article_list_impressions(
                article_ids=[
                    str(saved["article"]["article_id"])
                    for saved in saved_articles
                ],
                served_at=datetime.now(UTC),
                account_id=account_id,
                surface="library",
                response_id=uuid4(),
            )
        except Exception as exc:
            log.warning(
                "account reading-state impression unavailable reason=%s",
                type(exc).__name__,
            )
            raise AppClientError(503, "reading_state_unavailable") from exc
        if recorded_exposures != len(saved_articles):
            raise AppClientError(503, "reading_state_unavailable")
        return {
            "revision": int(state["revision"]),
            "saved_articles": saved_articles,
            "scene_progress": dict(state.get("scene_progress") or {}),
            "last_article_id": state.get("last_article_id"),
            "settings": dict(state["settings"]),
        }

    async def register_push(
        self,
        *,
        bearer_token: str,
        installation_id: str,
        key_id: str,
        device_token: bytes,
        environment: str,
        preferences: Mapping[str, bool] | None = None,
    ) -> dict:
        _, row = await self._authenticated_row(bearer_token)
        _, cipher = self._configured_dependencies()
        if not device_token or len(device_token) > 256:
            raise AppClientError(422, "invalid_push_device_token")
        try:
            normalized_installation = str(UUID(installation_id))
        except ValueError as exc:
            raise AppClientError(422, "invalid_push_installation") from exc
        encrypted = cipher.encrypt(_base64url_encode(device_token))
        normalized_preferences = preferences or {
            "editorial": True,
            "weekly_insight": False,
            "timeline_updates": False,
        }
        try:
            registration = await self.repository.upsert_push_registration(
                account_id=UUID(str(row["id"])),
                session_id=UUID(str(row["session_id"])),
                installation_id=normalized_installation,
                key_id=key_id,
                token_hash=hashlib.sha256(device_token).digest(),
                token_ciphertext=encrypted,
                environment=environment,
                editorial_enabled=bool(normalized_preferences.get("editorial", True)),
                weekly_insight_enabled=bool(
                    normalized_preferences.get("weekly_insight", False)
                ),
                timeline_updates_enabled=bool(
                    normalized_preferences.get("timeline_updates", False)
                ),
                timeline_refreshes_enabled=bool(
                    normalized_preferences.get(
                        "timeline_refreshes",
                        normalized_preferences.get("timeline_updates", False),
                    )
                ),
                timeline_additions_enabled=bool(
                    normalized_preferences.get(
                        "timeline_additions",
                        normalized_preferences.get("timeline_updates", False),
                    )
                ),
            )
        except LookupError as exc:
            raise AppClientError(401, "attested_installation_not_active") from exc
        except ValueError as exc:
            raise AppClientError(409, "push_token_conflict") from exc
        return {
            "active": True,
            "installation_id": registration["installation_id"],
            "environment": registration["environment"],
            "preferences": {
                "editorial": bool(registration.get("editorial_enabled", True)),
                "weekly_insight": bool(
                    registration.get("weekly_insight_enabled", False)
                ),
                "timeline_updates": bool(
                    registration.get("timeline_refreshes_enabled", False)
                    or registration.get("timeline_additions_enabled", False)
                ),
                "timeline_refreshes": bool(
                    registration.get("timeline_refreshes_enabled", False)
                ),
                "timeline_additions": bool(
                    registration.get("timeline_additions_enabled", False)
                ),
            },
        }

    async def update_push_preferences(
        self,
        *,
        bearer_token: str,
        installation_id: UUID,
        preferences: Mapping[str, bool],
    ) -> dict:
        if not any(
            preferences.get(key)
            for key in (
                "editorial", "weekly_insight", "timeline_updates",
                "timeline_refreshes", "timeline_additions",
            )
        ):
            raise AppClientError(422, "push_preference_required")
        _, row = await self._authenticated_row(bearer_token)
        registration = await self.repository.update_push_preferences(
            account_id=UUID(str(row["id"])),
            installation_id=str(installation_id),
            editorial_enabled=bool(preferences.get("editorial")),
            weekly_insight_enabled=bool(preferences.get("weekly_insight")),
            timeline_updates_enabled=bool(preferences.get("timeline_updates")),
            timeline_refreshes_enabled=bool(
                preferences.get("timeline_refreshes", preferences.get("timeline_updates"))
            ),
            timeline_additions_enabled=bool(
                preferences.get("timeline_additions", preferences.get("timeline_updates"))
            ),
        )
        if registration is None:
            raise AppClientError(404, "push_registration_not_found")
        return {
            "active": True,
            "installation_id": registration["installation_id"],
            "environment": registration["environment"],
            "preferences": {
                "editorial": bool(registration["editorial_enabled"]),
                "weekly_insight": bool(registration["weekly_insight_enabled"]),
                "timeline_updates": bool(
                    registration.get(
                        "timeline_refreshes_enabled",
                        registration.get("timeline_updates_enabled", False),
                    )
                    or registration.get(
                        "timeline_additions_enabled",
                        registration.get("timeline_updates_enabled", False),
                    )
                ),
                "timeline_refreshes": bool(
                    registration.get(
                        "timeline_refreshes_enabled",
                        registration.get("timeline_updates_enabled", False),
                    )
                ),
                "timeline_additions": bool(
                    registration.get(
                        "timeline_additions_enabled",
                        registration.get("timeline_updates_enabled", False),
                    )
                ),
            },
        }

    async def unregister_push(
        self, *, bearer_token: str, installation_id: UUID
    ) -> None:
        _, row = await self._authenticated_row(bearer_token)
        await self.repository.delete_push_registration(
            account_id=UUID(str(row["id"])),
            installation_id=str(installation_id),
        )

    async def list_timeline_subscriptions(self, *, bearer_token: str) -> dict:
        _, row = await self._authenticated_row(bearer_token)
        try:
            subscriptions = await self.repository.timeline_subscriptions(
                UUID(str(row["id"]))
            )
        except Exception as exc:
            log.warning(
                "timeline subscription list unavailable reason=%s",
                type(exc).__name__,
            )
            raise AppClientError(503, "timeline_subscription_unavailable") from exc
        return {
            "stable_keys": [
                str(subscription["timeline_stable_key"])
                for subscription in subscriptions[:64]
            ],
            "subscriptions": [
                {
                    "stable_key": str(subscription["timeline_stable_key"]),
                    "title": subscription.get("title"),
                    "summary": subscription.get("summary"),
                    "started_at": subscription.get("started_at"),
                    "ended_at": subscription.get("ended_at"),
                    "subscribed_at": subscription["created_at"],
                }
                for subscription in subscriptions[:64]
            ],
        }

    async def timeline_subscription_history(self, *, bearer_token: str) -> dict:
        _, row = await self._authenticated_row(bearer_token)
        try:
            events = await self.repository.timeline_subscription_history(
                UUID(str(row["id"])), limit=100
            )
        except Exception as exc:
            log.warning(
                "timeline subscription history unavailable reason=%s",
                type(exc).__name__,
            )
            raise AppClientError(503, "timeline_subscription_unavailable") from exc
        return {
            "events": [
                {
                    "id": event["id"],
                    "stable_key": str(event["timeline_stable_key"]),
                    "event_type": event["event_type"],
                    "occurred_at": event["occurred_at"],
                    "is_currently_subscribed": bool(event["is_currently_subscribed"]),
                    "title": event.get("title"),
                }
                for event in events
            ]
        }

    async def subscribe_timeline(
        self, *, bearer_token: str, stable_key: str
    ) -> dict:
        safe_key = _timeline_stable_key(stable_key)
        _, row = await self._authenticated_row(bearer_token)
        try:
            subscription = await self.repository.upsert_timeline_subscription(
                account_id=UUID(str(row["id"])),
                stable_key=safe_key,
                stale_hours=72,
                max_subscriptions=64,
            )
        except OverflowError as exc:
            raise AppClientError(
                409, "timeline_subscription_limit_reached"
            ) from exc
        except Exception as exc:
            log.warning(
                "timeline subscription unavailable reason=%s",
                type(exc).__name__,
            )
            raise AppClientError(503, "timeline_subscription_unavailable") from exc
        if subscription is None:
            raise AppClientError(404, "timeline_not_found")
        return {
            "stable_key": str(subscription["timeline_stable_key"]),
            "subscribed": True,
            "subscribed_at": subscription["created_at"],
        }

    async def unsubscribe_timeline(
        self, *, bearer_token: str, stable_key: str
    ) -> None:
        safe_key = _timeline_stable_key(stable_key)
        _, row = await self._authenticated_row(bearer_token)
        try:
            await self.repository.delete_timeline_subscription(
                account_id=UUID(str(row["id"])),
                stable_key=safe_key,
            )
        except Exception as exc:
            log.warning(
                "timeline unsubscribe unavailable reason=%s",
                type(exc).__name__,
            )
            raise AppClientError(503, "timeline_subscription_unavailable") from exc

    async def _authenticated_row(self, bearer_token: str) -> tuple[bytes, Mapping[str, Any]]:
        self._configured_dependencies()
        token_hash = _session_token_hash(bearer_token)
        row = await self.repository.account_for_session(token_hash)
        if row is None:
            raise AppClientError(401, "invalid_account_session")
        return token_hash, row

    async def app_attest_recovery_identity(
        self, bearer_token: str
    ) -> tuple[UUID, UUID]:
        """Return the exact live account/session pair for App Attest recovery."""
        _, row = await self._authenticated_row(bearer_token)
        return UUID(str(row["id"])), UUID(str(row["session_id"]))

    def _configured_dependencies(self) -> tuple[AppleIdentityProvider, TokenCipher]:
        try:
            self.settings.validate_apple_sign_in()
        except ValueError as exc:
            raise AppClientError(503, "apple_sign_in_not_configured") from exc
        if self.provider is None or self.cipher is None:
            raise AppClientError(503, "apple_sign_in_not_configured")
        return self.provider, self.cipher


def extract_bearer(authorization: str | None) -> str:
    if authorization is None or len(authorization) > 256:
        raise AppClientError(401, "invalid_account_session")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise AppClientError(401, "invalid_account_session")
    return token


def _timeline_stable_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or "/" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AppClientError(422, "invalid_timeline_stable_key")
    return value


def _session_token_hash(token: str) -> bytes:
    if len(token) < 32 or len(token) > 128:
        raise AppClientError(401, "invalid_account_session")
    try:
        raw = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise AppClientError(401, "invalid_account_session") from exc
    if len(raw) != 32:
        raise AppClientError(401, "invalid_account_session")
    return hashlib.sha256(raw).digest()


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _optional_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _profile(row: Mapping[str, Any]) -> dict:
    parts = [
        value.strip()
        for value in (row.get("given_name"), row.get("family_name"))
        if isinstance(value, str) and value.strip()
    ]
    return {
        "id": row["id"],
        "display_name": " ".join(parts) or None,
        "email": row.get("email"),
    }


def _request_daypart(value: Mapping[str, Any]) -> Daypart:
    occurred_at = value.get("occurred_at")
    offset = value.get("utc_offset_minutes")
    if (
        not isinstance(occurred_at, datetime)
        or occurred_at.tzinfo is None
        or occurred_at.utcoffset() is None
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not -720 <= offset <= 840
    ):
        raise ValueError("invalid recommendation time context")
    local = occurred_at.astimezone(UTC) + timedelta(minutes=offset)
    return daypart_for_hour(local.hour)


def _article_feature_from_row(
    row: Mapping[str, Any],
) -> ArticleRecommendationFeature | None:
    try:
        terms = tuple(str(value) for value in row.get("terms", []))
        weights = row.get("term_weights")
        if not isinstance(weights, Mapping):
            return None
        return ArticleRecommendationFeature(
            article_id=str(row["article_id"]),
            terms=terms,
            term_weights={str(key): float(value) for key, value in weights.items()},
            representative_topic=(
                str(row["representative_topic"])
                if row.get("representative_topic")
                else None
            ),
            domain=str(row["domain"]) if row.get("domain") else None,
            published_at=(
                row["published_at"]
                if isinstance(row.get("published_at"), datetime)
                else None
            ),
            quality_score=float(row["quality_score"]),
            headline=str(row.get("headline") or ""),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _candidate_lane(candidate: RecommendationCandidate | None) -> str:
    if candidate is None:
        return "now"
    for lane in ("core", "adjacent", "surprise", "now"):
        if lane in candidate.sources:
            return lane
    return "now"
