from __future__ import annotations

import unittest

from rag_app.auth import AuthenticationError, TokenAuthenticator


TEST_SECRET = "test-secret-that-is-at-least-thirty-two-bytes-long"


class TokenAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.authenticator = TokenAuthenticator(TEST_SECRET, clock_skew_seconds=0)

    def test_signed_identity_is_accepted(self):
        token = self.authenticator.issue_token(
            subject="engineer-42",
            role="PRODUCT_ENGINEER",
            line_ids=("LINE-02",),
            station_ids=("ST-04",),
            now=1_000,
        )
        identity = self.authenticator.authenticate(f"Bearer {token}", now=1_001)
        self.assertEqual(identity.subject, "engineer-42")
        self.assertEqual(identity.role, "PRODUCT_ENGINEER")
        self.assertEqual(identity.station_ids, ("ST-04",))

    def test_missing_token_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            self.authenticator.authenticate(None, now=1_001)

    def test_tampered_payload_is_rejected(self):
        token = self.authenticator.issue_token(subject="line-lead", role="LINE_LEAD", now=1_000)
        header, payload, signature = token.split(".")
        tampered = f"{header}.{payload[:-1]}A.{signature}"
        with self.assertRaisesRegex(AuthenticationError, "signature"):
            self.authenticator.authenticate(f"Bearer {tampered}", now=1_001)

    def test_expired_token_is_rejected(self):
        token = self.authenticator.issue_token(
            subject="engineer-42",
            role="PRODUCT_ENGINEER",
            ttl_seconds=10,
            now=1_000,
        )
        with self.assertRaisesRegex(AuthenticationError, "expired"):
            self.authenticator.authenticate(f"Bearer {token}", now=1_011)

    def test_unknown_role_cannot_be_issued(self):
        with self.assertRaisesRegex(ValueError, "invalid role"):
            self.authenticator.issue_token(subject="attacker", role="NOT_A_REAL_ROLE", now=1_000)


if __name__ == "__main__":
    unittest.main()
