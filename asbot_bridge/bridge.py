"""Bounded, fail-closed HTTPS bridge; stdlib only. SQLite state is shared by both profiles."""
from __future__ import annotations

from .private_io import secure_open, secure_file, secure_dir, lock_exclusive
from contextvars import ContextVar, copy_context
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import ssl
import threading
import struct
import time
import urllib.parse
import uuid
import zlib

MAX_PNG = 8 * 1024 * 1024
MAX_JSON = 1024 * 1024
OPERATION = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
KINDS = ("今天", "明天", "本周", "本月", "下一个")
_INBOUND = ContextVar("ASBOT_AUTHENTICATED_INBOUND", default=None)


class BridgeError(Exception):
    def __init__(self, code, message="Request could not be completed; no automatic resubmission."):
        self.code, self.message = code, message
        super().__init__(code)


@dataclass(frozen=True)
class Identity:
    profile: str
    platform: str
    user_id: str
    chat_id: str
    message_id: str


def bind_inbound(plugin_profile, platform, source, message_id, session_key):
    """Capture admitted platform metadata in the caller's task; no model text is parsed."""
    _INBOUND.set(None)
    if (platform != "weixin" or not isinstance(source, dict) or not isinstance(message_id, str)
            or not message_id or not session_key or (source.get("profile") or "default") != plugin_profile
            or source.get("chat_type") != "dm"):
        return
    # The per-message dict is shared by copied tool-thread contexts; no global media list.
    _INBOUND.set({"identity": (plugin_profile, platform, source.get("user_id"), source.get("chat_id"), message_id, session_key),
                  "media": []})


def trusted_identity(plugin_profile: str) -> Identity:
    """Read ONLY bound ContextVars, never Hermes' legacy os.environ fallback.

    The installed gateway binds these from authenticated inbound MessageEvent.source.
    Empty profile is the gateway's bound default profile, not an unbound fallback.
    Missing fields (including profile) fail closed in CLI/cron/delegated/host-isolated tools.
    """
    names = ("PROFILE", "PLATFORM", "USER_ID", "CHAT_ID", "CHAT_TYPE", "KEY")
    values = {var.name: value for var, value in copy_context().items()
              if var.name.startswith("HERMES_SESSION_") and isinstance(value, str)}
    if not plugin_profile or any("HERMES_SESSION_" + name not in values for name in names):
        raise BridgeError("TRUSTED_CONTEXT_REQUIRED")
    profile = values["HERMES_SESSION_PROFILE"] or "default"
    if profile != plugin_profile or values["HERMES_SESSION_PLATFORM"] != "weixin":
        raise BridgeError("SESSION_FORBIDDEN")
    if values["HERMES_SESSION_CHAT_TYPE"] not in ("dm", "private", "direct"):
        raise BridgeError("DIRECT_MESSAGE_REQUIRED")
    inbound = _INBOUND.get()
    receipt = inbound["identity"] if inbound else None
    fields = [values["HERMES_SESSION_" + name] for name in ("USER_ID", "CHAT_ID")]
    if (not receipt or receipt[:4] != (profile, "weixin", *fields)
            or receipt[5] != values["HERMES_SESSION_KEY"]):
        raise BridgeError("ADMITTED_MESSAGE_REQUIRED")
    message_id = values.get("HERMES_SESSION_MESSAGE_ID") or receipt[4]
    if message_id != receipt[4]:
        raise BridgeError("ADMITTED_MESSAGE_MISMATCH")
    fields.append(message_id)
    if any(not field or len(field) > 512 or any(ord(c) < 32 for c in field) for field in fields):
        raise BridgeError("TRUSTED_CONTEXT_REQUIRED")
    return Identity(profile, "weixin", *fields)


def remember_media(identity, result):
    inbound = _INBOUND.get()
    if (inbound and result.get("ok") is True and result.get("image") and
            inbound["identity"][:5] == (identity.profile, identity.platform, identity.user_id,
                                         identity.chat_id, identity.message_id)):
        path = result["image"]
        if path not in inbound["media"]:
            inbound["media"].append(path)


def append_media(profile, response_text, config_path=None, *, remote_authorize=False):
    """Official final-output transform ensures generated artifacts reach the media parser.

    Only paths recorded by this bridge in the current admitted message may be appended;
    model prose/other tools cannot nominate paths. No send-success receipt is inferred.
    """
    identity = trusted_identity(profile)
    if not isinstance(response_text, str) or not response_text:
        return None
    paths = _INBOUND.get()["media"]
    if remote_authorize and not paths:
        return None
    if config_path is not None or remote_authorize:
        try:
            bridge = Bridge.from_file(config_path)
            if remote_authorize:
                bridge.authorize_delivery(identity)
            else:
                bridge.principal(identity)
        except Exception:
            # Even an assistant that copied the path before revocation must not send it.
            for name in paths:
                if remote_authorize:
                    forms = {name, json.dumps(name), json.dumps(name, ensure_ascii=False)}
                    for form in sorted(forms, key=len, reverse=True):
                        response_text = re.sub(r"MEDIA:\s*" + re.escape(form), "", response_text, flags=re.I)
                        response_text = response_text.replace(form, "[图片未交付]")
                else:
                    response_text = response_text.replace("MEDIA:" + name, "").replace(name, "[图片授权已撤销]")
            message = "图片交付授权未通过，未附加截图。" if remote_authorize else "图片授权已撤销，未附加截图。"
            return response_text.rstrip() + "\n\n" + message
    additions = []
    for name in paths:
        path = Path(name)
        try:
            private_file(path)
            if time.time() - path.lstat().st_mtime >= 540:
                continue
        except OSError:
            continue
        directive = "MEDIA:" + str(path)
        if directive not in response_text:
            additions.append(directive)
    return response_text.rstrip() + "\n\n" + "\n".join(additions) if additions else None


def private_file(path: Path):
    try:
        secure_file(path)
    except (OSError, ValueError):
        raise BridgeError("PRIVATE_FILE_REQUIRED") from None


def private_dir(path: Path):
    if not path.is_absolute():
        raise BridgeError("ABSOLUTE_STATE_PATH_REQUIRED")
    try:
        secure_dir(path)
    except (OSError, ValueError):
        raise BridgeError("PRIVATE_DIRECTORY_REQUIRED") from None


def validate_config(config):
    """Validate the complete private shape before config can authorize delivery."""
    def text(value):
        return isinstance(value, str) and bool(value) and not any(ord(char) < 32 for char in value)

    if (not isinstance(config, dict) or not text(config.get("origin")) or not text(config.get("state_dir"))
            or not isinstance(config.get("access"), dict)
            or not all(text(config["access"].get(key)) for key in ("client_id", "client_secret"))
            or not isinstance(config.get("principals"), list)):
        raise BridgeError("INVALID_CONFIGURATION")
    if "external_access" in config and type(config["external_access"]) is not bool:
        raise BridgeError("INVALID_CONFIGURATION")
    identities, tokens = set(), set()
    for principal in config["principals"]:
        if (not isinstance(principal, dict)
                or not all(text(principal.get(key)) for key in ("profile", "user_id", "chat_id", "bearer_token"))
                or type(principal.get("enabled")) is not bool):
            raise BridgeError("INVALID_CONFIGURATION")
        identity = (principal["profile"], principal["user_id"], principal["chat_id"])
        if identity in identities or principal["bearer_token"] in tokens:
            raise BridgeError("INVALID_CONFIGURATION")
        identities.add(identity)
        tokens.add(principal["bearer_token"])
    return config


def normalize(capability, args):
    if not isinstance(args, dict):
        raise BridgeError("INVALID_INPUT")
    if capability == "screenshot.capture":
        if set(args) != {"shortcut"} or not isinstance(args["shortcut"], str):
            raise BridgeError("INVALID_INPUT")
        value = args["shortcut"].upper()
        if not re.fullmatch(r"(?:BTC)?[0-9]", value):
            raise BridgeError("INVALID_INPUT")
        return {"shortcut": value[-1]}
    if capability == "economic.query":
        kind, limit = args.get("kind", "今天"), args.get("limit", 20)
        if set(args) - {"kind", "limit"} or kind not in KINDS or type(limit) is not int or not 1 <= limit <= 50:
            raise BridgeError("INVALID_INPUT")
        return {"kind": kind, "limit": limit}
    raise BridgeError("CAPABILITY_FORBIDDEN")


class Transport:
    """One bounded, certificate-verified HTTPS connection per bridge invocation.

    No proxy, redirect, cross-origin pool or automatic POST retry. A broken socket
    is discarded; only the existing GET polling loop may retry a safe read.
    """
    def __init__(self, origin, access):
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise BridgeError("HTTPS_ORIGIN_REQUIRED")
        self.origin, self.access, self.hostname = origin, access, parsed.hostname
        self.context = ssl.create_default_context()
        self.connection = None
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            old, self.connection = self.connection, None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass

    def request(self, method, path, token, payload=None, png=False, idempotency_key=None):
        if (method not in ("GET", "POST") or not path.startswith("/api/v1/")
                or "?" in path or "#" in path or ".." in path
                or any(ord(c) < 32 for c in path)):
            raise BridgeError("INVALID_API_PATH")
        headers = {"CF-Access-Client-Id": self.access["client_id"],
                   "CF-Access-Client-Secret": self.access["client_secret"],
                   "Authorization": "Bearer " + token, "Accept": "image/png" if png else "application/json",
                   "User-Agent": "AS-BOT-Hermes/1.0"}
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
                raise BridgeError("INVALID_IDEMPOTENCY_KEY")
            headers["Idempotency-Key"] = idempotency_key
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self.lock:
            try:
                if self.connection is None:
                    self.connection = http.client.HTTPSConnection(self.hostname, 443, timeout=20, context=self.context)
                # HTTPSConnection does not use ambient proxy variables or follow redirects.
                self.connection.request(method, path, body=data, headers=headers)
                response = self.connection.getresponse()
                with response:
                    if not 200 <= response.status < 300:
                        raise BridgeError("HTTP_" + str(response.status))
                    maximum = MAX_PNG if png else MAX_JSON
                    body = response.read(maximum + 1)
                    if len(body) > maximum:
                        raise BridgeError("RESPONSE_TOO_LARGE")
                    if response.headers.get_content_type() != ("image/png" if png else "application/json"):
                        raise BridgeError("INVALID_RESPONSE_TYPE")
                    result = (response.status, body if png else json.loads(body))
                    if response.will_close:
                        self.close()
                    return result
            except BridgeError:
                self.close()
                raise
            except Exception:
                # A lost write response may represent accepted business work.
                self.close()
                raise BridgeError("TRANSPORT_ERROR") from None


def validate_png(data):
    if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_PNG:
        raise BridgeError("INVALID_PNG")
    offset, first, seen_data, compressed = 8, True, False, bytearray()
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        end = offset + 12 + length
        if end > len(data) or zlib.crc32(kind + payload) & 0xffffffff != struct.unpack(">I", data[end - 4:end])[0]:
            break
        if first:
            if kind != b"IHDR" or length != 13:
                break
            width, height = struct.unpack(">II", payload[:8])
            if not 0 < width <= 16000 or not 0 < height <= 16000 or width * height > 40000000:
                break
            depth, color, compression, filtering, interlace = payload[8:]
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
            valid_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if not channels or depth not in valid_depths[color] or compression or filtering or interlace:
                break
            stride = (width * channels * depth + 7) // 8 + 1
            expected = stride * height
            if expected > 160 * 1024 * 1024:
                break
            first = False
        elif kind == b"IHDR":
            break
        if kind == b"IDAT":
            seen_data = True
            compressed.extend(payload)
        if kind == b"IEND":
            if length == 0 and end == len(data) and seen_data:
                try:
                    decoder = zlib.decompressobj()
                    pixels = decoder.decompress(compressed, expected + 1)
                    if (len(pixels) == expected and decoder.eof and not decoder.unused_data
                            and not decoder.unconsumed_tail and all(pixels[row * stride] <= 4 for row in range(height))):
                        return
                except zlib.error:
                    pass
            break
        offset = end
    raise BridgeError("INVALID_PNG")


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        private_dir(self.root)
        self.path = self.root / "requests.sqlite3"
        fd = secure_open(self.path, os.O_CREAT | os.O_RDWR)
        os.close(fd)
        private_file(self.path)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, capability TEXT NOT NULL, state TEXT NOT NULL,
                operation TEXT, result TEXT, created REAL NOT NULL, owner INTEGER NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def call_lock(self, key):
        path = self.root / (key + ".lock")
        fd = secure_open(path, os.O_CREAT | os.O_RDWR)
        try:
            private_file(path)
            try:
                lock_exclusive(fd)
            except BlockingIOError:
                raise BridgeError("REQUEST_IN_PROGRESS") from None
            yield
        finally:
            os.close(fd)

    def begin(self, key, capability):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
            if row:
                return dict(row), False
            cap = 5 if capability == "screenshot.capture" else 2
            count = db.execute("SELECT COUNT(*) FROM requests WHERE capability=? AND state IN ('queued','submitting','pending','unknown')",
                               (capability,)).fetchone()[0]
            if count >= cap:
                raise BridgeError("QUEUE_FULL", "服务繁忙，请稍后发送新的请求。")
            db.execute("INSERT INTO requests VALUES (?,?,?,NULL,NULL,?,?)",
                       (key, capability, "queued", time.time(), os.getpid()))
            db.commit()
        return self.get(key), True

    def get(self, key):
        with self.connect() as db:
            return dict(db.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone())

    def update(self, key, state, operation=None, result=None):
        with self.connect() as db:
            db.execute("UPDATE requests SET state=?,operation=COALESCE(?,operation),result=? WHERE key=?",
                       (state, operation, json.dumps(result, ensure_ascii=False) if result is not None else None, key))

    def acquire(self, key, capability):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if capability == "screenshot.capture":
                # Unknown POST or unresolved server job retains the durable global slot.
                blocked = db.execute("SELECT 1 FROM requests WHERE capability=? AND state IN ('submitting','pending','unknown') AND key!=? LIMIT 1",
                                     (capability, key)).fetchone()
                first = db.execute("SELECT key FROM requests WHERE capability=? AND state='queued' ORDER BY created,key LIMIT 1",
                                   (capability,)).fetchone()
                if blocked or first and first[0] != key:
                    return False
            changed = db.execute("UPDATE requests SET state='submitting' WHERE key=? AND state='queued'", (key,)).rowcount
            db.commit()
            return bool(changed)


class Bridge:
    def __init__(self, config, transport=None, wait_seconds=180):
        self.config = config
        self.config_path = None
        self.transport = transport or Transport(config["origin"], config["access"])
        self.ledger = Ledger(config["state_dir"])
        self.wait_seconds = max(wait_seconds, 360) if config.get("external_access") is True and wait_seconds == 180 else wait_seconds
        self.artifacts = self.ledger.root / "artifacts"
        private_dir(self.artifacts)
        self.cleanup_artifacts()

    def cleanup_artifacts(self):
        # Enforce both source-image TTL and quoted-image caps at startup and
        # every invocation; expired files cannot survive an idle restart.
        retained_count = retained_bytes = 0
        for folder, retention in ((self.artifacts, 540), (self.ledger.root / "quoted-artifacts", 7 * 86400)):
            private_dir(folder)
            for path in sorted(folder.glob("*.png"), key=lambda p: p.lstat().st_mtime, reverse=True):
                if not re.fullmatch(r"[a-f0-9]{32}\.png", path.name):
                    continue
                private_file(path)
                retained_count += 1
                retained_bytes += path.lstat().st_size
                if time.time() - path.lstat().st_mtime >= retention or retained_count > 256 or retained_bytes > 512 * 1024 * 1024:
                    path.unlink()

    @classmethod
    def from_file(cls, file):
        path = Path(file)
        if not path.is_absolute():
            raise BridgeError("CONFIG_REQUIRED")
        private_file(path)
        try:
            config = validate_config(json.loads(path.read_text()))
            instance = cls(config)
            instance.config_path = path
            return instance
        except BridgeError:
            raise
        except Exception:
            raise BridgeError("INVALID_CONFIGURATION") from None

    def principal(self, identity, expected_token=None):
        if identity.platform != "weixin":
            raise BridgeError("SESSION_FORBIDDEN")
        config = self.config
        if self.config_path is not None:
            private_file(self.config_path)
            try:
                config = validate_config(json.loads(self.config_path.read_text()))
            except Exception:
                raise BridgeError("INVALID_CONFIGURATION") from None
            if any(config.get(field) != self.config.get(field) for field in ("origin", "access", "state_dir", "external_access", "account_id", "installation_id")):
                raise BridgeError("LOCAL_AUTHORIZATION_CHANGED")
        match = [p for p in config["principals"] if
                 (p["profile"], p["user_id"], p["chat_id"]) ==
                 (identity.profile, identity.user_id, identity.chat_id)]
        if len(match) != 1 or match[0].get("enabled") is not True:
            raise BridgeError("PRINCIPAL_FORBIDDEN")
        if expected_token is not None and match[0]["bearer_token"] != expected_token:
            raise BridgeError("LOCAL_AUTHORIZATION_CHANGED")
        return match[0]

    def installation_namespace(self, identity):
        principal = self.principal(identity)
        if self.config.get("external_access") is not True:
            return None
        # Metadata only namespaces local evidence; server authentication remains
        # authoritative and never trusts these user-editable identifiers.
        if self.config.get("account_id") and self.config.get("installation_id"):
            return [self.config["account_id"], self.config["installation_id"]]
        return [hashlib.sha256(principal["bearer_token"].encode()).hexdigest()]

    def service_get(self, identity, endpoint):
        if endpoint not in ("me", "screenshot-templates"):
            raise BridgeError("INVALID_API_PATH")
        principal = self.principal(identity)
        status, result = self.transport.request("GET", "/api/v1/" + endpoint, principal["bearer_token"])
        self.principal(identity, principal["bearer_token"])
        if status != 200 or not isinstance(result, dict):
            raise BridgeError("INVALID_API_RESPONSE")
        return result

    def authorize_delivery(self, identity, expected_token=None):
        principal = self.principal(identity, expected_token)
        if self.config.get("external_access") is True:
            result = self.service_get(identity, "me")
            if result.get("authorized") is not True or "screenshot.capture" not in result.get("capabilities", []):
                raise BridgeError("EXTERNAL_ACCESS_DISABLED")
        return principal

    def invoke(self, identity, capability, args):
        principal = self.principal(identity)  # Re-read config on every Hermes invocation/revocation.
        normalized = normalize(capability, args)
        key_fields = [identity.profile, identity.platform, identity.user_id,
                      identity.chat_id, identity.message_id, capability, normalized]
        namespace = self.installation_namespace(identity)
        if namespace is not None:
            key_fields.insert(0, namespace)
        key = hashlib.sha256(json.dumps(key_fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            with self.ledger.call_lock(key):
                return self._invoke(key, identity, principal, capability, normalized)
        finally:
            close = getattr(self.transport, "close", None)
            if callable(close):
                close()

    def _invoke(self, key, identity, principal, capability, normalized):
        row, fresh = self.ledger.begin(key, capability)
        if not fresh:
            if row["state"] == "succeeded":
                # Replay status, never automatically resend an image after unknown delivery.
                return {"ok": True, "duplicate": True, "state": "succeeded", "message": "该消息已完成，不重复提交或发送。"}
            if row["state"] == "pending" and row["operation"]:
                return self.poll(key, row["operation"], principal["bearer_token"], identity)
            if self.config.get("external_access") is True and row["state"] in ("unknown", "submitting"):
                return self.reconcile(key, capability, principal["bearer_token"], identity)
            return {"ok": False, "duplicate": True, "state": row["state"],
                    "error": "REQUEST_ALREADY_RECORDED", "message": "请求已记录；执行或发送结果未知时不会自动重放。"}
        deadline = time.monotonic() + self.wait_seconds
        while not self.ledger.acquire(key, capability):
            if time.monotonic() >= deadline:
                self.ledger.update(key, "failed")
                raise BridgeError("QUEUE_TIMEOUT")
            time.sleep(0.25)
        try:
            self.principal(identity, principal["bearer_token"])
        except BridgeError:
            # No network write has occurred for this queued request.
            self.ledger.update(key, "failed")
            raise
        try:
            # Durable submitting state BEFORE network write. Exactly one POST, never automatic retry.
            status, body = self.transport.request("POST", "/api/v1/capabilities/" + capability + ":invoke",
                                                  principal["bearer_token"], {"version": "1", "input": normalized},
                                                  **({"idempotency_key": key} if self.config.get("external_access") is True else {}))
            if capability == "economic.query":
                if status != 200 or body.get("capabilityId") != capability or "result" not in body:
                    raise BridgeError("INVALID_API_RESPONSE")
                result = {"ok": True, "result": body["result"]}
                self.principal(identity, principal["bearer_token"])
                self.ledger.update(key, "succeeded", result=result)
                return result
            operation = body.get("id")
            if status != 202 or not isinstance(operation, str) or not OPERATION.fullmatch(operation):
                raise BridgeError("INVALID_API_RESPONSE")
            self.ledger.update(key, "pending", operation=operation)
        except BridgeError as error:
            if error.code in ("HTTP_400", "HTTP_401", "HTTP_403", "HTTP_404", "HTTP_405", "HTTP_413", "HTTP_415", "HTTP_422", "HTTP_429"):
                self.ledger.update(key, "failed")
                raise
            # Any lost/invalid response may follow accepted execution. Fail closed and keep slot.
            self.ledger.update(key, "unknown")
            if self.config.get("external_access") is True:
                return self.reconcile(key, capability, principal["bearer_token"], identity)
            raise BridgeError("SUBMISSION_UNKNOWN", "提交结果未知；不会自动再次提交。请由管理员核对后恢复。") from error
        return self.poll(key, operation, principal["bearer_token"], identity)

    def reconcile(self, key, capability, token, identity):
        # Safe read only: a 404 is not permission to replay the original POST.
        self.principal(identity, token)
        try:
            status, body = self.transport.request("GET", "/api/v1/requests/" + capability + "/" + key, token)
            self.principal(identity, token)
            operation = body.get("id")
            if status != 200 or not isinstance(operation, str) or not OPERATION.fullmatch(operation):
                raise BridgeError("SUBMISSION_UNKNOWN")
            if capability == "economic.query":
                if body.get("state") != "succeeded" or body.get("resultUrl") != "/api/v1/operations/" + operation + "/result":
                    raise BridgeError("SUBMISSION_UNKNOWN")
                status, result = self.transport.request("GET", body["resultUrl"], token)
                self.principal(identity, token)
                if status != 200 or not isinstance(result, dict):
                    raise BridgeError("SUBMISSION_UNKNOWN")
                value = {"ok": True, "result": result}
                self.ledger.update(key, "succeeded", operation=operation, result=value)
                return value
            self.ledger.update(key, "pending", operation=operation)
            return self.poll(key, operation, token, identity)
        except BridgeError as error:
            if error.code in ("PRINCIPAL_FORBIDDEN", "LOCAL_AUTHORIZATION_CHANGED", "HTTP_401", "HTTP_403"):
                raise
            raise BridgeError("SUBMISSION_UNKNOWN", "提交状态仍待核实；未重新提交。") from None

    def poll(self, key, operation, token, identity):
        if not OPERATION.fullmatch(operation):
            raise BridgeError("INVALID_OPERATION")
        path = "/api/v1/operations/" + operation
        deadline, last_error = time.monotonic() + self.wait_seconds, None
        while True:
            try:
                self.principal(identity, token)
                status, body = self.transport.request("GET", path, token)
                self.principal(identity, token)
                if status != 200 or body.get("id") != operation:
                    raise BridgeError("INVALID_API_RESPONSE")
                if body.get("state") in ("failed", "cancelled"):
                    code = (body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else None
                    safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code) else "CAPTURE_FAILED"
                    self.ledger.update(key, "failed", result={"ok": False, "error": safe_code})
                    raise BridgeError("CAPTURE_FAILED")
                if body.get("state") == "succeeded":
                    if body.get("resultUrl") != path + "/result":
                        raise BridgeError("INVALID_RESULT_URL")
                    self.principal(identity, token)
                    status, data = self.transport.request("GET", path + "/result", token, png=True)
                    self.principal(identity, token)
                    if status != 200:
                        raise BridgeError("INVALID_API_RESPONSE")
                    validate_png(data)
                    metadata = body.get("result") if isinstance(body.get("result"), dict) else {}
                    digest = metadata.get("sha256")
                    if digest is not None and digest != hashlib.sha256(data).hexdigest():
                        raise BridgeError("RESULT_DIGEST_MISMATCH")
                    target = self.artifacts / (uuid.uuid4().hex + ".png")
                    fd = secure_open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "wb") as file:
                        file.write(data)
                    try:
                        self.principal(identity, token)
                    except BridgeError:
                        target.unlink()
                        raise
                    result = {"ok": True, "image": str(target), "media": "MEDIA:" + str(target),
                              "label": metadata.get("label"), "capturedAt": metadata.get("capturedAt"),
                              "message": "在回复中原样包含 media 指令以发送原生图片；需要分析时对同一 image 使用 vision。"}
                    self.ledger.update(key, "succeeded", result=result)
                    self.cleanup_artifacts()
                    return result
                if body.get("state") not in ("pending", "queued", "dispatching", "capturing", "cancelling", "unknown"):
                    raise BridgeError("INVALID_OPERATION_STATE")
            except BridgeError as error:
                if error.code == "CAPTURE_FAILED":
                    raise
                # GET is safe to retry, but authentication/revocation/expiry is terminal locally.
                if error.code in ("HTTP_401", "HTTP_403", "HTTP_404", "HTTP_410", "PRINCIPAL_FORBIDDEN", "LOCAL_AUTHORIZATION_CHANGED", "INVALID_CONFIGURATION", "PRIVATE_FILE_REQUIRED"):
                    raise
                last_error = error
            if time.monotonic() >= deadline:
                # Keep pending durable slot; a repeated same message may resume GET only.
                raise BridgeError("POLL_PENDING", "任务状态待确认；不会重新提交截图。") from last_error
            time.sleep(1)
