"""The crypto features are actually reachable at runtime (T-35).

Every module below had full unit coverage while being called from nowhere, so
these tests assert the wiring itself: that the app builds the collaborators,
that a reader response passes through the annotation step, and that the whole
annotation path degrades to a plain article instead of a failed response.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import api
from app.app_api_models import FeedItem


IOS_ROOT = Path(__file__).resolve().parents[2] / "ios"
PROJECTION = IOS_ROOT / "TextShortsApp/TextShortsApp/Logic/ArticleProjection.swift"
HOME = IOS_ROOT / "TextShortsApp/TextShortsApp/Screens/HomeScreen.swift"
MODELS = IOS_ROOT / "TextShortsApp/TextShortsApp/Model/PrototypeModels.swift"


class _Facade:
    def __init__(self, values=None, error=None):
        self.values = values or {}
        self.error = error
        self.calls = 0

    async def current(self, subjects):
        self.calls += 1
        if self.error:
            raise self.error
        return self.values


class _Repo:
    def __init__(self, *, chains=None, error=None):
        self.chains = chains or {}
        self.error = error

    async def chain_ids_for_articles(self, article_ids):
        if self.error:
            raise self.error
        return self.chains

    async def chain_articles(self, chain_id):
        return []

    async def chain_join_times(self, article_ids):
        return {}


def _item(article_id="a1"):
    return {
        "article_id": article_id,
        "map": {"tagged_atoms": [], "scenes": [{"id": "S-01", "claim_refs": []}]},
    }


@pytest.fixture
def wired(monkeypatch):
    facade = _Facade()
    repo = _Repo()
    monkeypatch.setattr(api.state, "market_data", facade, raising=False)
    monkeypatch.setattr(api.state, "repo", repo, raising=False)
    return facade, repo


# --- the app builds the collaborators ----------------------------------------

def test_app_state_declares_both_collaborators():
    annotations = api.AppState.__annotations__
    assert "market_data" in annotations
    assert "event_chain_worker" in annotations


def test_startup_builds_and_starts_them():
    source = inspect.getsource(api)
    assert "state.market_data = MarketDataFacade(" in source
    assert "state.event_chain_worker = EventChainWorker(" in source
    assert "state.event_chain_worker.start()" in source
    # A worker that is started must also be stopped, or shutdown hangs.
    assert "await state.event_chain_worker.stop()" in source


def test_the_symbol_directory_comes_from_settings():
    from app.config import Settings

    assert Settings(_env_file=None).market_data_ids_file == "market_data_ids.yaml"
    assert "SymbolDirectory.load(settings.market_data_ids_file)" in inspect.getsource(api)


# --- reader responses pass through the annotation step -----------------------

def test_every_feed_response_is_annotated():
    source = inspect.getsource(api)
    # There are three feed assembly paths (random, latest, cursor); a new one
    # that forgets this call would ship crypto articles with no annotations.
    assert source.count("items = await _annotate_crypto_items(items)") == 3
    assert source.count("response = FeedResponse(") == 3


@pytest.mark.asyncio
async def test_annotation_returns_the_items_it_was_given(wired):
    items = [_item()]
    assert await api._annotate_crypto_items(items) is items


@pytest.mark.asyncio
async def test_no_items_asks_nobody(wired):
    facade, _repo = wired
    assert await api._annotate_crypto_items([]) == []
    assert facade.calls == 0


@pytest.mark.asyncio
async def test_no_tagged_subjects_skips_the_market_lookup(wired):
    facade, _repo = wired
    await api._annotate_crypto_items([_item()])
    # Core articles carry no tagged atoms, so the read path stays untouched.
    assert facade.calls == 0


@pytest.mark.asyncio
async def test_a_market_failure_still_returns_the_article(monkeypatch):
    monkeypatch.setattr(
        api.state, "market_data", _Facade(error=RuntimeError("down")), raising=False
    )
    monkeypatch.setattr(api.state, "repo", _Repo(), raising=False)
    items = [
        {
            "article_id": "a1",
            "map": {
                "tagged_atoms": [
                    {
                        "claim_id": "CL-001",
                        "atom": "480억",
                        "tag": "tvl",
                        "subject_kind": "chain",
                        "subject_id": "ethereum",
                        "as_of": "2026-08-01T00:00:00+00:00",
                    }
                ],
                "scenes": [{"id": "S-01", "claim_refs": ["CL-001"]}],
            },
        }
    ]

    result = await api._annotate_crypto_items(items)

    assert result[0]["article_id"] == "a1"
    assert result[0]["map"]["scenes"][0]["staleness_note"] == ""


@pytest.mark.asyncio
async def test_a_chain_failure_still_returns_the_article(monkeypatch):
    monkeypatch.setattr(api.state, "market_data", _Facade(), raising=False)
    monkeypatch.setattr(
        api.state, "repo", _Repo(error=RuntimeError("db down")), raising=False
    )
    items = [_item()]

    result = await api._annotate_crypto_items(items)

    assert result[0]["article_id"] == "a1"


@pytest.mark.asyncio
async def test_an_absent_facade_is_not_an_error(monkeypatch):
    monkeypatch.setattr(api.state, "market_data", None, raising=False)
    monkeypatch.setattr(api.state, "repo", _Repo(), raising=False)
    assert await api._annotate_crypto_items([_item()]) is not None


# --- the app renders what the server sends -----------------------------------

def test_ios_carries_the_notes_into_both_projections():
    swift = PROJECTION.read_text(encoding="utf-8")
    # v2 and v3 article shapes both reach the reader.
    assert swift.count("revisionNote: item.revisionNote") == 2
    assert swift.count("chainProgressNote: item.chainProgressNote") == 2


def test_the_entry_note_prefers_chain_progress():
    swift = MODELS.read_text(encoding="utf-8")
    assert "var entryNote: String" in swift
    # chain progress already resolves to the correction when there is one.
    assert "chainProgressNote.isEmpty ? revisionNote : chainProgressNote" in swift


def test_ios_renders_the_entry_note_without_a_new_component():
    swift = HOME.read_text(encoding="utf-8")
    assert "if !article.entryNote.isEmpty {" in swift
    block = swift.split("if !article.entryNote.isEmpty {")[1][:600]
    # Comments may discuss what this is not; only the view code is checked.
    code = "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("//")
    )
    assert ".font(.system(size: 10))" in code
    for forbidden in ("sheet", "Button", "alert", "Badge"):
        assert forbidden not in code


def test_feed_item_model_forbids_the_deferred_chain_fields():
    """F-4 fields are not part of the client contract in this release.

    Attaching them to a feed item does not merely leak a deferred feature --
    FeedItem forbids extras, so the whole FeedResponse fails validation and the
    reader gets a 500. This is why the annotations are gated off by default.
    """
    from app.app_api_models import FeedItem

    for field in (
        "revision_note",
        "revision_target_article_id",
        "chain_progress_note",
    ):
        assert field not in FeedItem.model_fields

    with pytest.raises(ValidationError):
        FeedItem(article_id="a", revision_note="")


def test_event_chain_annotations_are_disabled_by_default():
    from app.config import Settings

    assert Settings().event_chain_annotations_enabled is False


@pytest.mark.asyncio
async def test_annotated_items_still_validate_through_the_real_feed_response(wired):
    """DR-13251: annotate, then validate through the model the reader returns.

    The outage was invisible to unit tests because they called the annotation
    functions directly and never built a FeedResponse. A field attached but not
    declared on FeedItem passes every such test and 500s in production, so the
    assertion has to run through the real response model.
    """
    from app.app_api_models import FeedResponse

    items = [_item("a1"), _item("a2")]
    annotated = await api._annotate_crypto_items(items)

    allowed = set(FeedItem.model_fields)
    for item in annotated:
        leaked = set(item) - allowed - {"map"}
        assert not leaked, f"fields not in the FeedItem contract: {sorted(leaked)}"

    FeedResponse(
        items=[
            FeedItem(status="ok", **{k: v for k, v in item.items() if k in allowed})
            for item in annotated
        ],
        cooldown_seconds=0.0,
    )


def test_crypto_map_model_override_leaves_core_untouched():
    """DR-13253: crypto may need a different typesetting model than core.

    Empty override means inherit, so core behaviour is unchanged unless an
    operator sets MAP_MODEL_CRYPTO deliberately.
    """
    from app.config import Settings

    default = Settings(OPENROUTER_MODEL="global/model")
    assert default.map_model_for("core") == "global/model"
    assert default.map_model_for("crypto") == "global/model"

    split = Settings(OPENROUTER_MODEL="global/model", MAP_MODEL_CRYPTO="other/model")
    assert split.map_model_for("core") == "global/model"
    assert split.map_model_for("crypto") == "other/model"
    # An unknown domain must not inherit crypto's override.
    assert split.map_model_for("climate") == "global/model"
