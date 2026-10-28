from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app import editorial_cli


TOKEN = "c" * 43


def client_factory(handler, calls):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        calls.append(kwargs)
        return httpx.Client(transport=transport, **kwargs)

    return factory


def test_command_surface_excludes_every_public_mutation_before_network(monkeypatch):
    forbidden = {
        "publish",
        "cancel",
        "withdraw",
        "today-order",
        "trash",
        "delete",
        "restore",
        "purge",
    }
    assert forbidden.isdisjoint(editorial_cli.EDITOR_COMMANDS)
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        raise AssertionError("parser must fail before network")

    with pytest.raises(SystemExit) as error:
        editorial_cli.main(
            ["--server", "https://desk.example", "publish"],
            client_factory=factory,
        )
    assert error.value.code == 2
    assert calls == []


@pytest.mark.parametrize(
    ("command", "schedule_version"),
    [("schedule", None), ("reschedule", 7)],
)
def test_schedule_commands_send_exact_cas_contract(
    command, schedule_version, monkeypatch, capsys
):
    requests = []
    scheduled_for = datetime.now(UTC) + timedelta(minutes=10)

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "draft_id": "11111111-1111-1111-1111-111111111111",
                "version": 1,
                "state": "scheduled",
                "scheduled_for": scheduled_for.isoformat(),
                "snapshot_draft_version": 3,
            },
        )

    argv = [
        command,
        "11111111-1111-1111-1111-111111111111",
        "--expected-draft-version",
        "3",
        "--scheduled-for",
        scheduled_for.isoformat(),
    ]
    if schedule_version is not None:
        argv.extend(["--expected-schedule-version", str(schedule_version)])
    monkeypatch.setenv("EDITORIAL_TOKEN", TOKEN)

    assert editorial_cli.main(
        ["--server", "https://desk.example", *argv],
        client_factory=client_factory(handler, []),
    ) == 0
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path == (
        "/v1/editor/drafts/11111111-1111-1111-1111-111111111111/schedule"
    )
    assert json.loads(request.content) == {
        "expected_draft_version": 3,
        "expected_schedule_version": schedule_version,
        "scheduled_for": scheduled_for.isoformat(),
    }
    capsys.readouterr()


@pytest.mark.parametrize(
    ("scheduled_for", "error_code"),
    [
        ("2026-09-02T10:00:00", "scheduled_for_timezone_required"),
        (
            (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "scheduled_for_too_far",
        ),
    ],
)
def test_schedule_rejects_invalid_time_before_network(
    scheduled_for, error_code, monkeypatch, capsys
):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        raise AssertionError("must not create client")

    monkeypatch.setenv("EDITORIAL_TOKEN", TOKEN)
    assert editorial_cli.main(
        [
            "--server",
            "https://desk.example",
            "schedule",
            "11111111-1111-1111-1111-111111111111",
            "--expected-draft-version",
            "3",
            "--scheduled-for",
            scheduled_for,
        ],
        client_factory=factory,
    ) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().err)["error"]["code"] == error_code


def test_schedule_local_window_includes_one_minute_and_one_hour():
    now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    earliest = (now + timedelta(seconds=60)).isoformat()
    latest = (now + timedelta(seconds=3_600)).isoformat()

    assert editorial_cli._scheduled_for_value(earliest, now=now) == earliest
    assert editorial_cli._scheduled_for_value(latest, now=now) == latest
    with pytest.raises(editorial_cli.EditorialCLIError) as too_soon:
        editorial_cli._scheduled_for_value(
            (now + timedelta(seconds=59)).isoformat(), now=now
        )
    with pytest.raises(editorial_cli.EditorialCLIError) as too_far:
        editorial_cli._scheduled_for_value(
            (now + timedelta(seconds=3_601)).isoformat(), now=now
        )

    assert too_soon.value.code == "scheduled_for_too_soon"
    assert too_far.value.code == "scheduled_for_too_far"


def test_draft_create_uses_bearer_header_and_json_stdout(
    tmp_path, monkeypatch, capsys
):
    document = {"format_version": 3, "headline": "CLI 초안"}
    source = tmp_path / "draft.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    requests = []
    factories = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"id": str(uuid4()), "version": 1})

    monkeypatch.setenv("EDITORIAL_TOKEN", TOKEN)
    result = editorial_cli.main(
        [
            "--server",
            "https://desk.example/",
            "draft-create",
            "--file",
            str(source),
        ],
        client_factory=client_factory(handler, factories),
    )

    assert result == 0
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://desk.example/v1/editor/drafts"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert json.loads(request.content) == {"document": document}
    assert json.loads(capsys.readouterr().out)["version"] == 1


@pytest.mark.parametrize(
    ("argv", "method", "path"),
    [
        (["me"], "GET", "/v1/editor/me"),
        (["draft-list"], "GET", "/v1/editor/drafts"),
        (["today"], "GET", "/v1/editor/today"),
        (["schedule-list"], "GET", "/v1/editor/schedules"),
        (
            ["schedule-get", "11111111-1111-1111-1111-111111111111"],
            "GET",
            "/v1/editor/drafts/11111111-1111-1111-1111-111111111111/schedule",
        ),
    ],
)
def test_read_commands_have_exact_remote_contract(
    argv, method, path, monkeypatch, capsys
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setenv("EDITORIAL_TOKEN", TOKEN)
    assert editorial_cli.main(
        ["--server", "https://desk.example", *argv],
        client_factory=client_factory(handler, []),
    ) == 0
    assert requests[0].method == method
    assert requests[0].url.path == path
    capsys.readouterr()


def test_operator_create_uses_master_header_and_returns_one_time_token(
    monkeypatch, capsys
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"id": str(uuid4()), "token": "one-time"})

    monkeypatch.setenv("EDITORIAL_MASTER_TOKEN", "master-value")
    assert editorial_cli.main(
        [
            "--server",
            "https://desk.example",
            "operator-create",
            "--display-name",
            "CLI 편집자",
        ],
        client_factory=client_factory(handler, []),
    ) == 0
    assert requests[0].headers["X-API-Token"] == "master-value"
    assert "Authorization" not in requests[0].headers
    assert json.loads(capsys.readouterr().out)["token"] == "one-time"


def test_http_and_network_errors_never_echo_token(monkeypatch, capsys):
    monkeypatch.setenv("EDITORIAL_TOKEN", TOKEN)

    def rejected(_request):
        return httpx.Response(
            401,
            json={"error": {"code": "invalid_or_missing_editor_token"}},
        )

    result = editorial_cli.main(
        ["--server", "https://desk.example", "me"],
        client_factory=client_factory(rejected, []),
    )
    output = capsys.readouterr()
    assert result == 3
    assert json.loads(output.err)["error"]["code"] == "invalid_or_missing_editor_token"
    assert TOKEN not in output.err + output.out

    def broken(**kwargs):
        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def request(self, *_args, **_kwargs):
                raise httpx.ConnectError("secret transport")

        return Client()

    result = editorial_cli.main(
        ["--server", "https://desk.example", "me"], client_factory=broken
    )
    output = capsys.readouterr()
    assert result == 4
    assert json.loads(output.err)["error"]["code"] == "network_error"
    assert TOKEN not in output.err + output.out


def test_invalid_origin_or_missing_token_stops_before_network(monkeypatch, capsys):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        raise AssertionError("must not create client")

    monkeypatch.delenv("EDITORIAL_TOKEN", raising=False)
    assert editorial_cli.main(
        ["--server", "http://remote.example", "me"], client_factory=factory
    ) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "insecure_server_origin"


def test_asset_upload_accepts_only_supported_local_images(tmp_path, monkeypatch, capsys):
    image = tmp_path / "cover.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"image_id": "a" * 64})

    monkeypatch.setenv("EDITORIAL_TOKEN", TOKEN)
    assert editorial_cli.main(
        ["--server", "https://desk.example", "asset-upload", str(image)],
        client_factory=client_factory(handler, []),
    ) == 0
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/editor/assets"
    assert b"image/png" in requests[0].content
    capsys.readouterr()
