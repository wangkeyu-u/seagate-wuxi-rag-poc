#!/usr/bin/env python3
"""Dependency-free HTTP boundary for the manufacturing evidence copilot.

The handler authenticates and validates external input before it reaches the
application service, maps internal failures to stable API errors, and attaches
one correlation ID to every response and structured access-log event.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from rag_app import TriageService
from rag_app.auth import ALLOWED_ROLES, AuthenticationError, AuthorizationError, Identity, TokenAuthenticator
from rag_app.generation import build_answer_generator
from rag_app.oidc import OidcAuthenticator
from rag_app.storage import DEFAULT_SOURCE_STALE_SECONDS, MAX_FEEDBACK_COMMENT_CHARS


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
APPLICATION_VERSION = "0.6.0"
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def validate_dev_auth_bind(host: str, enabled: bool) -> None:
    """Reject development identity minting unless every bind address is loopback."""

    if not enabled:
        return
    candidate = host.strip().strip("[]")
    if not candidate:
        raise ValueError("development authentication requires a loopback host")
    try:
        addresses = {str(ipaddress.ip_address(candidate))}
    except ValueError:
        try:
            addresses = {
                result[4][0]
                for result in socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise ValueError("development authentication requires a resolvable loopback host") from exc
    if not addresses or any(not is_loopback_address(address) for address in addresses):
        raise ValueError("development authentication requires a loopback host")


def build_authenticator(
    environment: Mapping[str, str],
    *,
    dev_auth: bool,
) -> tuple[TokenAuthenticator | OidcAuthenticator, str]:
    if dev_auth:
        return TokenAuthenticator(secrets.token_urlsafe(48)), "local-demo-hs256"
    mode = environment.get("RAG_AUTH_MODE", "gateway-hs256").strip().lower()
    if mode == "gateway-hs256":
        configured_secret = environment.get("RAG_AUTH_SECRET")
        if not configured_secret:
            raise ValueError("RAG_AUTH_SECRET is required for gateway-hs256 mode")
        return TokenAuthenticator(configured_secret), mode
    if mode == "oidc-rs256":
        return OidcAuthenticator.from_environment(environment), mode
    raise ValueError("RAG_AUTH_MODE must be gateway-hs256 or oidc-rs256")


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def parse_source_stale_thresholds(environment: Mapping[str, str]) -> dict[str, int]:
    raw = environment.get("RAG_SOURCE_STALE_SECONDS", "").strip()
    if not raw:
        return dict(DEFAULT_SOURCE_STALE_SECONDS)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RAG_SOURCE_STALE_SECONDS must be a JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != set(DEFAULT_SOURCE_STALE_SECONDS):
        raise ValueError("RAG_SOURCE_STALE_SECONDS must configure every approved source")
    for source_system, seconds in payload.items():
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 2_678_400:
            raise ValueError(f"invalid RAG_SOURCE_STALE_SECONDS value for {source_system}")
    return payload


class AppHandler(BaseHTTPRequestHandler):
    """Thin HTTP boundary; domain behavior lives in ``TriageService`` and storage.

    The handler deliberately normalizes identity, request correlation, CORS, and
    errors before routing. Keeping these controls in one boundary makes it harder
    for a new endpoint to accidentally bypass them.
    """

    server_version = f"YieldCopilot/{APPLICATION_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not log the raw request target: query strings may contain factory
        # identifiers. The request ID is enough to correlate with upstream logs.
        status = args[1] if len(args) > 1 else None
        print(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": self._request_id(),
                    "method": self.command,
                    "path": urlparse(self.path).path,
                    "status": status,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        if not origin or origin not in self._allowed_origins():
            return self._error(HTTPStatus.FORBIDDEN, "origin not allowed")
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("X-Request-ID", self._request_id())
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            self._enforce_origin()
            if path == "/api/health":
                return self._json(
                    {
                        "status": "ok",
                        "service": "yield-anomaly-copilot",
                        "mode": "synthetic-demo",
                        "version": APPLICATION_VERSION,
                        "authentication": "required-for-business-apis",
                        "generation": getattr(
                            self.server,
                            "generation_mode",
                            "deterministic evidence synthesis",
                        ),
                    }
                )
            if not path.startswith("/api/"):
                return self._serve_static(path)
            identity = self._require_identity()
            if path == "/api/whoami":
                return self._json(
                    {
                        "subject": identity.subject,
                        "role": identity.role,
                        "line_ids": list(identity.line_ids),
                        "station_ids": list(identity.station_ids),
                        "permissions": list(identity.permissions),
                        "auth_method": identity.auth_method,
                    }
                )
            service = self._service()
            if path == "/api/admin/source-health":
                if not identity.can_monitor_sources():
                    raise AuthorizationError("sources:monitor permission required")
                thresholds = getattr(
                    self.server,
                    "source_stale_after_seconds",
                    DEFAULT_SOURCE_STALE_SECONDS,
                )
                return self._json(
                    service.storage.source_health(stale_after_seconds=thresholds)
                )
            if path == "/api/admin/source-quarantine":
                if not identity.can_monitor_sources():
                    raise AuthorizationError("sources:monitor permission required")
                requested_status = query.get("status", ["OPEN"])[0].upper()
                status_filter = None if requested_status == "ALL" else requested_status
                limit = int(query.get("limit", ["50"])[0])
                return self._json(
                    {
                        "items": service.storage.list_source_quarantine(
                            status=status_filter,
                            limit=limit,
                        )
                    }
                )
            if path == "/api/meta":
                return self._json(service.repository.meta())
            if path == "/api/stats":
                return self._json(service.repository.dashboard_stats())
            if path == "/api/investigations":
                limit = int(query.get("limit", ["12"])[0])
                if not 1 <= limit <= 50:
                    raise ValueError("limit must be between 1 and 50")
                subject = None if identity.can_read_all_investigations() else identity.subject
                return self._json({"items": service.storage.list_investigations(limit, subject=subject)})
            path_parts = path.strip("/").split("/")
            if len(path_parts) == 3 and path_parts[:2] == ["api", "investigations"]:
                investigation_id = unquote(path_parts[2])
                subject = None if identity.can_read_all_investigations() else identity.subject
                item = service.storage.get_investigation(investigation_id, subject=subject)
                return self._json(item) if item else self._error(HTTPStatus.NOT_FOUND, "investigation not found")
            if path == "/api/cases":
                failure_code = query.get("failure_code", [None])[0]
                cases = service.repository.accessible_cases(identity.role, identity.line_ids, identity.station_ids)
                if failure_code:
                    cases = [item for item in cases if failure_code.upper() in item["failure_codes"]]
                return self._json({"items": cases[:50]})
            if path.startswith("/api/cases/"):
                case_id = unquote(path.rsplit("/", 1)[-1])
                item = service.repository.get_case(case_id, identity.role, identity.line_ids, identity.station_ids)
                return self._json(item) if item else self._error(HTTPStatus.NOT_FOUND, "case not found or not authorized")
            if path.startswith("/api/documents/"):
                version_id = unquote(path.rsplit("/", 1)[-1])
                item = service.repository.get_document(
                    version_id,
                    identity.role,
                    identity.line_ids,
                    identity.station_ids,
                )
                return self._json(item) if item else self._error(HTTPStatus.NOT_FOUND, "document not found or not authorized")
            if path == "/api/evaluations":
                return self._json({"items": service.repository.evaluations})
            return self._error(HTTPStatus.NOT_FOUND, "endpoint not found")
        except AuthenticationError as exc:
            return self._error(HTTPStatus.UNAUTHORIZED, str(exc), authenticate=True)
        except AuthorizationError as exc:
            return self._error(HTTPStatus.FORBIDDEN, str(exc))
        except (ValueError, TypeError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - safety boundary
            self.log_error("unhandled server error: %r", exc)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            self._enforce_origin()
            if parsed.path == "/api/dev/token":
                if not getattr(self.server, "dev_auth_enabled", False):
                    return self._error(HTTPStatus.NOT_FOUND, "endpoint not found")
                if not is_loopback_address(self.client_address[0]):
                    raise AuthorizationError("development authentication is loopback only")
                payload = self._read_json()
                unknown_fields = set(payload) - {"role"}
                if unknown_fields:
                    raise ValueError(f"unsupported request field: {sorted(unknown_fields)[0]}")
                role = str(payload.get("role") or "PRODUCT_ENGINEER").upper()
                if role not in ALLOWED_ROLES or role == "ADMIN":
                    raise ValueError("invalid role")
                permissions = (
                    (
                        "investigations:read:all",
                        "sources:monitor",
                        "sources:quarantine:manage",
                    )
                    if role == "QUALITY_ENGINEER"
                    else ()
                )
                authenticator = self._authenticator()
                if not isinstance(authenticator, TokenAuthenticator):
                    raise RuntimeError("development authenticator is not configured")
                token = authenticator.issue_token(
                    subject=f"local-demo:{role.lower()}",
                    role=role,
                    permissions=permissions,
                    ttl_seconds=3600,
                )
                return self._json(
                    {
                        "access_token": token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "identity": {"subject": f"local-demo:{role.lower()}", "role": role},
                        "development_only": True,
                    },
                    status=HTTPStatus.CREATED,
                )
            identity = self._require_identity()
            service = self._service()
            payload = self._read_json()
            action_parts = parsed.path.strip("/").split("/")
            if (
                len(action_parts) == 5
                and action_parts[:3] == ["api", "admin", "source-quarantine"]
                and action_parts[4] == "resolution"
            ):
                if not identity.can_manage_source_quarantine():
                    raise AuthorizationError("sources:quarantine:manage permission required")
                unknown_fields = set(payload) - {"resolution", "notes"}
                if unknown_fields:
                    raise ValueError(f"unsupported request field: {sorted(unknown_fields)[0]}")
                result = service.storage.resolve_source_quarantine(
                    unquote(action_parts[3]),
                    resolved_at=now_iso(),
                    resolved_by=identity.subject,
                    resolved_by_role=identity.role,
                    resolution=str(payload.get("resolution") or "").upper(),
                    notes=payload.get("notes") or "",
                )
                return self._json(result)
            if parsed.path == "/api/triage":
                return self._json(service.triage(payload, identity), status=HTTPStatus.CREATED)
            if len(action_parts) == 4 and action_parts[:2] == ["api", "investigations"] and action_parts[3] == "feedback":
                investigation_id = unquote(action_parts[2])
                subject = None if identity.can_read_all_investigations() else identity.subject
                if not service.storage.get_investigation(investigation_id, subject=subject):
                    return self._error(HTTPStatus.NOT_FOUND, "investigation not found")
                rating = str(payload.get("rating") or "").upper()
                if rating not in {"USEFUL", "PARTIALLY_USEFUL", "NOT_USEFUL", "RISKY"}:
                    raise ValueError("invalid feedback rating")
                comment = payload.get("comment") or ""
                if not isinstance(comment, str):
                    raise ValueError("feedback comment must be a string")
                if len(comment) > MAX_FEEDBACK_COMMENT_CHARS:
                    raise ValueError("feedback comment too long")
                feedback = service.storage.add_feedback(investigation_id, now_iso(), rating, comment)
                return self._json(feedback, status=HTTPStatus.CREATED)
            if len(action_parts) == 4 and action_parts[:2] == ["api", "investigations"] and action_parts[3] == "checks":
                investigation_id = unquote(action_parts[2])
                if not service.storage.get_investigation(investigation_id, subject=identity.subject):
                    return self._error(HTTPStatus.NOT_FOUND, "investigation not found")
                unknown_fields = set(payload) - {"step_sequence", "outcome", "notes", "evidence_ids"}
                if unknown_fields:
                    raise ValueError(f"unsupported request field: {sorted(unknown_fields)[0]}")
                check = service.storage.add_check_result(
                    investigation_id,
                    now_iso(),
                    actor_subject=identity.subject,
                    actor_role=identity.role,
                    step_sequence=payload.get("step_sequence"),
                    outcome=str(payload.get("outcome") or ""),
                    notes=payload.get("notes") or "",
                    evidence_ids=payload.get("evidence_ids") or [],
                )
                return self._json(check, status=HTTPStatus.CREATED)
            if len(action_parts) == 4 and action_parts[:2] == ["api", "investigations"] and action_parts[3] == "status":
                investigation_id = unquote(action_parts[2])
                unknown_fields = set(payload) - {"status"}
                if unknown_fields:
                    raise ValueError(f"unsupported request field: {sorted(unknown_fields)[0]}")
                target_status = str(payload.get("status") or "").upper()
                if target_status == "PUBLISHED":
                    if identity.role not in {"QUALITY_ENGINEER", "ADMIN"}:
                        raise AuthorizationError("quality review role required to publish")
                    subject = None if identity.can_read_all_investigations() or identity.role == "ADMIN" else identity.subject
                else:
                    if target_status not in {"INVESTIGATING", "CHECKED", "ROOT_CAUSE_REVIEW"}:
                        raise ValueError("unsupported owner status transition")
                    subject = identity.subject
                if not service.storage.get_investigation(investigation_id, subject=subject):
                    return self._error(HTTPStatus.NOT_FOUND, "investigation not found")
                transition = service.storage.transition_investigation(
                    investigation_id,
                    target_status,
                    now_iso(),
                    actor_subject=identity.subject,
                    actor_role=identity.role,
                )
                return self._json(transition)
            if len(action_parts) == 4 and action_parts[:2] == ["api", "investigations"] and action_parts[3] == "reviews":
                investigation_id = unquote(action_parts[2])
                if identity.role not in {"QUALITY_ENGINEER", "ADMIN"}:
                    raise AuthorizationError("quality review role required")
                subject = None if identity.can_read_all_investigations() or identity.role == "ADMIN" else identity.subject
                if not service.storage.get_investigation(investigation_id, subject=subject):
                    return self._error(HTTPStatus.NOT_FOUND, "investigation not found")
                unknown_fields = set(payload) - {"decision", "notes"}
                if unknown_fields:
                    raise ValueError(f"unsupported request field: {sorted(unknown_fields)[0]}")
                review = service.storage.add_review(
                    investigation_id,
                    now_iso(),
                    reviewer_subject=identity.subject,
                    reviewer_role=identity.role,
                    decision=str(payload.get("decision") or ""),
                    notes=payload.get("notes") or "",
                )
                return self._json(review, status=HTTPStatus.CREATED)
            return self._error(HTTPStatus.NOT_FOUND, "endpoint not found")
        except AuthenticationError as exc:
            return self._error(HTTPStatus.UNAUTHORIZED, str(exc), authenticate=True)
        except AuthorizationError as exc:
            return self._error(HTTPStatus.FORBIDDEN, str(exc))
        except json.JSONDecodeError:
            return self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
        except (ValueError, TypeError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - safety boundary
            self.log_error("unhandled server error: %r", exc)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    def _authenticator(self) -> TokenAuthenticator | OidcAuthenticator:
        authenticator = getattr(self.server, "authenticator", None)
        if not isinstance(authenticator, (TokenAuthenticator, OidcAuthenticator)):
            raise RuntimeError("server authenticator is not configured")
        return authenticator

    def _service(self) -> TriageService:
        service = getattr(self.server, "service", None)
        if not isinstance(service, TriageService):
            raise RuntimeError("server triage service is not configured")
        return service

    def _request_id(self) -> str:
        existing = getattr(self, "_normalized_request_id", None)
        if existing:
            return existing
        supplied = self.headers.get("X-Request-ID")
        request_id = supplied if supplied and REQUEST_ID.fullmatch(supplied) else f"REQ-{secrets.token_hex(12)}"
        self._normalized_request_id = request_id
        return request_id

    def _require_identity(self) -> Identity:
        return self._authenticator().authenticate(self.headers.get("Authorization"))

    def _allowed_origins(self) -> set[str]:
        return set(getattr(self.server, "allowed_origins", set()))

    def _enforce_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin not in self._allowed_origins():
            raise AuthorizationError("origin not allowed")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 2_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self._error(HTTPStatus.FORBIDDEN, "forbidden")
        if not target.exists() or not target.is_file():
            target = STATIC_DIR / "index.html"
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type.endswith("javascript") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_id())
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_id())
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str, *, authenticate: bool = False) -> None:
        headers = {"WWW-Authenticate": 'Bearer realm="yield-copilot"'} if authenticate else None
        error_codes = {
            HTTPStatus.BAD_REQUEST: "INVALID_REQUEST",
            HTTPStatus.UNAUTHORIZED: "AUTHENTICATION_REQUIRED",
            HTTPStatus.FORBIDDEN: "ACCESS_DENIED",
            HTTPStatus.NOT_FOUND: "NOT_FOUND",
            HTTPStatus.INTERNAL_SERVER_ERROR: "INTERNAL_ERROR",
        }
        return self._json(
            {
                "error": message,
                "error_code": error_codes.get(status, "REQUEST_FAILED"),
                "status": int(status),
                "request_id": self._request_id(),
            },
            status=status,
            extra_headers=headers,
        )

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self._allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Request-ID")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "X-Request-ID")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the synthetic SeaTrack-style Yield RCA Evidence Copilot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument(
        "--dev-auth",
        action="store_true",
        help="enable the local-only role token endpoint; never use this option in production",
    )
    args = parser.parse_args()
    validate_dev_auth_bind(args.host, args.dev_auth)
    try:
        authenticator, auth_mode = build_authenticator(os.environ, dev_auth=args.dev_auth)
        source_stale_after_seconds = parse_source_stale_thresholds(os.environ)
        answer_generator = build_answer_generator(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    configured_origins = {
        value.strip()
        for value in os.environ.get("RAG_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    allowed_origins = configured_origins or {
        f"http://{args.host}:{args.port}",
        f"http://localhost:{args.port}",
    }
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    server.authenticator = authenticator
    server.service = TriageService(ROOT, answer_generator=answer_generator)
    server.allowed_origins = allowed_origins
    server.dev_auth_enabled = args.dev_auth
    server.auth_mode = auth_mode
    server.source_stale_after_seconds = source_stale_after_seconds
    server.generation_mode = server.service.generation_mode
    print(f"SeaTrack-style Yield RCA Evidence Copilot running at http://{args.host}:{args.port}")
    print("Synthetic demo only — no Seagate internal data is included.")
    print(f"Authentication mode: {auth_mode}")
    print(f"Generation mode: {server.generation_mode}")
    if args.dev_auth:
        print("Development authentication is enabled. Do not expose this process outside localhost.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
