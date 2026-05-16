from __future__ import annotations

import base64
import hashlib
import hmac
import unittest

from taskforge.security import verify_trello_signature


class SecurityTests(unittest.TestCase):
    def test_signature_validation(self) -> None:
        secret = "top-secret"
        callback_url = "https://example.com/webhooks/trello"
        body = b'{"action":{"id":"1"}}'
        signature = base64.b64encode(
            hmac.new(secret.encode(), body + callback_url.encode(), hashlib.sha1).digest()
        ).decode("ascii")

        self.assertTrue(
            verify_trello_signature(
                secret=secret,
                callback_url=callback_url,
                raw_body=body,
                header_value=signature,
            )
        )

    def test_signature_can_be_disabled_for_local_testing(self) -> None:
        self.assertTrue(
            verify_trello_signature(
                secret="",
                callback_url="https://example.com/webhooks/trello",
                raw_body=b"{}",
                header_value=None,
            )
        )


if __name__ == "__main__":
    unittest.main()

