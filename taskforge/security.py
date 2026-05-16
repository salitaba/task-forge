from __future__ import annotations

import base64
import hashlib
import hmac


def verify_trello_signature(
    *,
    secret: str,
    callback_url: str,
    raw_body: bytes,
    header_value: str | None,
) -> bool:
    if not secret:
        return True
    if not header_value:
        return False

    digest = hmac.new(secret.encode("utf-8"), raw_body + callback_url.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header_value)

