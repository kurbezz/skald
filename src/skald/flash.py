"""One-shot, signed flash messages carried across a POST → redirect → GET."""

import base64
import hashlib
import hmac
import json

from starlette.requests import HTTPConnection
from starlette.responses import Response

from skald.config import get_settings

FLASH_COOKIE_NAME = "flash"
FLASH_KINDS = {"success", "info", "warning", "error"}
_MAX_MESSAGE_LENGTH = 300


def _sign(payload: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, f"flash:{payload}".encode("utf-8"), hashlib.sha256).hexdigest()


def set_flash(response: Response, message: str, kind: str = "success") -> Response:
    """Attach a message shown once on the next rendered page."""
    if kind not in FLASH_KINDS:
        kind = "info"
    data = json.dumps({"m": message[:_MAX_MESSAGE_LENGTH], "k": kind}, separators=(",", ":"))
    payload = base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")
    response.set_cookie(
        FLASH_COOKIE_NAME,
        f"{payload}.{_sign(payload)}",
        httponly=True,
        samesite="lax",
        max_age=60,
    )
    return response


def read_flash(connection: HTTPConnection) -> dict[str, str] | None:
    """Return ``{"message", "kind"}`` from a valid flash cookie, else ``None``."""
    raw = connection.cookies.get(FLASH_COOKIE_NAME)
    if not raw or "." not in raw:
        return None
    payload, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    message, kind = data.get("m"), data.get("k")
    if not isinstance(message, str) or not message or kind not in FLASH_KINDS:
        return None
    return {"message": message, "kind": kind}
