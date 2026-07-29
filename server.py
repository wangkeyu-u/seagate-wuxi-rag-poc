#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from rag_app import TriageService
from rag_app.auth import ALLOWED_ROLES, AuthenticationError, AuthorizationError, Identity, TokenAuthenticator


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
SERVICE = TriageService(ROOT)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "YieldCopilot/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        if not origin or origin not in self._allowed_origins():
            return self._error(HTTPStatus.FORBIDDEN, "origin not allowed")
        self.send_response(HTTPStatus.NO_CONTENT)
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
                        "authentication": "required-for-business-apis",
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
            if path == "/api/meta":
                return self._json(SERVICE.repository.meta())
            if path == "/api/stats":
                return self._json(SERVICE.repository.dashboard_stats())
            if path == "/api/investigations":
                limit = int(query.get("limit", ["12"])[0])
                if not 1 <= limit <= 50:
                    raise ValueError("limit must be between 1 and 50")
                subject = None if identity.can_read_all_investigations() else identity.subject
                return self._json({"items": SERVICE.storage.list_investigations(limit, subject=subject)})
            if path.startswith("/api/investigations/"):
                investigation_id = unquote(path.rsplit("/", 1)[-1])
                subject = None if identity.can_read_all_investigations() else identity.subject
                item = SERVICE.storage.get_investigation(investigation_id, subject=subject)
                return self._json(item) if item else self._error(HTTPStatus.NOT_FOUND, "investigation not found")
            if path == "/api/cases":
                failure_code = query.get("failure_code", [None])[0]
                cases = SERVICE.repository.accessible_cases(identity.role, identity.line_ids, identity.station_ids)
                if failure_code:
                    cases = [item for item in cases if failure_code.upper() in item["failure_codes"]]
                return self._json({"items": cases[:50]})
            if path.startswith("/api/cases/"):
                case_id = unquote(path.rsplit("/", 1)[-1])
                item = SERVICE.repository.get_case(case_id, identity.role, identity.line_ids, identity.station_ids)
                return self._json(item) if item else self._error(HTTPStatus.NOT_FOUND, "case not found or not authorized")
            if path.startswith("/api/documents/"):
                version_id = unquote(path.rsplit("/", 1)[-1])
                item = SERVICE.repository.get_document(version_id, identity.role)
                return self._json(item) if item else self._error(HTTPStatus.NOT_FOUND, "document not found or not authorized")
            if path == "/api/evaluations":
                return self._json({"items": SERVICE.repository.evaluations})
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
                payload = self._read_json()
                unknown_fields = set(payload) - {"role"}
                if unknown_fields:
                    raise ValueError(f"unsupported request field: {sorted(unknown_fields)[0]}")
                role = str(payload.get("role") or "PRODUCT_ENGINEER").upper()
                if role not in ALLOWED_ROLES or role == "ADMIN":
                    raise ValueError("invalid role")
                permissions = ("investigations:read:all",) if role == "QUALITY_ENGINEER" else ()
                token = self._authenticator().issue_token(
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
            payload = self._read_json()
            if parsed.path == "/api/triage":
                return self._json(SERVICE.triage(payload, identity), status=HTTPStatus.CREATED)
            if parsed.path.startswith("/api/investigations/") and parsed.path.endswith("/feedback"):
                parts = parsed.path.strip("/").split("/")
                investigation_id = unquote(parts[2])
                subject = None if identity.can_read_all_investigations() else identity.subject
                if not SERVICE.storage.get_investigation(investigation_id, subject=subject):
                    return self._error(HTTPStatus.NOT_FOUND, "investigation not found")
                rating = str(payload.get("rating") or "").upper()
                if rating not in {"USEFUL", "PARTIALLY_USEFUL", "NOT_USEFUL", "RISKY"}:
                    raise ValueError("invalid feedback rating")
                from datetime import datetime, timedelta, timezone

                created_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
                feedback = SERVICE.storage.add_feedback(investigation_id, created_at, rating, str(payload.get("comment") or ""))
                return self._json(feedback, status=HTTPStatus.CREATED)
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

    def _authenticator(self) -> TokenAuthenticator:
        authenticator = getattr(self.server, "authenticator", None)
        if not isinstance(authenticator, TokenAuthenticator):
            raise RuntimeError("server authenticator is not configured")
        return authenticator

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
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str, *, authenticate: bool = False) -> None:
        headers = {"WWW-Authenticate": 'Bearer realm="yield-copilot"'} if authenticate else None
        return self._json({"error": message, "status": int(status)}, status=status, extra_headers=headers)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self._allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


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
    configured_secret = os.environ.get("RAG_AUTH_SECRET")
    if not configured_secret and not args.dev_auth:
        parser.error("RAG_AUTH_SECRET is required unless --dev-auth is explicitly enabled")
    auth_secret = configured_secret or secrets.token_urlsafe(48)
    authenticator = TokenAuthenticator(auth_secret)
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
    server.allowed_origins = allowed_origins
    server.dev_auth_enabled = args.dev_auth
    print(f"SeaTrack-style Yield RCA Evidence Copilot running at http://{args.host}:{args.port}")
    print("Synthetic demo only — no Seagate internal data is included.")
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
