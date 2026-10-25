"""Native Hermes plugin. No identity or credential is accepted as a tool argument."""
import json
from .bridge import Bridge, BridgeError, append_media, bind_inbound, remember_media, trusted_identity


def register(ctx):
    # These are operator-owned per-profile settings, captured at plugin load.
    config_path = ctx.get_config("config_path", "")
    profile = ctx.get_config("profile", "")
    mode = ctx.get_config("transport_mode", "official")
    if mode not in ("official", "legacy"):
        raise ValueError("UNSUPPORTED_TRANSPORT_MODE")
    references = None
    if mode == "legacy":
        # Explicit existing-install opt-in only. Importing the default plugin
        # never imports native adapter shims, upload helpers or reference repair.
        from .direct import Deliveries, accept
        from .permissions import WeixinPermissions
        from .references import References
        from .weixin_ingress import CommandIngress
        ingress = CommandIngress(ctx, profile, config_path)
        ctx.register_platform_handler("weixin", ingress.wire)
        ctx.on_unload(ingress.close)
        references = References(profile, config_path)
        ctx.register_hook("pre_gateway_dispatch", references.observe)
        permissions = WeixinPermissions(profile, config_path, ctx.get_config("auto_approve_weixin", False))
        ctx.register_hook("pre_llm_call", permissions.apply)
        ctx.register_hook("pre_tool_call", permissions.apply)
        ctx.register_hook("on_session_end", permissions.end_turn)
        if permissions.enabled:
            ctx.on_unload(permissions.close)
    else:
        from .standard import bind_admission, capture_status, safe_error

    def admitted(source, message_id, session_key, platform, text="", **_metadata):
        # Official hook supplies authenticated MessageEvent metadata after admission;
        # Weixin currently leaves SessionSource.message_id empty.
        if mode == "official":
            return bind_admission(config_path, profile, source, message_id, session_key, platform)
        bind_inbound(profile, platform, source, message_id, session_key)
        if references is not None and platform == "weixin":
            try:
                unavailable = references.unavailable(source, message_id, session_key, platform, text)
                if unavailable:
                    return unavailable
            except Exception:
                pass
        return accept(ctx, config_path, profile, source, message_id, session_key, platform, text)

    ctx.register_hook("post_gateway_admission", admitted)

    def deliver(response_text, **_metadata):
        try:
            return append_media(profile, response_text, config_path, remote_authorize=mode == "official")
        except BridgeError:
            return None

    ctx.register_hook("transform_llm_output", deliver)

    def invoke(capability, args):
        try:
            identity = trusted_identity(profile)
            result = Bridge.from_file(config_path).invoke(identity, capability, args)
            remember_media(identity, result)
            if mode == "official" and capability == "screenshot.capture":
                result = capture_status(result)
            return json.dumps(result, ensure_ascii=False)
        except BridgeError as error:
            if mode == "official":
                return json.dumps(safe_error(error.code), ensure_ascii=False)
            return json.dumps({"ok": False, "error": error.code, "message": error.message},
                              ensure_ascii=False)
        except Exception:
            # Never forward HTTP errors, configuration values or traceback secrets to a model.
            return json.dumps({"ok": False, "error": "BRIDGE_UNAVAILABLE"})

    def capture(args, **_context):
        return invoke("screenshot.capture", args)

    def economic(args, **_context):
        return invoke("economic.query", args)

    def recent(args, **_context):
        try:
            if args:
                raise BridgeError("INVALID_INPUT")
            identity = trusted_identity(profile)
            return json.dumps(Deliveries(Bridge.from_file(config_path)).latest(identity), ensure_ascii=False)
        except BridgeError as error:
            return json.dumps({"ok": False, "error": error.code, "message": error.message}, ensure_ascii=False)
        except Exception:
            return json.dumps({"ok": False, "error": "RECENT_IMAGE_UNAVAILABLE"})

    if mode == "legacy":
        ctx.register_tool(name="asbot_last_screenshot", toolset="asbot_bridge", handler=recent,
                      description="Read this Weixin peer's latest directly delivered screenshot for analysis; never captures again.",
                      schema={"name": "asbot_last_screenshot", "description":
                              "When asked to analyze the previous screenshot after a numeric command, read this image then use vision_analyze. "
                              "Do not call capture again or resend the image. Images expire within ten minutes.",
                              "parameters": {"type": "object", "additionalProperties": False, "properties": {}}})

    capture_description = ("Capture BTC0–BTC9. The plugin retains the private image and attaches it through the normal "
                           "Hermes reply after fresh delivery authorization. Return ordinary text; do not recapture or "
                           "claim image delivery from a successful capture status.") if mode == "official" else (
                           "Capture BTC0–BTC9. On success include the returned media directive verbatim in your reply. "
                           "For analysis use vision on the returned image path; never recapture just to analyze.")
    ctx.register_tool(name="asbot_screenshot_capture", toolset="asbot_bridge", handler=capture,
                      description="Capture an allowed AS-BOT template; identities come from the live Weixin session.",
                      schema={"name": "asbot_screenshot_capture", "description":
                              capture_description,
                              "parameters": {"type": "object", "additionalProperties": False,
                                             "required": ["shortcut"], "properties": {
                                                 "shortcut": {"type": "string", "pattern": "^(BTC)?[0-9]$"}}}})
    ctx.register_tool(name="asbot_economic_query", toolset="asbot_bridge", handler=economic,
                      description="Read AS-BOT economic calendar with freshness evidence.",
                      schema={"name": "asbot_economic_query", "description":
                              "Read economic calendar, preserving source and freshness evidence. Not real-time news search.",
                              "parameters": {"type": "object", "additionalProperties": False,
                                             "properties": {"kind": {"type": "string", "enum":
                                                                       ["今天", "明天", "本周", "本月", "下一个"]},
                                                            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}})
