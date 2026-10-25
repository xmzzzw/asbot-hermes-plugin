"""Inspect and repair native Weixin image references without guessing a target."""
import asyncio
import base64
import copy
from contextvars import ContextVar
import hashlib
import json
import time
from urllib.parse import urlsplit

from .bridge import Bridge, BridgeError
from .direct import admitted_identity, live_adapter, principal_key
from .quote_store import QuoteStore, reference_ids


def reference_items(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("item_list"), list):
        return []
    result = []
    for item in raw["item_list"][:16]:
        if not isinstance(item, dict):
            continue
        reference = item.get("ref_msg")
        if not isinstance(reference, dict):
            continue
        value = reference.get("message_item")
        if isinstance(value, dict):
            result.append(value)
    return result


def shape(raw, event):
    """Fixed field-presence diagnostics only. No arbitrary keys, text or values."""
    raw = raw if isinstance(raw, dict) else {}
    items = raw.get("item_list") if isinstance(raw.get("item_list"), list) else []
    refs = reference_items(raw)
    return {"version": 1, "item_count": min(len(items), 99),
            "server_reference_id_present": any(isinstance(i, dict) and isinstance(i.get("ref_msg"), dict) and i["ref_msg"].get("svr_id") is not None for i in items[:16]),
            "nested_reference_id_present": any(v.get("msg_id") is not None for v in refs),
            "top_ref_present": isinstance(raw.get("ref_msg"), dict),
            "item_ref_count": sum(isinstance(i, dict) and isinstance(i.get("ref_msg"), dict) for i in items[:16]),
            "reference_items": [{"type": v.get("type") if type(v.get("type")) is int and 0 <= v["type"] <= 9 else None,
                                 "image_payload": isinstance(v.get("image_item"), dict),
                                 "media_descriptor": isinstance((v.get("image_item") or {}).get("media"), dict)
                                 if isinstance(v.get("image_item"), dict) else False} for v in refs],
            "native_attachment_count": min(len(getattr(event, "media_urls", None) or []), 99)}


def image_reference(item):
    if (not isinstance(item, dict) or not (item.get("type") is None or type(item.get("type")) is int and item["type"] == 2)
            or not isinstance(item.get("image_item"), dict)
            or any(k in item for k in ("voice_item", "file_item", "video_item", "text_item"))):
        return None
    payload = item["image_item"]
    media = payload.get("media")
    if not isinstance(media, dict):
        return None
    encrypted = media.get("encrypt_query_param")
    if not isinstance(encrypted, str) or not 0 < len(encrypted) <= 4096:
        return None
    # Native decoding is reused only with the fixed live adapter CDN endpoint.
    # Never fetch a supplied full_url (including a whitelisted redirector).
    normalized = copy.deepcopy(item)
    normalized["image_item"]["media"].pop("full_url", None)
    normalized["type"] = 2
    return normalized


async def native_reference_image(adapter, item):
    """Reuse native crypto/cache primitives, without its exception-logging wrapper.

    Native ``_collect_media`` logs an exception's full URL, which can contain a
    private CDN download parameter. Let errors reach our bounded silent handler
    instead; logging policy for unrelated Hermes requests remains unchanged.
    """
    from gateway.platforms.weixin import (_download_and_decrypt_media,
                                         cache_image_from_bytes_async)
    payload = item["image_item"]
    media = payload["media"]
    aes_key = media.get("aes_key")
    if payload.get("aeskey"):
        # Match the installed native image decoder's alternate raw hex form.
        aes_key = base64.b64encode(bytes.fromhex(str(payload["aeskey"]))).decode("ascii") or aes_key
    data = await _download_and_decrypt_media(
        adapter._poll_session, cdn_base_url=adapter._cdn_base_url,
        encrypted_query_param=media["encrypt_query_param"], aes_key_b64=aes_key,
        full_url=None, timeout_seconds=10)
    return await cache_image_from_bytes_async(data, ".jpg")


class References:
    def __init__(self, profile, config_path):
        self.profile, self.config_path = profile, config_path
        self.receipt = ContextVar("asbot_reference_receipt", default=None)

    def unavailable(self, source, message_id, session_key, platform, text):
        identity = admitted_identity(self.profile, platform, source, message_id, session_key)
        receipt = self.receipt.get()
        if receipt != (identity, "unavailable"):
            return None
        Bridge.from_file(self.config_path).principal(identity)
        return {"action": "handled", "reply": "这条微信引用未提供可读取的原图，或原图缓存已过期。请将被引用的图片直接转发给我（不要只引用），我就能分析准确的那张图。"}

    async def observe(self, event, gateway, **_metadata):
        self.receipt.set(None)
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None)
        if platform != "weixin":
            return None
        try:
            # This hook precedes the shared admission gate. Perform both existing
            # authorization checks before file access, diagnostics or media I/O.
            authorize = getattr(gateway, "_is_user_authorized_for_source", None)
            if not callable(authorize) or not authorize(source):
                return None
            snapshot = source.to_dict()
            identity = admitted_identity(self.profile, platform, snapshot,
                                         getattr(event, "message_id", None), "reference-inspection")
            bridge = Bridge.from_file(self.config_path)
            token = bridge.principal(identity)["bearer_token"]
            raw = getattr(event, "raw_message", None)
            diagnostic = shape(raw, event)
            self.record(bridge, identity, diagnostic)
            ids = reference_ids(raw)
            unambiguous = diagnostic["item_ref_count"] == 1 and len(ids) <= 1
            if not (getattr(event, "media_urls", None) or []) and len(ids) == 1 and unambiguous:
                adapter = live_adapter(self.profile)
                path = QuoteStore(bridge).find(identity, adapter._account_id, ids[0])
                if path:
                    bridge.principal(identity, token)
                    event.media_urls, event.media_types = [path], ["image/png"]
                    from gateway.platforms.base import MessageType
                    event.message_type = MessageType.PHOTO
                    diagnostic["server_id_cache_hit"] = True
            if not (getattr(event, "media_urls", None) or []):
                candidates = [v for v in reference_items(raw) if image_reference(v)]
                if len(candidates) == 1 and unambiguous:
                    adapter = live_adapter(self.profile)
                    from gateway.platforms.weixin import _WEIXIN_CDN_ALLOWLIST
                    parsed = urlsplit(adapter._cdn_base_url)
                    if parsed.scheme != "https" or parsed.hostname not in _WEIXIN_CDN_ALLOWLIST or parsed.username or parsed.password or parsed.port not in (None, 443):
                        raise BridgeError("UNTRUSTED_MEDIA_ORIGIN")
                    # Current Hermes already decodes complete typed references.
                    # Repair only the same native shape with an omitted type;
                    # no title/latest-image inference or standalone transport.
                    path = await asyncio.wait_for(native_reference_image(
                        adapter, image_reference(candidates[0])), timeout=10)
                    bridge.principal(identity, token)
                    if isinstance(path, str) and path:
                        event.media_urls = [path]
                        event.media_types = ["image/jpeg"]
                        from gateway.platforms.base import MessageType
                        event.message_type = MessageType.PHOTO
                        diagnostic["repaired_attachment_count"] = 1
            self.record(bridge, identity, diagnostic)
            if diagnostic["item_ref_count"] and not (getattr(event, "media_urls", None) or []):
                import re
                empty_reference = any(v.get("type") == 0 and not v.get("text_item") for v in reference_items(raw))
                if empty_reference or re.search(r"图片|截图|这张图|那张图|引用.{0,12}图|\b(?:image|picture|screenshot)\b", getattr(event, "text", ""), re.I):
                    self.receipt.set((identity, "unavailable"))
        except Exception:
            # No details from native CDN exceptions/credentials enter prompts.
            return None
        return None

    def record(self, bridge, identity, diagnostic):
        key = hashlib.sha256((principal_key(identity) + ":" + identity.message_id).encode()).hexdigest()
        with bridge.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS reference_diagnostics (key TEXT PRIMARY KEY,profile TEXT,created REAL,shape TEXT)")
            db.execute("INSERT OR REPLACE INTO reference_diagnostics VALUES (?,?,?,?)",
                       (key, identity.profile, time.time(), json.dumps(diagnostic, sort_keys=True)))
            db.execute("DELETE FROM reference_diagnostics WHERE key NOT IN (SELECT key FROM reference_diagnostics ORDER BY created DESC LIMIT 64)")
