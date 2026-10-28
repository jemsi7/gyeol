"""Static and route contracts for the read-only operations dashboard."""

from pathlib import Path

import httpx
import pytest

from app.api import app


DASHBOARD = Path(__file__).parents[1] / "app" / "ops_dashboard.html"


def test_dashboard_static_security_refresh_and_accessibility_contract():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'lang="ko"' in html
    assert 'id="token-form"' in html
    assert 'sessionStorage.getItem(TOKEN_KEY)' in html
    assert 'sessionStorage.setItem(TOKEN_KEY, token)' in html
    assert "localStorage" not in html
    assert '"X-API-Token": token' in html
    assert "const POLL_MS = 5000" in html
    assert 'addEventListener("visibilitychange"' in html
    assert 'role="progressbar"' in html or 'setAttribute("role", "progressbar")' in html
    assert 'role="tablist"' in html
    assert 'aria-live="polite"' in html
    assert "Editorial push" in html
    assert "이용 가능 기사" in html
    assert 'id="metric-available"' in html
    assert 'metric(dom["metric-available"], snapshot.available_articles)' in html
    assert "grid-template-columns: repeat(5, 1fr)" in html
    assert "@media (max-width: 1200px)" in html
    assert "기사 텍스트 임베딩" in html
    assert 'id="article-embedding-title"' in html
    assert 'id="article-embedding-count"' in html
    assert 'id="article-embedding-percent"' in html
    assert 'id="article-embedding-progress"' in html
    assert 'id="article-embedding-updated"' in html
    assert 'aria-label="전체 대상 기사 중 임베딩 완료 비율"' in html
    assert "renderArticleEmbeddings(snapshot.article_embeddings)" in html
    assert 'removeAttribute("aria-valuenow")' in html
    assert 'setAttribute("aria-valuenow"' in html
    assert 'setAttribute("aria-valuetext"' in html
    assert "embedded_count" in html
    assert "total_count" in html
    assert "progress?.status === \"ready\"" in html
    assert 'progress?.status === "disabled"' in html
    assert 'id="push-sent"' in html
    assert 'id="push-retries"' in html
    assert 'id="push-failed"' in html
    assert "renderPush(snapshot.push)" in html
    assert "innerHTML" not in html
    assert "queue/drain" not in html
    assert "linear-gradient" in html  # loading skeleton only, never text
    assert "background-clip: text" not in html


def test_dashboard_has_five_exclusive_views_analytics_controls_and_kpis():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'aria-label="운영 대시보드 보기"' in html
    assert html.count('class="view-tab"') == 5
    for view in ("overview", "users", "activity", "content", "system"):
        assert f'data-view="{view}"' in html
        assert f'id="panel-{view}"' in html
        assert f'aria-controls="panel-{view}"' in html
    for metric_id in (
        "analytics-total-members",
        "analytics-today-dau",
        "analytics-current-mau",
        "analytics-stickiness",
        "analytics-total-views",
    ):
        assert f'id="{metric_id}"' in html
    assert "전체 사용자" in html
    assert "계정 + 익명 설치 활동 coverage" in html
    assert "ArrowLeft" in html
    assert "ArrowRight" in html
    assert "Home" in html
    assert "End" in html
    assert "activateView" in html


def test_dashboard_uses_bounded_analytics_charts_and_paginated_top_100():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert "/ops/api/analytics?" in html
    assert "ANALYTICS_POLL_MS = 60000" in html
    assert "dailyDays" in html
    assert "monthlyMonths" in html
    assert 'data-days="30"' in html
    assert 'data-days="90"' in html
    assert 'data-days="365"' in html
    assert 'data-months="6"' in html
    assert 'data-months="12"' in html
    assert 'data-activity-view="dau"' in html
    assert 'data-activity-view="mau"' in html
    assert 'data-activity-view="mix"' in html
    assert "createElementNS" in html
    assert "renderUserTrend" in html
    assert "renderActivityStage" in html
    assert "TOP_PAGE_SIZE = 25" in html
    assert "renderPopularArticles" in html
    assert 'id="popular-articles-body"' in html
    assert 'id="popular-prev"' in html
    assert 'id="popular-next"' in html
    assert 'data-content-ranking="popular"' in html
    assert 'data-content-ranking="high"' in html
    assert 'data-content-ranking="low"' in html
    assert "high_view_rate_articles" in html
    assert "low_view_rate_articles" in html
    assert "view_rate_percent" in html
    assert "impression_count" in html
    assert "first_impressed_at" in html
    assert "최소 노출 5회" in html
    assert "100%를 넘을 수 있습니다" in html
    assert "개별 클릭률이 아닙니다" in html
    assert "popularPage = 0" in html
    assert "innerHTML" not in html


def test_dashboard_surfaces_kst_session_coverage_and_preserves_system_view():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert "Asia/Seoul" in html
    assert "로그인 세션 갱신" in html
    assert 'id="analytics-coverage"' in html
    assert 'id="analytics-updated"' in html
    assert 'id="panel-system"' in html
    assert 'id="budget-form"' in html
    assert 'id="queue-body"' in html
    assert "refreshSnapshot" in html
    assert "POLL_MS = 5000" in html


@pytest.mark.asyncio
async def test_ops_dashboard_shell_is_public_but_contains_no_secret():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ops")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "작업 운영 현황" in response.text
    assert "이용 가능 기사" in response.text
    assert "기사 텍스트 임베딩" in response.text
    assert "ops-secret" not in response.text
