"""Offline unit tests for normalization — no network or DB required."""
from datetime import timezone

from app.models import Article, canonical_article_id


def test_from_api_full():
    item = {
        "url": "https://example.com/a",
        "url_mobile": "",
        "title": " Hello World ",
        "seendate": "20260710T094500Z",
        "socialimage": "https://img/x.jpg",
        "domain": "example.com",
        "language": "English",
        "sourcecountry": "United States",
    }
    a = Article.from_api(item, source_query="test")
    assert a.id == canonical_article_id("https://example.com/a")
    assert a.title == "Hello World"
    assert a.seendate.tzinfo == timezone.utc
    assert a.seendate.year == 2026 and a.seendate.hour == 9
    assert a.socialimage_url == "https://img/x.jpg"
    assert a.has_image is True
    assert a.source_query == "test"


def test_from_api_missing_image():
    a = Article.from_api({"url": "https://x/y", "socialimage": ""})
    assert a.has_image is False
    assert a.socialimage_url is None


def test_bad_seendate_is_none():
    a = Article.from_api({"url": "https://x/y", "seendate": "not-a-date"})
    assert a.seendate is None


def test_id_is_stable_and_urlsafe():
    i1 = canonical_article_id("https://x/y")
    i2 = canonical_article_id(" https://x/y ")
    assert i1 == i2
    assert len(i1) == 64
