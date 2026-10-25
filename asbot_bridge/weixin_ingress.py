"""Version-pinned, reversible compatibility lane on the EXISTING Weixin adapter.

Hermes has no pre-fingerprint/pre-batch plugin hook. Its native-handler factory
normally exposes a read-only adapter; this explicit local compatibility shim is
an exception, isolated to three instance attributes, never the class/source or
poller. Other text still uses Hermes' original batching and busy agent lane.
"""
import asyncio
from contextvars import ContextVar
import hashlib
import inspect
from pathlib import Path
import time
import textwrap

from .bridge import Bridge, BridgeError
from .direct import accept, admitted_identity, commands, control

PROCESS_SHA256 = "469542dfb4255a30f2bc5419fdb7bc090059da4c1471775237e3518859fb27a1"


def supported(adapter):
    return (type(adapter).__module__ == "gateway.platforms.weixin"
            and hashlib.sha256(textwrap.dedent(inspect.getsource(type(adapter)._process_message)).strip().encode()).hexdigest() == PROCESS_SHA256
            and all(callable(getattr(adapter, key, None)) for key in
                    ("_process_message", "_enqueue_text_event", "_canonicalize", "send"))
            and callable(getattr(getattr(adapter, "_dedup", None), "is_duplicate", None)))


class ScopedDedup:
    def __init__(self, original, exemption):
        self.original, self.exemption = original, exemption

    def is_duplicate(self, key):
        # Only the precise content key in THIS raw-message task is exempt.
        # Server message IDs always go through the native deduplicator.
        if key == self.exemption.get():
            return False
        return self.original.is_duplicate(key)

    def __getattr__(self, name):
        return getattr(self.original, name)


class CommandIngress:
    def __init__(self, ctx, profile, config_path):
        self.ctx, self.profile, self.config_path = ctx, profile, config_path
        self.installed = []
        self.janitor = None
        self.exemption = ContextVar("asbot_weixin_content_exemption", default=None)

    def record(self, status):
        bridge = Bridge.from_file(self.config_path)
        with bridge.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS command_ingress (profile TEXT PRIMARY KEY,status TEXT NOT NULL,release TEXT,updated REAL)")
            db.execute("INSERT OR REPLACE INTO command_ingress VALUES (?,?,?,?)",
                       (self.profile, status, Path(__file__).resolve().parents[1].name, time.time()))

    def wire(self, _native, adapter):
        if not supported(adapter):
            self.record("UNSUPPORTED_NATIVE_VERSION")
            raise BridgeError("UNSUPPORTED_NATIVE_VERSION")
        if any(a is adapter for a, *_ in self.installed):
            return
        original_process, original_enqueue, original_dedup = adapter._process_message, adapter._enqueue_text_event, adapter._dedup
        dedup = ScopedDedup(original_dedup, self.exemption)

        async def process(message):
            # Native parsing, native ID-dedup, pairing and context-token capture
            # remain in the original method. Exempt only an enabled own DM peer.
            from gateway.platforms.weixin import _extract_text
            key = None
            if isinstance(message, dict):
                text = _extract_text(message.get("item_list") or [])
                if commands(text) or control(text):
                    sender = str(message.get("from_user_id") or "").strip()
                    try:
                        bridge = Bridge.from_file(self.config_path)
                        from .bridge import Identity
                        bridge.principal(Identity(self.profile, "weixin", sender, sender, "intake"))
                        key = "content:" + sender + ":" + hashlib.md5(text.encode()).hexdigest()
                    except Exception:
                        pass
            token = self.exemption.set(key)
            try:
                return await original_process(message)
            finally:
                self.exemption.reset(token)

        def enqueue(event):
            raw = getattr(event, "raw_message", None)
            quoted = isinstance(raw, dict) and any(isinstance(i, dict) and i.get("ref_msg") for i in raw.get("item_list", []))
            if ((commands(getattr(event, "text", None)) or control(getattr(event, "text", None)))
                    and not quoted and not getattr(event, "media_urls", None)):
                task = self.dispatch(adapter, event)
                try:
                    self.ctx.spawn_task(task, name="asbot-command-admission")
                except Exception:
                    task.close()
                    raise
                return
            return original_enqueue(event)

        adapter._process_message, adapter._enqueue_text_event, adapter._dedup = process, enqueue, dedup
        self.installed.append((adapter, process, enqueue, dedup, original_process, original_enqueue, original_dedup))
        self.record("READY")
        if self.janitor is None and Bridge.from_file(self.config_path).config.get("external_access") is True:
            task = self.cleanup_loop()
            try:
                self.janitor = self.ctx.spawn_task(task, name="asbot-private-cache-cleanup")
            except Exception:
                task.close()
                raise

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                # Constructor cleans only owned random PNGs, including quota caps.
                await asyncio.to_thread(Bridge.from_file, self.config_path)
            except Exception:
                pass


    async def dispatch(self, adapter, event):
        # This dedicated capability lane intentionally does not occupy/interrupt
        # an AI session. Reapply trusted gateway AND project admission ourselves.
        source = event.source
        try:
            adapter._canonicalize(source)
            from gateway.config import Platform
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if (runner is None or getattr(event, "internal", False)
                    or getattr(source, "profile_route_rejected", False)
                    or getattr(source, "is_bot", False)
                    or runner._authorization_adapter(Platform.WEIXIN, self.profile) is not adapter
                    or not runner._is_user_authorized_for_source(source)):
                return
            identity = admitted_identity(self.profile, "weixin", source.to_dict(), event.message_id, "command-lane")
            bridge = Bridge.from_file(self.config_path)
            bridge.principal(identity)
            if not runner._admit_bot_message_for_source(source):
                return
            from agent.estop import paused_reply
            paused = paused_reply()
            if paused:
                reply = paused
            elif runner._external_drain_active or runner._startup_restore_in_progress or runner._draining:
                reply = "截图入口正在恢复或维护，请稍后重试。"
            else:
                answer = accept(self.ctx, self.config_path, self.profile, source.to_dict(),
                                event.message_id, "command-lane", "weixin", event.text, adapter=adapter)
                reply = answer.get("reply") if answer else None
            if reply:
                bridge.principal(identity)
                await asyncio.wait_for(adapter.send(identity.chat_id, reply), timeout=15)
        except Exception:
            # No raw API error/identity/credential goes to the model or logs.
            # Never fall through to an AI turn after a capability admission.
            return

    def close(self):
        if self.janitor is not None and callable(getattr(self.janitor, "cancel", None)):
            self.janitor.cancel()
        self.janitor = None
        for adapter, process, enqueue, dedup, old_process, old_enqueue, old_dedup in reversed(self.installed):
            for name, own, original in (("_process_message", process, old_process),
                                        ("_enqueue_text_event", enqueue, old_enqueue),
                                        ("_dedup", dedup, old_dedup)):
                if getattr(adapter, name) is own:
                    setattr(adapter, name, original)
            # Hermes remembers factory qualnames across plugin unload/reload.
            wired = getattr(adapter, "_plugin_handlers_wired", None)
            if isinstance(wired, set):
                wired.discard(("asbot-bridge", self.wire.__qualname__))
        self.installed.clear()
