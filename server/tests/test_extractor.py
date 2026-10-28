"""Deterministic article extraction fallback tests."""
from __future__ import annotations

import trafilatura

from app.extractor import _fallback_article_text, _parse


def test_parse_uses_semantic_html_fallback_when_primary_is_too_short(
    monkeypatch,
):
    paragraph = "Fallback article evidence stays inside the article element. " * 8
    html = (
        "<html><body><nav>Navigation noise " + ("x " * 300) + "</nav>"
        f"<article><p>{paragraph}</p></article></body></html>"
    )

    def fake_extract(_html, **kwargs):
        if kwargs.get("output_format") == "xml":
            return None
        return "Short primary."

    monkeypatch.setattr(trafilatura, "extract", fake_extract)

    body, images, strategy = _parse(
        html,
        "https://news.example/article",
        min_chars=300,
    )

    assert strategy == "semantic_html"
    assert body == paragraph.strip()
    assert "Navigation noise" not in body
    assert images == []


def test_parse_keeps_longer_primary_without_running_fallback(monkeypatch):
    primary = "Primary evidence. " * 30

    def fake_extract(_html, **kwargs):
        if kwargs.get("output_format") == "xml":
            return None
        return primary

    def fail_fallback(_html):
        raise AssertionError("fallback must not run for an eligible primary")

    monkeypatch.setattr(trafilatura, "extract", fake_extract)
    monkeypatch.setattr("app.extractor._fallback_article_text", fail_fallback)

    body, images, strategy = _parse(
        "<article><p>unused</p></article>",
        "https://news.example/article",
        min_chars=300,
    )

    assert body == primary.strip()
    assert strategy == "trafilatura"
    assert images == []


def test_fallback_ignores_non_article_chrome_and_scripts():
    html = """
    <header>Header noise</header>
    <nav>Navigation noise</nav>
    <main>
      <script>secret script noise</script>
      <p>First exact paragraph.</p>
      <p>Second exact paragraph.</p>
    </main>
    <footer>Footer noise</footer>
    """

    assert _fallback_article_text(html) == (
        "First exact paragraph.\n\nSecond exact paragraph."
    )
