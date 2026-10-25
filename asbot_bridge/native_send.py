"""Send one PNG using the existing Weixin transport and retain its server ID."""
import asyncio
import base64
import hashlib
import json
import inspect
from pathlib import Path
import secrets
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
import uuid

from .bridge import BridgeError, MAX_PNG, private_file, validate_png
from .quote_store import message_id

MAX_API_RESPONSE = 1024 * 1024


async def _api(session, api, token, endpoint, payload):
    """Exact native JSON wire format, bounded reads and no credential redirects."""
    from gateway.platforms.weixin import _headers, CHANNEL_VERSION
    if endpoint not in ("ilink/bot/getuploadurl", "ilink/bot/sendmessage"):
        raise BridgeError("INVALID_NATIVE_ENDPOINT")
    body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
                      ensure_ascii=False, separators=(",", ":"))
    async def request():
        async with session.post(api + "/" + endpoint, data=body,
                                headers=_headers(token, body), allow_redirects=False) as response:
            if response.status != 200:
                raise BridgeError("NATIVE_API_FAILED")
            parts, size = [], 0
            while True:
                chunk = await response.content.read(min(16384, MAX_API_RESPONSE + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_API_RESPONSE:
                    raise BridgeError("NATIVE_API_RESPONSE_TOO_LARGE")
                parts.append(chunk)
            result = json.loads(b"".join(parts).decode("utf-8"))
            if not isinstance(result, dict) or any(result.get(k) not in (None, 0) for k in ("ret", "errcode")):
                raise BridgeError("NATIVE_API_FAILED")
            return result
    return await asyncio.wait_for(request(), timeout=15)


def _message(chat_id, context, item):
    msg = {"from_user_id": "", "to_user_id": chat_id,
           "client_id": "hermes-weixin-" + uuid.uuid4().hex,
           "message_type": 2, "message_state": 2, "item_list": [item]}
    if context:
        msg["context_token"] = context
    return {"msg": msg}


def _https_url(value, hosts, *, origin=False):
    if not isinstance(value, str) or len(value) > 16384:
        raise BridgeError("UNTRUSTED_MEDIA_ORIGIN")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username
            or parsed.password or parsed.port is not None or parsed.fragment
            or origin and (parsed.path not in ("", "/") or parsed.query)):
        raise BridgeError("UNTRUSTED_MEDIA_ORIGIN")
    return value.rstrip("/") if origin else value


async def _upload(session, ciphertext, upload_url):
    # The native upload helper follows redirects and includes the private URL or
    # response body in errors. Retain its wire format with redirects disabled.
    async with session.post(upload_url, data=ciphertext,
                            headers={"Content-Type": "application/octet-stream"},
                            allow_redirects=False) as response:
        encrypted = response.headers.get("x-encrypted-param") if response.status == 200 else None
        if not isinstance(encrypted, str) or not 0 < len(encrypted) <= 4096:
            raise BridgeError("NATIVE_UPLOAD_FAILED")
        return encrypted


async def send_image(adapter, chat_id, image_path, caption="", *, authorize=None):
    """No new connection, credential copy, monkeypatch or image-send replay."""
    async def authorize_current():
        if authorize:
            result = authorize()
            if inspect.isawaitable(result):
                await result

    try:
        from gateway.platforms.weixin import (_WEIXIN_CDN_ALLOWLIST,
            _aes128_ecb_encrypt, _image_item)
        session, token = adapter._send_session, adapter._token
        await authorize_current()
        if session is None or not isinstance(token, str) or not token:
            raise BridgeError("LIVE_ADAPTER_REQUIRED")
        api = _https_url(adapter._base_url, {"ilinkai.weixin.qq.com"}, origin=True)
        cdn = _https_url(adapter._cdn_base_url, _WEIXIN_CDN_ALLOWLIST)
        if urlsplit(cdn).path not in ("", "/", "/c2c") or urlsplit(cdn).query:
            raise BridgeError("UNTRUSTED_MEDIA_ORIGIN")
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 512:
            raise BridgeError("INVALID_NATIVE_RECIPIENT")
        path = Path(image_path)
        if not path.is_absolute() or path.suffix.lower() != ".png":
            raise BridgeError("INVALID_ARTIFACT")
        private_file(path)
        if not 0 < path.stat().st_size <= MAX_PNG:
            raise BridgeError("INVALID_PNG")
        plaintext = path.read_bytes()
        validate_png(plaintext)
        filekey, key = secrets.token_hex(16), secrets.token_bytes(16)
        ciphertext = _aes128_ecb_encrypt(plaintext, key)
        upload = await _api(session, api, token, "ilink/bot/getuploadurl", {
            "to_user_id": chat_id, "media_type": 1, "filekey": filekey, "rawsize": len(plaintext),
            "rawfilemd5": hashlib.md5(plaintext).hexdigest(), "filesize": len(ciphertext),
            "no_need_thumb": True, "aeskey": key.hex()})
        param = upload.get("upload_param") if isinstance(upload, dict) else None
        if isinstance(param, str) and 0 < len(param) <= 4096:
            upload_url = cdn + "/upload?encrypted_query_param=" + quote(param, safe="") + "&filekey=" + quote(filekey, safe="")
        else:
            upload_url = _https_url(upload.get("upload_full_url") if isinstance(upload, dict) else None,
                                    _WEIXIN_CDN_ALLOWLIST)
        await authorize_current()
        encrypted = await asyncio.wait_for(_upload(session, ciphertext, upload_url), timeout=30)
        await authorize_current()
        if caption:
            context = adapter._token_store.get(adapter._account_id, chat_id)
            await _api(session, api, token, "ilink/bot/sendmessage",
                       _message(chat_id, context, {"type": 1, "text_item": {"text": caption}}))
        context = adapter._token_store.get(adapter._account_id, chat_id)
        item = _image_item(encrypt_query_param=encrypted,
            aes_key_for_api=base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
            ciphertext_size=len(ciphertext), plaintext_size=len(plaintext), filename=path.name,
            rawfilemd5=hashlib.md5(plaintext).hexdigest())
        await authorize_current()
        response = await _api(session, api, token, "ilink/bot/sendmessage", _message(chat_id, context, item))
        if not isinstance(response, dict) or any(response.get(k) not in (None, 0) for k in ("ret", "errcode")):
            raise BridgeError("NATIVE_SEND_FAILED")
        return SimpleNamespace(success=True, message_id=message_id(response.get("message_id")),
                               image_digest=hashlib.sha256(plaintext).hexdigest())
    except asyncio.CancelledError:
        raise
    except Exception:
        # Do not expose native API/CDN exceptions, URLs, ciphertext or response bodies.
        raise BridgeError("NATIVE_IMAGE_SEND_UNKNOWN") from None
