from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.timeline_copy import TimelineCopy, TimelineCopyInput, TimelineCopyPolisher


NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _plans(count: int = 4):
    return [
        TimelineCopyInput(
            stable_key=f"timeline-{index}",
            topic=f"AI 인프라 {index}",
            started_at=NOW - timedelta(days=10),
            ended_at=NOW,
            headlines=(f"사건 {index}-1", f"사건 {index}-2", f"사건 {index}-3", f"사건 {index}-4"),
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, response=None, *, configured=True, error=None):
        self.response = response
        self.configured = configured
        self.error = error
        self.calls = []

    async def complete_json(self, system, user_text, **kwargs):
        self.calls.append((system, user_text, kwargs))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_copy_polisher_uses_one_batched_structured_call():
    plans = _plans(8)
    response = {
        "timelines": [
            {
                "stable_key": item.stable_key,
                "title": f"{item.topic}의 변화",
                "summary": f"{item.topic}에서 이어진 주요 사건을 발행 흐름에 맞춰 설명합니다.",
            }
            for item in plans
        ]
    }
    client = FakeClient(response)

    result = await TimelineCopyPolisher(client).polish(plans)

    assert len(client.calls) == 1
    assert set(result) == {item.stable_key for item in plans}
    assert client.calls[0][2]["output_schema"].name == "explore_timeline_copy_v1"
    assert "main" not in client.calls[0][1]
    assert "branch" not in client.calls[0][1]


@pytest.mark.asyncio
async def test_copy_polisher_rejects_shape_and_falls_back_without_changing_keys():
    plans = _plans()
    previous = {"timeline-0": TimelineCopy("이전 타임라인", "이전에 검증되어 발행된 타임라인 설명을 그대로 유지합니다.")}
    client = FakeClient({"timelines": []})

    result = await TimelineCopyPolisher(client).polish(plans, previous)

    assert result["timeline-0"] == previous["timeline-0"]
    assert set(result) == {item.stable_key for item in plans}
    assert all(4 <= len(copy.title) <= 100 for copy in result.values())
    assert all(20 <= len(copy.summary) <= 240 for copy in result.values())


@pytest.mark.asyncio
async def test_copy_polisher_provider_unavailable_uses_deterministic_fallback():
    plans = _plans()
    client = FakeClient(configured=False)

    first = await TimelineCopyPolisher(client).polish(plans)
    second = await TimelineCopyPolisher(client).polish(plans)

    assert first == second
    assert client.calls == []


@pytest.mark.parametrize("count", [0, 3, 9])
@pytest.mark.asyncio
async def test_copy_polisher_requires_four_to_eight_plans(count):
    with pytest.raises(ValueError, match="4..8"):
        await TimelineCopyPolisher(FakeClient(configured=False)).polish(_plans(count))
