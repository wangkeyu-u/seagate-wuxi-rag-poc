"""Strict OIDC/JWKS verification without hidden network or identity fallback.

This standard-library implementation keeps the PoC dependency-free and makes
every trust decision reviewable. A real deployment should still use the JOSE
library and identity configuration approved by the enterprise security team.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .auth import ALLOWED_ROLES, AuthenticationError, Identity


MAX_TOKEN_CHARS = 16_384
MAX_JWKS_BYTES = 1_000_000
MAX_JWKS_KEYS = 64
CLAIM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
RSA_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
RSA_PRIVATE_PARAMETERS = {"d", "p", "q", "dp", "dq", "qi", "oth"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise AuthenticationError(f"duplicate JSON member: {key}")
        output[key] = value
    return output


def _b64url_decode(value: Any, label: str, *, maximum_bytes: int = MAX_JWKS_BYTES) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum_bytes * 2:
        raise AuthenticationError(f"invalid {label} encoding")
    if not BASE64URL.fullmatch(value):
        raise AuthenticationError(f"invalid {label} encoding")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise AuthenticationError(f"invalid {label} encoding") from exc
    if len(decoded) > maximum_bytes:
        raise AuthenticationError(f"invalid {label} encoding")
    return decoded


def _decode_json_segment(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = _b64url_decode(value, label, maximum_bytes=64_000)
        parsed = json.loads(decoded.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError(f"invalid {label} JSON") from exc
    if not isinstance(parsed, dict):
        raise AuthenticationError(f"invalid {label} JSON")
    return parsed


def _claim_array(
    value: Any,
    claim: str,
    *,
    maximum: int = 64,
    item_maximum: int = 128,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise AuthenticationError(f"invalid {claim} claim")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > item_maximum or "\x00" in item:
            raise AuthenticationError(f"invalid {claim} claim")
        if item not in output:
            output.append(item)
    return tuple(output)


def _numeric_date(value: Any, claim: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthenticationError(f"invalid {claim} claim")
    return value


def _validate_https_url(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise ValueError(f"{label} must be a bounded HTTPS URL")
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a bounded HTTPS URL without credentials or fragment")


def _environment_integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    audience: str
    jwks_url: str
    group_role_mapping: Mapping[str, str]
    groups_claim: str = "groups"
    lines_claim: str = "line_ids"
    stations_claim: str = "station_ids"
    permissions_claim: str = "permissions"
    permission_allowlist: tuple[str, ...] = ()
    clock_skew_seconds: int = 30
    max_token_lifetime_seconds: int = 3_600
    jwks_cache_seconds: int = 300
    jwks_timeout_seconds: int = 5
    accepted_types: tuple[str, ...] = ("JWT", "at+jwt")

    def __post_init__(self) -> None:
        _validate_https_url(self.issuer, "OIDC issuer")
        issuer_parts = urlparse(self.issuer)
        if issuer_parts.query:
            raise ValueError("OIDC issuer must not contain a query")
        _validate_https_url(self.jwks_url, "OIDC JWKS URL")
        if not isinstance(self.audience, str) or not self.audience or len(self.audience) > 512:
            raise ValueError("OIDC audience must be a bounded non-empty string")
        mapping = dict(self.group_role_mapping)
        if not mapping or len(mapping) > 256:
            raise ValueError("OIDC group-to-role mapping must contain between 1 and 256 entries")
        for group, role in mapping.items():
            if not isinstance(group, str) or not group or len(group) > 256 or "\x00" in group:
                raise ValueError("OIDC group-to-role mapping contains an invalid group")
            if role not in ALLOWED_ROLES:
                raise ValueError("OIDC group-to-role mapping contains an unsupported role")
        object.__setattr__(self, "group_role_mapping", mapping)
        for claim in (
            self.groups_claim,
            self.lines_claim,
            self.stations_claim,
            self.permissions_claim,
        ):
            if not isinstance(claim, str) or not CLAIM_NAME.fullmatch(claim):
                raise ValueError("OIDC claim names must be safe top-level JSON member names")
        if not 0 <= self.clock_skew_seconds <= 300:
            raise ValueError("OIDC clock skew must be between 0 and 300 seconds")
        if not 60 <= self.max_token_lifetime_seconds <= 86_400:
            raise ValueError("OIDC maximum token lifetime must be between 60 and 86400 seconds")
        if not 30 <= self.jwks_cache_seconds <= 86_400:
            raise ValueError("OIDC JWKS cache duration must be between 30 and 86400 seconds")
        if not 1 <= self.jwks_timeout_seconds <= 30:
            raise ValueError("OIDC JWKS timeout must be between 1 and 30 seconds")
        if not self.accepted_types or any(
            not isinstance(item, str) or not item or len(item) > 64 for item in self.accepted_types
        ):
            raise ValueError("OIDC accepted token types are invalid")
        permissions = tuple(dict.fromkeys(self.permission_allowlist))
        if len(permissions) > 128 or any(
            not isinstance(item, str) or not item or len(item) > 128 for item in permissions
        ):
            raise ValueError("OIDC permission allowlist is invalid")
        object.__setattr__(self, "permission_allowlist", permissions)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> OidcConfig:
        required = {
            "RAG_OIDC_ISSUER": environment.get("RAG_OIDC_ISSUER"),
            "RAG_OIDC_AUDIENCE": environment.get("RAG_OIDC_AUDIENCE"),
            "RAG_OIDC_JWKS_URL": environment.get("RAG_OIDC_JWKS_URL"),
            "RAG_OIDC_GROUP_ROLE_MAP": environment.get("RAG_OIDC_GROUP_ROLE_MAP"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing OIDC configuration: {', '.join(sorted(missing))}")
        try:
            role_mapping = json.loads(required["RAG_OIDC_GROUP_ROLE_MAP"], object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, AuthenticationError) as exc:
            raise ValueError("RAG_OIDC_GROUP_ROLE_MAP must be a JSON object with unique keys") from exc
        if not isinstance(role_mapping, dict):
            raise ValueError("RAG_OIDC_GROUP_ROLE_MAP must be a JSON object")
        permission_allowlist = tuple(
            value.strip()
            for value in environment.get("RAG_OIDC_PERMISSION_ALLOWLIST", "").split(",")
            if value.strip()
        )
        return cls(
            issuer=required["RAG_OIDC_ISSUER"],
            audience=required["RAG_OIDC_AUDIENCE"],
            jwks_url=required["RAG_OIDC_JWKS_URL"],
            group_role_mapping=role_mapping,
            groups_claim=environment.get("RAG_OIDC_GROUPS_CLAIM", "groups"),
            lines_claim=environment.get("RAG_OIDC_LINES_CLAIM", "line_ids"),
            stations_claim=environment.get("RAG_OIDC_STATIONS_CLAIM", "station_ids"),
            permissions_claim=environment.get("RAG_OIDC_PERMISSIONS_CLAIM", "permissions"),
            permission_allowlist=permission_allowlist,
            clock_skew_seconds=_environment_integer(
                environment,
                "RAG_OIDC_CLOCK_SKEW_SECONDS",
                30,
                minimum=0,
                maximum=300,
            ),
            max_token_lifetime_seconds=_environment_integer(
                environment,
                "RAG_OIDC_MAX_TOKEN_LIFETIME_SECONDS",
                3_600,
                minimum=60,
                maximum=86_400,
            ),
            jwks_cache_seconds=_environment_integer(
                environment,
                "RAG_OIDC_JWKS_CACHE_SECONDS",
                300,
                minimum=30,
                maximum=86_400,
            ),
            jwks_timeout_seconds=_environment_integer(
                environment,
                "RAG_OIDC_JWKS_TIMEOUT_SECONDS",
                5,
                minimum=1,
                maximum=30,
            ),
        )


@dataclass(frozen=True)
class RsaPublicKey:
    kid: str
    modulus: int
    exponent: int
    size_bytes: int


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urllib.error.HTTPError(req.full_url, code, "JWKS redirects are disabled", headers, fp)


def _fetch_jwks(url: str, timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "SeaTrack-RAG-OIDC/0.5"},
        method="GET",
    )
    opener = urllib.request.build_opener(_RejectRedirects)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_JWKS_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise AuthenticationError("OIDC signing keys are unavailable") from exc
    if len(raw) > MAX_JWKS_BYTES:
        raise AuthenticationError("OIDC JWKS response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("OIDC JWKS response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("OIDC JWKS response must be a JSON object")
    return payload


class JwksCache:
    def __init__(
        self,
        url: str,
        *,
        cache_seconds: int,
        timeout_seconds: int,
        fetcher: Callable[[str, int], dict[str, Any]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.url = url
        self.cache_seconds = cache_seconds
        self.timeout_seconds = timeout_seconds
        self._fetcher = fetcher or _fetch_jwks
        self._monotonic = monotonic
        self._keys: dict[str, RsaPublicKey] = {}
        self._expires_at = 0.0
        self._next_miss_refresh_at = 0.0
        self._lock = threading.Lock()

    def get(self, kid: str) -> RsaPublicKey:
        with self._lock:
            now = self._monotonic()
            refreshed = False
            if now >= self._expires_at:
                self._refresh(now)
                refreshed = True
            key = self._keys.get(kid)
            if key is None and not refreshed and now >= self._next_miss_refresh_at:
                self._refresh(now)
                self._next_miss_refresh_at = now + min(30, self.cache_seconds)
                key = self._keys.get(kid)
            if key is None:
                raise AuthenticationError("unknown OIDC signing key")
            return key

    def _refresh(self, now: float) -> None:
        payload = self._fetcher(self.url, self.timeout_seconds)
        self._keys = self._parse(payload)
        self._expires_at = now + self.cache_seconds

    @staticmethod
    def _parse(payload: Any) -> dict[str, RsaPublicKey]:
        if not isinstance(payload, dict) or "keys" not in payload:
            raise AuthenticationError("OIDC JWKS must contain a keys array")
        raw_keys = payload["keys"]
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= MAX_JWKS_KEYS:
            raise AuthenticationError("OIDC JWKS keys array is invalid")
        output: dict[str, RsaPublicKey] = {}
        seen_kids: set[str] = set()
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                raise AuthenticationError("OIDC JWK must be a JSON object")
            if RSA_PRIVATE_PARAMETERS.intersection(raw_key):
                raise AuthenticationError("OIDC JWKS must not contain private RSA parameters")
            kid = raw_key.get("kid")
            if kid is None:
                continue
            if not isinstance(kid, str) or not kid or len(kid) > 128 or "\x00" in kid:
                raise AuthenticationError("OIDC JWK contains an invalid kid")
            if kid in seen_kids:
                raise AuthenticationError("OIDC JWKS contains duplicate kid values")
            seen_kids.add(kid)
            if raw_key.get("kty") != "RSA":
                continue
            if raw_key.get("alg") != "RS256":
                continue
            if raw_key.get("use") not in {None, "sig"}:
                continue
            key_ops = raw_key.get("key_ops")
            if key_ops is not None and key_ops != ["verify"]:
                continue
            modulus_bytes = _b64url_decode(raw_key.get("n"), "RSA modulus", maximum_bytes=1_024)
            exponent_bytes = _b64url_decode(raw_key.get("e"), "RSA exponent", maximum_bytes=8)
            if modulus_bytes[0] == 0 or exponent_bytes[0] == 0:
                raise AuthenticationError("OIDC RSA integers must use minimal unsigned encoding")
            modulus = int.from_bytes(modulus_bytes, "big")
            exponent = int.from_bytes(exponent_bytes, "big")
            if not 2_048 <= modulus.bit_length() <= 8_192:
                raise AuthenticationError("OIDC RSA key size must be between 2048 and 8192 bits")
            if exponent < 3 or exponent > 0xFFFFFFFF or exponent % 2 == 0:
                raise AuthenticationError("OIDC RSA public exponent is invalid")
            output[kid] = RsaPublicKey(
                kid=kid,
                modulus=modulus,
                exponent=exponent,
                size_bytes=(modulus.bit_length() + 7) // 8,
            )
        if not output:
            raise AuthenticationError("OIDC JWKS contains no usable RS256 signing keys")
        return output


def _verify_rs256(signing_input: bytes, signature: bytes, key: RsaPublicKey) -> bool:
    if len(signature) != key.size_bytes:
        return False
    signature_value = int.from_bytes(signature, "big")
    if signature_value >= key.modulus:
        return False
    encoded = pow(signature_value, key.exponent, key.modulus).to_bytes(key.size_bytes, "big")
    digest_info = RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding_length = key.size_bytes - len(digest_info) - 3
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def parse_rs256_jwks(payload: Any) -> dict[str, RsaPublicKey]:
    """Parse a strict public-only RS256 trust set for other local trust boundaries."""

    return JwksCache._parse(payload)


def verify_rs256_signature(signing_input: bytes, signature: bytes, key: RsaPublicKey) -> bool:
    """Verify an RSASSA-PKCS1-v1_5 SHA-256 signature with a parsed public key."""

    return _verify_rs256(signing_input, signature, key)


@dataclass
class OidcAuthenticator:
    config: OidcConfig
    jwks: JwksCache | None = field(default=None)

    def __post_init__(self) -> None:
        if self.jwks is None:
            self.jwks = JwksCache(
                self.config.jwks_url,
                cache_seconds=self.config.jwks_cache_seconds,
                timeout_seconds=self.config.jwks_timeout_seconds,
            )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> OidcAuthenticator:
        return cls(OidcConfig.from_environment(environment))

    def authenticate(self, authorization_header: str | None, *, now: int | None = None) -> Identity:
        if not authorization_header:
            raise AuthenticationError("authentication required")
        scheme, separator, token = authorization_header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            raise AuthenticationError("expected Authorization: Bearer <token>")
        if len(token) > MAX_TOKEN_CHARS:
            raise AuthenticationError("bearer token is too large")
        parts = token.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise AuthenticationError("invalid bearer token")
        encoded_header, encoded_payload, encoded_signature = parts
        header = _decode_json_segment(encoded_header, "JWT header")
        claims = _decode_json_segment(encoded_payload, "JWT claims")
        if set(header) - {"alg", "kid", "typ"}:
            raise AuthenticationError("unsupported JWT header parameter")
        if header.get("alg") != "RS256":
            raise AuthenticationError("unsupported JWT signing algorithm")
        token_type = header.get("typ")
        if token_type is not None and token_type not in self.config.accepted_types:
            raise AuthenticationError("unsupported JWT type")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 128 or "\x00" in kid:
            raise AuthenticationError("JWT kid is required")
        signature = _b64url_decode(encoded_signature, "JWT signature", maximum_bytes=1_024)
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        key = self.jwks.get(kid)
        if not _verify_rs256(signing_input, signature, key):
            raise AuthenticationError("invalid bearer token signature")
        return self._identity_from_claims(claims, int(time.time() if now is None else now))

    def _identity_from_claims(self, claims: dict[str, Any], current: int) -> Identity:
        if claims.get("iss") != self.config.issuer:
            raise AuthenticationError("invalid token issuer")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 256 or "\x00" in subject:
            raise AuthenticationError("invalid subject claim")
        audience_claim = claims.get("aud")
        if isinstance(audience_claim, str):
            audiences = (audience_claim,)
        elif isinstance(audience_claim, list):
            audiences = _claim_array(audience_claim, "aud", maximum=16, item_maximum=512)
        else:
            raise AuthenticationError("invalid aud claim")
        if self.config.audience not in audiences:
            raise AuthenticationError("invalid token audience")
        authorized_party = claims.get("azp")
        if len(audiences) > 1 and authorized_party is None:
            raise AuthenticationError("multi-audience token requires azp")
        if authorized_party is not None and authorized_party != self.config.audience:
            raise AuthenticationError("invalid azp claim")
        expires_at = _numeric_date(claims.get("exp"), "exp")
        issued_at = _numeric_date(claims.get("iat"), "iat")
        not_before = _numeric_date(claims.get("nbf"), "nbf") if "nbf" in claims else issued_at
        skew = self.config.clock_skew_seconds
        if issued_at > current + skew or not_before > current + skew:
            raise AuthenticationError("token is not yet valid")
        if expires_at <= current - skew:
            raise AuthenticationError("token expired")
        if expires_at <= issued_at:
            raise AuthenticationError("invalid token lifetime")
        if expires_at - issued_at > self.config.max_token_lifetime_seconds:
            raise AuthenticationError("token lifetime exceeds configured maximum")
        groups = _claim_array(
            claims.get(self.config.groups_claim),
            self.config.groups_claim,
            maximum=256,
            item_maximum=256,
        )
        mapped_roles = {
            self.config.group_role_mapping[group]
            for group in groups
            if group in self.config.group_role_mapping
        }
        if len(mapped_roles) != 1:
            raise AuthenticationError("identity must map to exactly one application role")
        supplied_permissions = _claim_array(
            claims.get(self.config.permissions_claim),
            self.config.permissions_claim,
            maximum=128,
        )
        permission_allowlist = set(self.config.permission_allowlist)
        permissions = tuple(
            permission for permission in supplied_permissions if permission in permission_allowlist
        )
        return Identity(
            subject=subject,
            role=next(iter(mapped_roles)),
            line_ids=_claim_array(claims.get(self.config.lines_claim), self.config.lines_claim),
            station_ids=_claim_array(claims.get(self.config.stations_claim), self.config.stations_claim),
            permissions=permissions,
            auth_method="oidc-rs256",
        )
