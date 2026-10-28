"""Bounded primary-document discovery and persistence (DR-13217)."""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import httpx
import pytest
import yaml

from app.config import Settings
from app.models import canonical_article_id
from app.primary_document_worker import PrimaryDocumentWorker


class FakeRepo:
    def __init__(self, *, outstanding: int = 0, existing: set[str] | None = None):
        self.outstanding = outstanding
        self.existing = set(existing or ())
        self.started: list[tuple[int, str, str]] = []
        self.finished: list[tuple[int, dict]] = []
        self.missing_calls: list[list[str]] = []
        self.articles = []

    async def primary_document_outstanding_count(self) -> int:
        return self.outstanding

    async def start_run(self, job_name: str, query: str) -> int:
        run_id = len(self.started) + 1
        self.started.append((run_id, job_name, query))
        return run_id

    async def finish_run(self, run_id: int, **values) -> None:
        self.finished.append((run_id, values))

    async def missing_article_ids(self, article_ids: list[str]) -> set[str]:
        self.missing_calls.append(list(article_ids))
        return {value for value in article_ids if value not in self.existing}

    async def upsert_article(self, article) -> str:
        self.articles.append(article)
        if article.id in self.existing:
            return "updated"
        self.existing.add(article.id)
        return "inserted"


def _source(
    name: str,
    *,
    discovery: str = "rss",
    url: str | None = None,
    prefix: str | None = None,
    document_type: str = "governance_proposal",
) -> dict:
    return {
        "name": name,
        "document_type": document_type,
        "discovery": discovery,
        "url": url or f"https://{name}.example/feed.xml",
        "allow_url_prefixes": [prefix or f"https://{name}.example/docs/"],
        "enabled": True,
    }


def _write_sources(tmp_path: Path, sources: list[dict]) -> str:
    path = tmp_path / "primary_documents.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "sources": sources}, sort_keys=False),
        encoding="utf-8",
    )
    return str(path)


def _settings(path: str, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        PRIMARY_DOCUMENTS_FILE=path,
        **overrides,
    )


def _rss(base: str, count: int, *, fragment: bool = False) -> str:
    items = []
    for index in range(count):
        suffix = "#section" if fragment else ""
        items.append(
            "<item>"
            f"<title>RSS document {index}</title>"
            f"<link>{base}{index}{suffix}</link>"
            "<pubDate>Sun, 09 Aug 2026 12:00:00 GMT</pubDate>"
            "</item>"
        )
    return "<rss><channel>" + "".join(items) + "</channel></rss>"


def _atom(base: str, count: int) -> str:
    entries = []
    for index in range(count):
        entries.append(
            "<entry>"
            f"<title>Atom document {index}</title>"
            f'<link rel="self" href="{base}feed/{index}" />'
            f'<link rel="alternate" href="{base}{index}#atom" />'
            "<updated>2026-08-09T12:00:00Z</updated>"
            "</entry>"
        )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(entries)
        + "</feed>"
    )


def _index(count: int) -> str:
    anchors = [
        f'<a href="/news/{index}#details"><span>Index</span> document {index}</a>'
        for index in range(count)
    ]
    return "<html><body>" + "".join(anchors) + "</body></html>"


def _client(responses: dict[str, tuple[int, str]]) -> tuple[httpx.AsyncClient, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        assert request.headers["user-agent"] == "text-shorts-primary-documents/1.0"
        status, text = responses[url]
        return httpx.Response(
            status,
            text=text,
            headers={"content-type": "application/xml; charset=utf-8"},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requested


@pytest.mark.asyncio
async def test_rss_atom_and_index_are_bounded_per_source_and_tick(tmp_path):
    sources = [
        _source(
            "rss",
            url="https://rss.example/feed.xml",
            prefix="https://rss.example/docs/",
        ),
        _source(
            "atom",
            url="https://atom.example/feed.xml",
            prefix="https://atom.example/docs/",
            document_type="post_mortem",
        ),
        _source(
            "index",
            discovery="index_html",
            url="https://index.example/news",
            prefix="https://index.example/news/",
            document_type="foundation_notice",
        ),
    ]
    path = _write_sources(tmp_path, sources)
    client, requested = _client(
        {
            "https://rss.example/feed.xml": (
                200,
                _rss("https://rss.example/docs/", 51, fragment=True),
            ),
            "https://atom.example/feed.xml": (
                200,
                _atom("https://atom.example/docs/", 51),
            ),
            "https://index.example/news": (200, _index(51)),
        }
    )
    repo = FakeRepo()

    async with client:
        worker = PrimaryDocumentWorker(_settings(path), repo, client=client)
        result = await worker.run_once()
        await worker.close()
        assert client.is_closed is False

    assert result.scanned == 150
    assert result.eligible == 150
    assert result.inserted == 7
    assert result.skipped_existing == 0
    assert result.rejected == 0
    assert result.failed_sources == ()
    assert requested == [source["url"] for source in sources]
    assert len(repo.missing_calls) == 1
    assert len(repo.missing_calls[0]) == 150
    assert len(repo.articles) == 7
    assert all("#" not in article.url for article in repo.articles)
    assert repo.articles[0].seendate.tzinfo == UTC
    assert [values["fetched"] for _run, values in repo.finished] == [50, 50, 50]
    assert all(values["ok"] for _run, values in repo.finished)


@pytest.mark.asyncio
async def test_yaml_order_and_fragment_dedupe_keep_first_attribution(tmp_path):
    first = _source(
        "first",
        url="https://first.example/feed.xml",
        prefix="https://shared.example/docs/",
        document_type="governance_proposal",
    )
    second = _source(
        "second",
        url="https://second.example/feed.xml",
        prefix="https://shared.example/",
        document_type="post_mortem",
    )
    path = _write_sources(tmp_path, [first, second])
    first_payload = """<rss><channel>
      <item><title>First owner</title><link>https://shared.example/docs/one#first</link></item>
      <item><title>Outside</title><link>https://outside.example/one</link></item>
      <item><title>   </title><link>https://shared.example/docs/no-title</link></item>
    </channel></rss>"""
    second_payload = """<rss><channel>
      <item><title>Second owner</title><link>https://shared.example/docs/one#second</link></item>
    </channel></rss>"""
    client, _requested = _client(
        {
            first["url"]: (200, first_payload),
            second["url"]: (200, second_payload),
        }
    )
    repo = FakeRepo()

    async with client:
        result = await PrimaryDocumentWorker(
            _settings(path), repo, client=client
        ).run_once()

    assert result.scanned == 4
    assert result.eligible == 1
    assert result.inserted == 1
    assert result.rejected == 3
    assert result.failed_sources == ()
    article = repo.articles[0]
    assert article.url == "https://shared.example/docs/one"
    assert article.coverage_domain == "crypto"
    assert article.document_type == "governance_proposal"
    assert article.document_source_name == "first"
    assert article.source_query == "primary-document:first"


@pytest.mark.asyncio
async def test_existing_ids_are_filtered_once_and_new_inserts_stop_at_seven(tmp_path):
    source = _source("bounded")
    path = _write_sources(tmp_path, [source])
    urls = [f"https://bounded.example/docs/{index}" for index in range(10)]
    existing = {canonical_article_id(urls[0]), canonical_article_id(urls[1])}
    repo = FakeRepo(existing=existing)
    client, _requested = _client({source["url"]: (200, _rss(urls[0][:-1], 10))})

    async with client:
        result = await PrimaryDocumentWorker(
            _settings(path), repo, client=client
        ).run_once()

    assert len(repo.missing_calls) == 1
    assert repo.missing_calls[0] == [canonical_article_id(url) for url in urls]
    assert result.eligible == 10
    assert result.skipped_existing == 2
    assert result.inserted == 7
    assert len(repo.articles) == 7


@pytest.mark.asyncio
async def test_outstanding_backpressure_skips_network_and_near_cap_limits_insert(tmp_path):
    source = _source("pressure")
    path = _write_sources(tmp_path, [source])

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("backpressure must run before source HTTP")

    client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    held = FakeRepo(outstanding=28)
    async with client:
        result = await PrimaryDocumentWorker(
            _settings(path), held, client=client
        ).run_once()
    assert result.inserted == 0
    assert result.scanned == 0
    assert held.started == []
    assert held.missing_calls == []

    near = FakeRepo(outstanding=27)
    client, _requested = _client(
        {source["url"]: (200, _rss("https://pressure.example/docs/", 3))}
    )
    async with client:
        near_result = await PrimaryDocumentWorker(
            _settings(path), near, client=client
        ).run_once()
    assert near_result.inserted == 1
    assert len(near.articles) == 1


@pytest.mark.asyncio
async def test_source_failure_is_isolated_and_each_source_audit_is_closed(tmp_path):
    bad = _source("bad")
    good = _source("good", document_type="post_mortem")
    path = _write_sources(tmp_path, [bad, good])
    client, requested = _client(
        {
            bad["url"]: (503, "unavailable"),
            good["url"]: (200, _rss("https://good.example/docs/", 1)),
        }
    )
    repo = FakeRepo()

    async with client:
        result = await PrimaryDocumentWorker(
            _settings(path), repo, client=client
        ).run_once()

    assert requested == [bad["url"], good["url"]]
    assert result.failed_sources == ("bad",)
    assert result.inserted == 1
    assert [name for _id, name, _query in repo.started] == [
        "primary-document:bad",
        "primary-document:good",
    ]
    assert len(repo.finished) == 2
    by_run = {run_id: values for run_id, values in repo.finished}
    assert by_run[1]["ok"] is False
    assert by_run[1]["error"] == "primary_document_http_status"
    assert by_run[2]["ok"] is True
    assert by_run[2]["inserted"] == 1


@pytest.mark.asyncio
async def test_empty_or_ineligible_source_is_failed_without_stopping_next(tmp_path):
    empty = _source("empty")
    good = _source("after")
    path = _write_sources(tmp_path, [empty, good])
    client, _requested = _client(
        {
            empty["url"]: (200, "<rss><channel></channel></rss>"),
            good["url"]: (200, _rss("https://after.example/docs/", 1)),
        }
    )
    repo = FakeRepo()

    async with client:
        result = await PrimaryDocumentWorker(
            _settings(path), repo, client=client
        ).run_once()

    assert result.failed_sources == ("empty",)
    assert result.inserted == 1
    assert repo.finished[0][1]["error"] == "primary_document_no_eligible_entries"
    assert repo.finished[1][1]["ok"] is True


@pytest.mark.asyncio
async def test_close_is_idempotent_and_only_closes_an_owned_client(tmp_path):
    path = _write_sources(tmp_path, [])
    settings = _settings(path)

    owned_worker = PrimaryDocumentWorker(settings, FakeRepo())
    owned_client = owned_worker._http_client()
    await owned_worker.close()
    await owned_worker.close()
    assert owned_client.is_closed is True
    with pytest.raises(RuntimeError, match="closed"):
        await owned_worker.run_once()

    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
    injected_worker = PrimaryDocumentWorker(settings, FakeRepo(), client=injected)
    await injected_worker.close()
    assert injected.is_closed is False
    await injected.aclose()
