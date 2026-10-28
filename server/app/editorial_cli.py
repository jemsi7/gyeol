"""Remote, JSON-only CLI for preparing and near-term scheduling drafts.

Only explicit, CAS-protected schedules within one hour are available. Immediate
publish and all other public-state or draft-lifecycle mutations remain absent.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID

import httpx


EDITOR_COMMANDS = (
    "operator-create",
    "operator-revoke",
    "me",
    "draft-list",
    "draft-get",
    "draft-create",
    "draft-update",
    "asset-upload",
    "validate",
    "revisions",
    "today",
    "schedule-list",
    "schedule-get",
    "schedule",
    "reschedule",
)
_MASTER_COMMANDS = {"operator-create", "operator-revoke"}
_JSON_MAX_BYTES = 1_000_000
_UPLOAD_MAX_BYTES = 16 * 1024 * 1024
_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class EditorialCLIError(RuntimeError):
    def __init__(self, code: str, exit_code: int):
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="editorial-desk",
        description="Prepare drafts and explicitly schedule them within one hour",
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("EDITORIAL_SERVER_URL", ""),
        help="HTTPS server origin (or EDITORIAL_SERVER_URL)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create_operator = commands.add_parser("operator-create")
    create_operator.add_argument("--display-name", required=True)
    create_operator.add_argument("--role", choices=("admin", "editor"), default="editor")

    revoke_operator = commands.add_parser("operator-revoke")
    revoke_operator.add_argument("operator_id", type=_uuid)

    commands.add_parser("me")
    commands.add_parser("draft-list")

    draft_get = commands.add_parser("draft-get")
    draft_get.add_argument("draft_id", type=_uuid)

    draft_create = commands.add_parser("draft-create")
    draft_create.add_argument("--file", default="-", help="EditorialDocument JSON or -")

    draft_update = commands.add_parser("draft-update")
    draft_update.add_argument("draft_id", type=_uuid)
    draft_update.add_argument("--expected-version", required=True, type=_positive_int)
    draft_update.add_argument("--file", default="-", help="EditorialDocument JSON or -")

    upload = commands.add_parser("asset-upload")
    upload.add_argument("path")
    upload.add_argument("--content-type", choices=tuple(sorted(set(_IMAGE_TYPES.values()))))

    validate = commands.add_parser("validate")
    validate.add_argument("draft_id", type=_uuid)

    revisions = commands.add_parser("revisions")
    revisions.add_argument("draft_id", type=_uuid)

    commands.add_parser("today")
    commands.add_parser("schedule-list")

    schedule_get = commands.add_parser("schedule-get")
    schedule_get.add_argument("draft_id", type=_uuid)

    schedule = commands.add_parser("schedule")
    schedule.add_argument("draft_id", type=_uuid)
    schedule.add_argument("--expected-draft-version", required=True, type=_positive_int)
    schedule.add_argument("--scheduled-for", required=True)

    reschedule = commands.add_parser("reschedule")
    reschedule.add_argument("draft_id", type=_uuid)
    reschedule.add_argument("--expected-draft-version", required=True, type=_positive_int)
    reschedule.add_argument(
        "--expected-schedule-version", required=True, type=_positive_int
    )
    reschedule.add_argument("--scheduled-for", required=True)
    return parser


def _uuid(raw: str) -> str:
    try:
        return str(UUID(raw))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a UUID") from error


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _scheduled_for_value(raw: str, *, now: datetime | None = None) -> str:
    normalized = raw.strip()
    parse_value = normalized[:-1] + "+00:00" if normalized.endswith(("Z", "z")) else normalized
    try:
        value = datetime.fromisoformat(parse_value)
    except ValueError as error:
        raise EditorialCLIError("scheduled_for_invalid", 2) from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise EditorialCLIError("scheduled_for_timezone_required", 2)
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    delta = value.astimezone(UTC) - now_utc
    if delta < timedelta(seconds=60):
        raise EditorialCLIError("scheduled_for_too_soon", 2)
    if delta > timedelta(hours=1):
        raise EditorialCLIError("scheduled_for_too_far", 2)
    return normalized


def _normalize_origin(raw: str) -> str:
    try:
        parsed = urlsplit(raw.strip())
        port = parsed.port
    except ValueError as error:
        raise EditorialCLIError("invalid_server_origin", 2) from error
    host = (parsed.hostname or "").lower()
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise EditorialCLIError("invalid_server_origin", 2)
    scheme = parsed.scheme.lower()
    local = host in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and local):
        raise EditorialCLIError("insecure_server_origin", 2)
    display_host = f"[{host}]" if ":" in host else host
    netloc = f"{display_host}:{port}" if port is not None else display_host
    normalized = SplitResult(scheme, netloc, "", "", "")
    return urlunsplit(normalized)


def _read_document(path_value: str) -> dict[str, Any]:
    try:
        if path_value == "-":
            raw = sys.stdin.buffer.read(_JSON_MAX_BYTES + 1)
        else:
            path = Path(path_value)
            if path.stat().st_size > _JSON_MAX_BYTES:
                raise EditorialCLIError("json_too_large", 2)
            raw = path.read_bytes()
    except EditorialCLIError:
        raise
    except OSError as error:
        raise EditorialCLIError("json_read_failed", 2) from error
    if not raw:
        raise EditorialCLIError("json_input_empty", 2)
    if len(raw) > _JSON_MAX_BYTES:
        raise EditorialCLIError("json_too_large", 2)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EditorialCLIError("invalid_json", 2) from error
    if not isinstance(document, dict):
        raise EditorialCLIError("document_must_be_object", 2)
    return document


def _request_spec(args: argparse.Namespace) -> tuple[str, str, dict[str, Any]]:
    command = args.command
    if command == "operator-create":
        return "POST", "/v1/editor/operators", {
            "json": {"display_name": args.display_name, "role": args.role}
        }
    if command == "operator-revoke":
        return "DELETE", f"/v1/editor/operators/{args.operator_id}", {}
    if command == "me":
        return "GET", "/v1/editor/me", {}
    if command == "draft-list":
        return "GET", "/v1/editor/drafts", {}
    if command == "draft-get":
        return "GET", f"/v1/editor/drafts/{args.draft_id}", {}
    if command == "draft-create":
        return "POST", "/v1/editor/drafts", {
            "json": {"document": _read_document(args.file)}
        }
    if command == "draft-update":
        return "PUT", f"/v1/editor/drafts/{args.draft_id}", {
            "json": {
                "expected_version": args.expected_version,
                "document": _read_document(args.file),
            }
        }
    if command == "asset-upload":
        path = Path(args.path)
        try:
            size = path.stat().st_size
            if size <= 0 or size > _UPLOAD_MAX_BYTES:
                raise EditorialCLIError("asset_size_invalid", 2)
            data = path.read_bytes()
        except EditorialCLIError:
            raise
        except OSError as error:
            raise EditorialCLIError("asset_read_failed", 2) from error
        content_type = args.content_type or _IMAGE_TYPES.get(path.suffix.lower())
        if content_type not in _IMAGE_TYPES.values():
            guessed, _ = mimetypes.guess_type(path.name)
            content_type = guessed if guessed in _IMAGE_TYPES.values() else None
        if content_type is None:
            raise EditorialCLIError("unsupported_asset_type", 2)
        return "POST", "/v1/editor/assets", {
            "files": {"file": (path.name, data, content_type)}
        }
    if command == "validate":
        return "POST", f"/v1/editor/drafts/{args.draft_id}/validate", {}
    if command == "revisions":
        return "GET", f"/v1/editor/drafts/{args.draft_id}/revisions", {}
    if command == "today":
        return "GET", "/v1/editor/today", {}
    if command == "schedule-list":
        return "GET", "/v1/editor/schedules", {}
    if command == "schedule-get":
        return "GET", f"/v1/editor/drafts/{args.draft_id}/schedule", {}
    if command in {"schedule", "reschedule"}:
        schedule_version = (
            args.expected_schedule_version if command == "reschedule" else None
        )
        return "PUT", f"/v1/editor/drafts/{args.draft_id}/schedule", {
            "json": {
                "expected_draft_version": args.expected_draft_version,
                "expected_schedule_version": schedule_version,
                "scheduled_for": _scheduled_for_value(args.scheduled_for),
            }
        }
    raise EditorialCLIError("unsupported_command", 2)


def _write_json(value: Any, *, stream: Any) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
    stream.write("\n")


def _write_error(code: str, *, status: int | None = None) -> None:
    error: dict[str, Any] = {"code": code}
    if status is not None:
        error["status"] = status
    _write_json({"error": error}, stream=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., httpx.Client] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        origin = _normalize_origin(args.server)
        environment_name = (
            "EDITORIAL_MASTER_TOKEN"
            if args.command in _MASTER_COMMANDS
            else "EDITORIAL_TOKEN"
        )
        token = os.environ.get(environment_name, "").strip()
        if not token or any(character.isspace() for character in token):
            raise EditorialCLIError("editorial_token_required", 2)
        method, path, request_args = _request_spec(args)
        headers = {"Accept": "application/json"}
        if args.command in _MASTER_COMMANDS:
            headers["X-API-Token"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
        factory = client_factory or httpx.Client
        try:
            with factory(
                base_url=origin,
                timeout=30.0,
                follow_redirects=False,
                headers=headers,
            ) as client:
                response = client.request(method, path, **request_args)
        except httpx.HTTPError:
            raise EditorialCLIError("network_error", 4) from None
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            payload = None
        if response.status_code >= 400:
            code = f"http_{response.status_code}"
            if isinstance(payload, dict):
                body = payload.get("error")
                if isinstance(body, dict) and isinstance(body.get("code"), str):
                    code = body["code"]
            _write_error(code, status=response.status_code)
            return 3
        if payload is None:
            raise EditorialCLIError("invalid_json_response", 3)
        _write_json(payload, stream=sys.stdout)
        return 0
    except EditorialCLIError as error:
        _write_error(error.code)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
