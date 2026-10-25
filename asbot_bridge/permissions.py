"""Operator opt-in auto approval for authorized live Weixin turns only."""
from contextvars import copy_context
import threading

from .bridge import Bridge, trusted_identity, _INBOUND


class WeixinPermissions:
    def __init__(self, profile, config_path, enabled):
        self.profile, self.config_path, self.enabled = profile, config_path, enabled is True
        self.owned = set()
        self.lock = threading.RLock()

    def apply(self, **_metadata):
        if not self.enabled:
            return None
        values = {var.name: value for var, value in copy_context().items()}
        key = values.get("approval_session_key")
        if not isinstance(key, str) or not key or key != values.get("HERMES_SESSION_KEY"):
            return None
        from tools import approval
        try:
            admitted = _INBOUND.get()
            if not admitted or admitted.get("approval_closed"):
                raise ValueError("No active admitted turn")
            identity = trusted_identity(self.profile)
            Bridge.from_file(self.config_path).principal(identity)
        except Exception:
            with self.lock:
                if key in self.owned:
                    approval.disable_session_yolo(key)
                    self.owned.discard(key)
            return None
        with self.lock:
            if not approval.is_session_yolo_enabled(key):
                approval.enable_session_yolo(key)
                self.owned.add(key)
        return None

    def end_turn(self, **_metadata):
        # Hermes emits on_session_end from every run_conversation finalizer,
        # including failure/interruption, before gateway approval context exits.
        values = {var.name: value for var, value in copy_context().items()}
        key = values.get("approval_session_key")
        if key and key == values.get("HERMES_SESSION_KEY"):
            from tools import approval
            with self.lock:
                if key in self.owned:
                    approval.disable_session_yolo(key)
                    self.owned.discard(key)
            admitted = _INBOUND.get()
            if admitted:
                admitted["approval_closed"] = True

    def close(self):
        from tools import approval
        with self.lock:
            for key in self.owned:
                approval.disable_session_yolo(key)
            self.owned.clear()
