from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable


ALLOWED_ROLES = {
    "PRODUCT_ENGINEER",
    "PROCESS_ENGINEER",
    "QUALITY_ENGINEER",
    "FA_ENGINEER",
    "LINE_LEAD",
    "ADMIN",
}


class AuthenticationError(ValueError):
    """Raised when an identity envelope is missing, malformed, or untrusted."""


class AuthorizationError(PermissionError):
    """Raised when an authenticated identity is outside its allowed data scope."""


@dataclass(frozen=True)
class Identity:
    subject: str
    role: str
    line_ids: tuple[str, ...] = ()
    station_ids: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    auth_method: str = "signed-gateway-envelope"

    def can_read_all_investigations(self) -> bool:
        return "investigations:read:all" in self.permissions


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:  # pragma: no cover - implementation-dependent decoder errors
        raise AuthenticationError("invalid bearer token encoding") from exc


def _compact_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _string_tuple(value: Any, claim: str, *, maximum: int = 64) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise AuthenticationError(f"invalid {claim} claim")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 128:
            raise AuthenticationError(f"invalid {claim} claim")
        if item not in output:
            output.append(item)
    return tuple(output)


class TokenAuthenticator:
    """Validate short-lived identity envelopes signed by a trusted gateway.

    This standard-library implementation is for the local PoC boundary. A Seagate
    deployment should place its approved OIDC/SSO gateway in front of the service
    and mint the same normalized claims only after enterprise token validation.
    """

    issuer = "seagate-rag-identity-gateway"

    def __init__(self, secret: str | bytes, *, clock_skew_seconds: int = 30):
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret_bytes) < 32:
            raise ValueError("RAG_AUTH_SECRET must contain at least 32 bytes")
        self._secret = secret_bytes
        self.clock_skew_seconds = clock_skew_seconds

    def issue_token(
        self,
        *,
        subject: str,
        role: str,
        line_ids: Iterable[str] = (),
        station_ids: Iterable[str] = (),
        permissions: Iterable[str] = (),
        ttl_seconds: int = 3600,
        now: int | None = None,
    ) -> str:
        role = role.upper()
        if role not in ALLOWED_ROLES:
            raise ValueError("invalid role")
        if not subject or len(subject) > 128:
            raise ValueError("invalid subject")
        issued_at = int(time.time() if now is None else now)
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": subject,
            "role": role,
            "line_ids": list(line_ids),
            "station_ids": list(station_ids),
            "permissions": list(permissions),
            "iss": self.issuer,
            "iat": issued_at,
            "exp": issued_at + ttl_seconds,
        }
        encoded_header = _b64url_encode(_compact_json(header))
        encoded_payload = _b64url_encode(_compact_json(payload))
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = _b64url_encode(hmac.new(self._secret, signing_input, hashlib.sha256).digest())
        return f"{encoded_header}.{encoded_payload}.{signature}"

    def authenticate(self, authorization_header: str | None, *, now: int | None = None) -> Identity:
        if not authorization_header:
            raise AuthenticationError("authentication required")
        scheme, separator, token = authorization_header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            raise AuthenticationError("expected Authorization: Bearer <token>")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationError("invalid bearer token")
        encoded_header, encoded_payload, encoded_signature = parts
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        supplied = _b64url_decode(encoded_signature)
        if not hmac.compare_digest(expected, supplied):
            raise AuthenticationError("invalid bearer token signature")
        try:
            header = json.loads(_b64url_decode(encoded_header))
            claims = json.loads(_b64url_decode(encoded_payload))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise AuthenticationError("invalid bearer token payload") from exc
        if header != {"alg": "HS256", "typ": "JWT"} or not isinstance(claims, dict):
            raise AuthenticationError("unsupported bearer token")
        if claims.get("iss") != self.issuer:
            raise AuthenticationError("invalid token issuer")
        current = int(time.time() if now is None else now)
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if not isinstance(issued_at, int) or not isinstance(expires_at, int):
            raise AuthenticationError("invalid token lifetime")
        if issued_at > current + self.clock_skew_seconds:
            raise AuthenticationError("token is not yet valid")
        if expires_at <= current - self.clock_skew_seconds:
            raise AuthenticationError("token expired")
        subject = claims.get("sub")
        role = claims.get("role")
        if not isinstance(subject, str) or not subject or len(subject) > 128:
            raise AuthenticationError("invalid subject claim")
        if not isinstance(role, str) or role not in ALLOWED_ROLES:
            raise AuthenticationError("invalid role claim")
        return Identity(
            subject=subject,
            role=role,
            line_ids=_string_tuple(claims.get("line_ids"), "line_ids"),
            station_ids=_string_tuple(claims.get("station_ids"), "station_ids"),
            permissions=_string_tuple(claims.get("permissions"), "permissions"),
        )
