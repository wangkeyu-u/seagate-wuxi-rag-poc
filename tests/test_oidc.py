from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import unittest
from http.client import HTTPConnection

from rag_app.auth import AuthenticationError, TokenAuthenticator
from rag_app.oidc import (
    JwksCache,
    OidcAuthenticator,
    OidcConfig,
    RSA_SHA256_DIGEST_INFO_PREFIX,
)
import server as app_server
from server import build_authenticator


# Public example RSA material from RFC 7517 Appendix C. The private exponent is
# used only to construct deterministic test tokens and is not an application key.
RSA_N = (
    "t6Q8PWSi1dkJj9hTP8hNYFlvadM7DflW9mWepOJhJ66w7nyoK1gPNqFMSQRy"
    "O125Gp-TEkodhWr0iujjHVx7BcV0llS4w5ACGgPrcAd6ZcSR0-Iqom-QFcNP"
    "8Sjg086MwoqQU_LYywlAGZ21WSdS_PERyGFiNnj3QQlO8Yns5jCtLCRwLHL0"
    "Pb1fEv45AuRIuUfVcPySBWYnDyGxvjYGDSM-AqWS9zIQ2ZilgT-GqUmipg0X"
    "OC0Cc20rgLe2ymLHjpHciCKVAbY5-L32-lSeZO-Os6U15_aXrk9Gw8cPUaX1"
    "_I8sLGuSiVdt3C_Fn2PZ3Z8i744FPFGGcG1qs2Wz-Q"
)
RSA_E = "AQAB"
RSA_D = (
    "GRtbIQmhOZtyszfgKdg4u_N-R_mZGU_9k7JQ_jn1DnfTuMdSNprTeaSTyWfS"
    "NkuaAwnOEbIQVy1IQbWVV25NY3ybc_IhUJtfri7bAXYEReWaCl3hdlPKXy9U"
    "vqPYGR0kIXTQRqns-dVJ7jahlI7LyckrpTmrM8dWBo4_PMaenNnPiQgO0xnu"
    "ToxutRZJfJvG4Ox4ka3GORQd9CsCZ2vsUDmsXOfUENOyMqADC6p1M3h33tsu"
    "rY15k9qMSpG9OX_IJAXmxzAh_tWiZOwk2K4yxH9tS3Lq1yX8C1EWmeRDkK2a"
    "hecG85-oLKQt5VEpWHKmjOi_gJSdSgqcN96X52esAQ"
)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64int(value: str) -> int:
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


def rsa_sign(signing_input: bytes) -> bytes:
    modulus = b64int(RSA_N)
    private_exponent = b64int(RSA_D)
    size_bytes = (modulus.bit_length() + 7) // 8
    digest_info = RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding = b"\xff" * (size_bytes - len(digest_info) - 3)
    encoded = b"\x00\x01" + padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(size_bytes, "big")


def issue_token(claims, *, kid="rfc-test-key", alg="RS256", typ="JWT", extra_header=None):
    header = {"alg": alg, "kid": kid, "typ": typ}
    header.update(extra_header or {})
    encoded_header = b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_claims = b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    return f"{encoded_header}.{encoded_claims}.{b64url(rsa_sign(signing_input))}"


def public_jwk(kid="rfc-test-key"):
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": RSA_N,
        "e": RSA_E,
    }


class OidcAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.config = OidcConfig(
            issuer="https://idp.example.test/tenant/v2.0",
            audience="api://yield-copilot",
            jwks_url="https://idp.example.test/tenant/discovery/keys",
            group_role_mapping={
                "group-pe": "PRODUCT_ENGINEER",
                "group-qa": "QUALITY_ENGINEER",
            },
            permission_allowlist=("investigations:read:all",),
            clock_skew_seconds=0,
        )
        self.jwks_payload = {"keys": [public_jwk()]}
        self.authenticator = OidcAuthenticator(
            self.config,
            JwksCache(
                self.config.jwks_url,
                cache_seconds=300,
                timeout_seconds=5,
                fetcher=lambda _url, _timeout: self.jwks_payload,
            ),
        )

    def claims(self, **updates):
        claims = {
            "iss": self.config.issuer,
            "sub": "enterprise-user-42",
            "aud": self.config.audience,
            "iat": 1_000,
            "exp": 1_300,
            "groups": ["group-pe", "unmapped-enterprise-group"],
            "line_ids": ["LINE-02"],
            "station_ids": ["ST-04"],
            "permissions": ["investigations:read:all", "unapproved:admin"],
        }
        claims.update(updates)
        return claims

    def test_valid_rs256_token_maps_group_scope_and_allowlisted_permission(self):
        identity = self.authenticator.authenticate(
            f"Bearer {issue_token(self.claims())}",
            now=1_100,
        )
        self.assertEqual(identity.subject, "enterprise-user-42")
        self.assertEqual(identity.role, "PRODUCT_ENGINEER")
        self.assertEqual(identity.line_ids, ("LINE-02",))
        self.assertEqual(identity.station_ids, ("ST-04",))
        self.assertEqual(identity.permissions, ("investigations:read:all",))
        self.assertEqual(identity.auth_method, "oidc-rs256")

    def test_algorithm_confusion_and_untrusted_header_parameters_are_rejected(self):
        with self.assertRaisesRegex(AuthenticationError, "algorithm"):
            self.authenticator.authenticate(
                f"Bearer {issue_token(self.claims(), alg='HS256')}",
                now=1_100,
            )
        with self.assertRaisesRegex(AuthenticationError, "header parameter"):
            self.authenticator.authenticate(
                f"Bearer {issue_token(self.claims(), extra_header={'jku': 'https://attacker.invalid/jwks'})}",
                now=1_100,
            )
        valid = issue_token(self.claims())
        encoded_header, encoded_claims, encoded_signature = valid.split(".")
        replacement = "A" if encoded_signature[0] != "A" else "B"
        tampered = f"{encoded_header}.{encoded_claims}.{replacement}{encoded_signature[1:]}"
        with self.assertRaisesRegex(AuthenticationError, "signature"):
            self.authenticator.authenticate(f"Bearer {tampered}", now=1_100)

        duplicate_header = b64url(
            b'{"alg":"RS256","kid":"rfc-test-key","kid":"other","typ":"JWT"}'
        )
        signing_input = f"{duplicate_header}.{encoded_claims}".encode("ascii")
        duplicate_token = f"{duplicate_header}.{encoded_claims}.{b64url(rsa_sign(signing_input))}"
        with self.assertRaisesRegex(AuthenticationError, "duplicate JSON"):
            self.authenticator.authenticate(f"Bearer {duplicate_token}", now=1_100)

    def test_issuer_audience_expiry_and_lifetime_are_enforced(self):
        invalid_claims = (
            (self.claims(iss="https://attacker.invalid"), "issuer"),
            (self.claims(aud="api://another-service"), "audience"),
            (self.claims(exp=1_099), "expired"),
            (self.claims(exp=5_000), "lifetime"),
        )
        for claims, message in invalid_claims:
            with self.subTest(message=message), self.assertRaisesRegex(AuthenticationError, message):
                self.authenticator.authenticate(f"Bearer {issue_token(claims)}", now=1_100)

    def test_multiple_audiences_require_matching_authorized_party(self):
        claims = self.claims(aud=[self.config.audience, "api://other"])
        with self.assertRaisesRegex(AuthenticationError, "requires azp"):
            self.authenticator.authenticate(f"Bearer {issue_token(claims)}", now=1_100)
        claims["azp"] = self.config.audience
        identity = self.authenticator.authenticate(f"Bearer {issue_token(claims)}", now=1_100)
        self.assertEqual(identity.role, "PRODUCT_ENGINEER")

    def test_zero_or_multiple_mapped_roles_fail_closed(self):
        for groups in (["unmapped"], ["group-pe", "group-qa"]):
            with self.subTest(groups=groups), self.assertRaisesRegex(AuthenticationError, "exactly one"):
                self.authenticator.authenticate(
                    f"Bearer {issue_token(self.claims(groups=groups))}",
                    now=1_100,
                )

    def test_scope_claims_must_be_arrays(self):
        with self.assertRaisesRegex(AuthenticationError, "line_ids"):
            self.authenticator.authenticate(
                f"Bearer {issue_token(self.claims(line_ids='LINE-02'))}",
                now=1_100,
            )

    def test_unknown_kid_forces_one_refresh_and_supports_rotation(self):
        calls = []

        def rotating_fetcher(_url, _timeout):
            calls.append(len(calls) + 1)
            keys = [public_jwk()]
            if len(calls) >= 2:
                keys.append(public_jwk("rotated-key"))
            return {"keys": keys}

        authenticator = OidcAuthenticator(
            self.config,
            JwksCache(
                self.config.jwks_url,
                cache_seconds=300,
                timeout_seconds=5,
                fetcher=rotating_fetcher,
            ),
        )
        authenticator.authenticate(f"Bearer {issue_token(self.claims())}", now=1_100)
        identity = authenticator.authenticate(
            f"Bearer {issue_token(self.claims(), kid='rotated-key')}",
            now=1_100,
        )
        self.assertEqual(identity.subject, "enterprise-user-42")
        self.assertEqual(len(calls), 2)
        for _attempt in range(2):
            with self.assertRaisesRegex(AuthenticationError, "unknown"):
                authenticator.authenticate(
                    f"Bearer {issue_token(self.claims(), kid='not-published')}",
                    now=1_100,
                )
        self.assertEqual(len(calls), 2)

    def test_private_or_duplicate_jwks_keys_are_rejected(self):
        private_key = public_jwk()
        private_key["d"] = RSA_D
        for payload, message in (
            ({"keys": [private_key]}, "private"),
            ({"keys": [public_jwk(), public_jwk()]}, "duplicate"),
        ):
            cache = JwksCache(
                self.config.jwks_url,
                cache_seconds=300,
                timeout_seconds=5,
                fetcher=lambda _url, _timeout, payload=payload: payload,
            )
            with self.subTest(message=message), self.assertRaisesRegex(AuthenticationError, message):
                cache.get("rfc-test-key")

    def test_environment_configuration_and_server_mode_are_fail_closed(self):
        environment = {
            "RAG_AUTH_MODE": "oidc-rs256",
            "RAG_OIDC_ISSUER": self.config.issuer,
            "RAG_OIDC_AUDIENCE": self.config.audience,
            "RAG_OIDC_JWKS_URL": self.config.jwks_url,
            "RAG_OIDC_GROUP_ROLE_MAP": '{"group-pe":"PRODUCT_ENGINEER"}',
        }
        authenticator, mode = build_authenticator(environment, dev_auth=False)
        self.assertIsInstance(authenticator, OidcAuthenticator)
        self.assertEqual(mode, "oidc-rs256")
        with self.assertRaisesRegex(ValueError, "RAG_AUTH_SECRET"):
            build_authenticator({}, dev_auth=False)
        local, local_mode = build_authenticator({}, dev_auth=True)
        self.assertIsInstance(local, TokenAuthenticator)
        self.assertEqual(local_mode, "local-demo-hs256")

    def test_http_identity_endpoint_accepts_oidc_authenticator(self):
        class QuietHandler(app_server.AppHandler):
            def log_message(self, fmt, *args):
                return

        httpd = app_server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        httpd.authenticator = self.authenticator
        httpd.allowed_origins = set()
        httpd.dev_auth_enabled = False
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            issued_at = int(time.time())
            http_claims = self.claims(iat=issued_at, exp=issued_at + 300)
            connection = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            connection.request(
                "GET",
                "/api/whoami",
                headers={"Authorization": f"Bearer {issue_token(http_claims)}"},
            )
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["role"], "PRODUCT_ENGINEER")
            self.assertEqual(body["auth_method"], "oidc-rs256")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
