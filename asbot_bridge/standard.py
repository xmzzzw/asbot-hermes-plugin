"""Public Hermes admission binding for ordinary model-invoked plugin tools.

This hook never submits capabilities or waits for screenshots. Hermes admission
hooks fail open after their timeout; slow work here could submit a capture and
then also fall through to a model turn. Numeric commands, busy bypass and private
native reference repair are therefore unsupported in standard mode.
"""
import re
from .bridge import Bridge, Identity, bind_inbound


def bind_admission(config_path, profile, source, message_id, session_key, platform):
    """Bind only a fully admitted, local-allowlisted DM; return no flow directive.

    The public hook supplies the transport message id omitted from Weixin's
    SessionSource. Tools must subsequently match this receipt and Hermes' live
    ContextVars; text or process environment cannot supply identity.
    """
    bind_inbound(profile, "", {}, "", "")  # clear a prior receipt before validation
    if (platform != "weixin" or not isinstance(source, dict)
            or (source.get("profile") or "default") != profile
            or source.get("chat_type") != "dm" or not isinstance(session_key, str) or not session_key
            or source.get("is_bot") is True or source.get("profile_route_rejected") is True):
        return None
    values = [source.get("user_id"), source.get("chat_id"), message_id]
    if any(not isinstance(value, str) or not value or len(value) > 512
           or any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values):
        return None
    try:
        bridge = Bridge.from_file(config_path)
        bridge.principal(Identity(profile, platform, *values))
    except Exception:
        # Local check only; unbound later tools fail closed in trusted_identity().
        return None
    bind_inbound(profile, platform, source, message_id, session_key)
    return None


def support():
    return {"transportMode": "official", "tools": ["screenshot.capture", "economic.query"],
            "numericCommands": "UNSUPPORTED", "busyBypass": "UNSUPPORTED",
            "repeatedInbound": "HERMES_OWNED_NOT_GUARANTEED",
            "referenceRepair": "UNSUPPORTED", "nativeReceipt": "PENDING_REAL_RECEIPT",
            "qualified": False}


def capture_status(result):
    """Model-safe status only: the private image remains in the admission ctx.

    No dynamic message, label, media directive, file path or backend metadata is
    returned. A transform timeout can therefore only release path-free prose.
    """
    if result.get("duplicate") is True:
        return {"ok": result.get("ok") is True, "duplicate": True,
                "state": "recorded", "message": "该请求已记录，不重复提交或发送图片。"}
    if result.get("ok") is True and result.get("image"):
        return {"ok": True, "state": "awaiting_delivery_authorization",
                "message": "截图已完成；插件会在核对交付授权后通过本次回复附图。不要再次截图。"}
    return {"ok": False, "state": "recorded",
            "message": "请求已记录，结果待核对；不要重复提交或声称图片已送达。"}


def safe_error(code):
    value = code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code) else "BRIDGE_UNAVAILABLE"
    return {"ok": False, "error": value,
            "message": "请求未完成；请保留原请求状态，核对后继续。"}
