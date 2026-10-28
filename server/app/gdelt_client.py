"""Async client for the GDELT DOC 2.0 API (artlist mode).

Responsibilities:
  * build valid query params
  * throttle (GDELT rate-limits aggressively)
  * retry transient failures
  * tolerate GDELT's occasionally-malformed JSON
  * return normalized Article objects

DOC 2.0 returns at most 250 records per call, so to collect a wider window
we walk it in time slices (startdatetime/enddatetime) newest-first.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .models import Article

log = logging.getLogger("gdelt.client")

_GDELT_DT = "%Y%m%d%H%M%S"
_GDELT_MIN_INTERVAL_S = 5.0
_PERMANENT_RESPONSE_ERRORS = {
    "Your query was too short or too long.": (
        "request rejected by GDELT (query length or syntax)"
    ),
    "Timespan is too short.": "request rejected by GDELT (timespan too short)",
}


class GdeltError(RuntimeError):
    pass


class GdeltTransientError(GdeltError):
    """A transport/provider failure that may succeed on a later attempt."""


class GdeltRequestError(GdeltError):
    """A permanent request validation failure that must not be retried."""


class GdeltClient:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None):
        self.s = settings
        self._client = client
        self._owns_client = client is None
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "GdeltClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.s.gdelt_timeout_s,
                headers={"User-Agent": self.s.gdelt_user_agent},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            interval = _effective_min_interval_s(self.s.gdelt_min_interval_s)
            wait = interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, GdeltTransientError)),
        wait=wait_exponential(multiplier=5, min=5, max=60),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    async def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        await self._throttle()
        resp = await self._client.get(self.s.gdelt_base_url, params=params)
        if resp.status_code == 429:
            raise GdeltTransientError("rate limited (429)")
        if resp.status_code >= 500:
            raise GdeltTransientError(f"server error {resp.status_code}")
        resp.raise_for_status()
        text = resp.text.strip()
        if not text:
            return {"articles": []}
        permanent_error = _PERMANENT_RESPONSE_ERRORS.get(text)
        if permanent_error is not None:
            raise GdeltRequestError(permanent_error)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # GDELT sometimes returns a bare error string or truncated JSON.
            cleaned = _salvage_json(text)
            if cleaned is not None:
                return cleaned
            log.warning("Un-parseable GDELT response (%d chars): %.120s", len(text), text)
            raise GdeltTransientError("invalid JSON from GDELT")

    async def fetch(
        self,
        query: str,
        *,
        timespan: str | None = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        max_records: int | None = None,
        sort: str = "datedesc",
        source_lang: str | None = None,
        source_country: str | None = None,
    ) -> list[Article]:
        """One DOC 2.0 call. Prefer `timespan` OR (`start`,`end`), not both."""
        params: dict[str, Any] = {
            "query": _augment_query(query, source_lang, source_country),
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records or self.s.gdelt_max_records,
            "sort": sort,
        }
        if start and end:
            params["startdatetime"] = start.astimezone(timezone.utc).strftime(_GDELT_DT)
            params["enddatetime"] = end.astimezone(timezone.utc).strftime(_GDELT_DT)
        elif timespan:
            params["timespan"] = timespan

        data = await self._get_json(params)
        items = data.get("articles") or []
        articles = [Article.from_api(it, source_query=query) for it in items if it.get("url")]
        log.info("GDELT query=%r -> %d articles", query, len(articles))
        return articles

    async def fetch_window(
        self,
        query: str,
        *,
        hours: int = 24,
        slice_hours: int = 6,
        max_records: int | None = None,
        source_lang: str | None = None,
        source_country: str | None = None,
    ) -> list[Article]:
        """Collect a wider time window by walking it in slices.

        Each slice is a separate 250-cap call, so a 24h window in 6h slices
        can surface up to ~1000 articles. Overlapping slices are deduplicated
        here by article id and again at the DB layer (upsert on id).
        """
        end = datetime.now(timezone.utc)
        span = timedelta(hours=hours)
        step = timedelta(hours=max(1, slice_hours))
        collected: dict[str, Article] = {}
        cursor = end
        floor = end - span
        total_slices = successful_slices = failed_slices = 0
        last_transient: Exception | None = None
        while cursor > floor:
            s_start = max(cursor - step, floor)
            total_slices += 1
            try:
                batch = await self.fetch(
                    query,
                    timespan=None,
                    start=s_start,
                    end=cursor,
                    max_records=max_records,
                    source_lang=source_lang,
                    source_country=source_country,
                )
            except (httpx.TransportError, GdeltTransientError) as exc:
                failed_slices += 1
                last_transient = exc
                log.warning(
                    "GDELT slice skipped after retries query=%r start=%s end=%s "
                    "error=%s",
                    query,
                    s_start.isoformat(),
                    cursor.isoformat(),
                    type(exc).__name__,
                )
            else:
                successful_slices += 1
                for article in batch:
                    collected[article.id] = article
            cursor = s_start

        if successful_slices == 0 and last_transient is not None:
            raise last_transient
        if failed_slices:
            log.warning(
                "GDELT window partial query=%r succeeded=%d failed=%d total=%d "
                "last_error=%s",
                query,
                successful_slices,
                failed_slices,
                total_slices,
                type(last_transient).__name__,
            )
        log.info(
            "GDELT window query=%r hours=%d slices=%d/%d -> %d unique",
            query,
            hours,
            successful_slices,
            total_slices,
            len(collected),
        )
        return list(collected.values())


def _augment_query(
    query: str,
    lang: "str | list[str] | None",
    country: str | None,
) -> str:
    parts = [query.strip()]
    if lang:
        languages = [lang] if isinstance(lang, str) else list(lang)
        if len(languages) == 1:
            parts.append(f"sourcelang:{languages[0]}")
        elif languages:
            # Space is AND in DOC 2.0, so several languages have to be an
            # explicit OR group or the query would match nothing.
            joined = " OR ".join(f"sourcelang:{item}" for item in languages)
            parts.append(f"({joined})")
    if country:
        parts.append(f"sourcecountry:{country}")
    return " ".join(parts)


def _effective_min_interval_s(configured: float) -> float:
    """Honor GDELT's advertised five-second request floor."""
    return max(_GDELT_MIN_INTERVAL_S, configured)


def _salvage_json(text: str) -> Optional[dict[str, Any]]:
    """Best-effort recovery: trim to the outermost braces and retry once."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
