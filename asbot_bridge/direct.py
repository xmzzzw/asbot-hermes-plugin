"""Admitted Weixin numeric commands: no agent turn, no new transport instance."""
import asyncio
from dataclasses import replace
import hashlib
from .private_io import secure_open, lock_exclusive
import os
import uuid
import weakref
import json
from pathlib import Path
import re
import time

from .bridge import Bridge, BridgeError, Identity, private_file, validate_png
from .quote_store import QuoteStore, message_id
from .native_send import send_image


def commands(text):
    """A complete, bounded command grammar, never numbers embedded in prose."""
    if not isinstance(text, str) or len(text) > 256:
        return None
    text = re.sub(r"^/(?:asbot|截图)\s+", "", text.strip(), flags=re.I)
    token = r"(?:(?:BTC|截图)\s*)?([0-9])"
    if not re.fullmatch(token + r"(?:[\s,，、]+" + token + r")*", text.strip(), re.I):
        return None
    return tuple(re.findall(token, text.strip(), re.I))


def command(text):
    values = commands(text)
    return values[0] if values and len(values) == 1 else None


def control(text):
    if not isinstance(text, str):
        return None
    value = text.strip()
    value = re.sub(r"^/asbot\s+", "", value, flags=re.I)
    if value in ("help", "截图帮助", "/截图", "/asbot"):
        return "help"
    if value in ("status", "截图状态", "/截图 状态"):
        return "status"
    if value in ("quota", "截图额度"):
        return "quota"
    match = re.fullmatch(r"(?:calendar|日历|经济日历)\s*(今天|明天|本周|本月|下一个)?", value, re.I)
    return "calendar:" + (match[1] or "今天") if match else None


HELP = ("截图：直接发 0–9 或 BTC0–BTC9。\n"
        "连续发送 1、2，或一次发 1 2，都会逐条排队回图；最多 5 条。\n"
        "数字截图不调用 AI；需要分析时引用图片提问。\n"
        "发‘截图状态’查看本会话队列。")


def admitted_identity(profile, platform, source, message_id, session_key):
    # Only the official post-admission hook calls this function. Model tools keep
    # their separate strict ContextVar check; text never supplies routing identity.
    if (platform != "weixin" or not isinstance(source, dict)
            or (source.get("profile") or "default") != profile
            or source.get("chat_type") != "dm" or not session_key):
        raise BridgeError("DIRECT_MESSAGE_REQUIRED")
    values = [source.get("user_id"), source.get("chat_id"), message_id]
    if any(not isinstance(v, str) or not v or len(v) > 512
           or any(ord(c) < 32 for c in v) for v in values):
        raise BridgeError("ADMITTED_MESSAGE_REQUIRED")
    return Identity(profile, platform, *values)


def live_adapter(profile):
    from gateway.config import Platform
    from hermes_cli.profiles import get_active_profile_name
    from tools.send_message_senders import _live_adapter
    if (get_active_profile_name() or "default") != profile:
        raise BridgeError("SESSION_FORBIDDEN")
    runner, adapter = _live_adapter(Platform.WEIXIN)
    # No standalone fallback, default-profile fallback or connect/login/consumer.
    if runner is None or adapter is None or not callable(getattr(runner, "_authorization_adapter", None)):
        raise BridgeError("LIVE_ADAPTER_REQUIRED")
    return adapter


def principal_key(identity):
    return hashlib.sha256(json.dumps([identity.profile, identity.platform,
                                     identity.user_id, identity.chat_id]).encode()).hexdigest()


class Deliveries:
    def __init__(self, bridge):
        self.bridge = bridge
        self.owner = uuid.uuid4().hex
        self.owner_path = bridge.ledger.root / ("direct-owner-" + self.owner + ".lock")
        self.owner_fd = secure_open(self.owner_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        lock_exclusive(self.owner_fd)
        self._close_owner = weakref.finalize(self, os.close, self.owner_fd)
        with bridge.ledger.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS direct_deliveries (
                key TEXT PRIMARY KEY, principal TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, image TEXT,
                capture_seconds REAL, send_seconds REAL)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(direct_deliveries)")}
            for name in ("shortcut", "error_code", "owner"):
                if name not in columns:
                    db.execute("ALTER TABLE direct_deliveries ADD COLUMN " + name + " TEXT")

    def reap_ended_owners(self, db):
        # A free OS lock proves that owner's in-process tasks have ended. Never
        # infer death from wall time/PID or delete/replace lockfiles to acquire it.
        for row in db.execute("SELECT DISTINCT owner FROM direct_deliveries WHERE state IN ('accepted','capturing','sending')").fetchall():
            owner = row[0]
            if owner == self.owner or not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
                continue  # pre-ownership records require explicit operator evidence
            path = self.bridge.ledger.root / ("direct-owner-" + owner + ".lock")
            fd = None
            try:
                private_file(path)
                fd = secure_open(path, os.O_RDWR)
                lock_exclusive(fd)
                db.execute("UPDATE direct_deliveries SET state='unknown',updated=?,error_code='OWNER_ENDED' WHERE owner=? AND state IN ('accepted','capturing','sending')",
                           (time.time(), owner))
            except (OSError, BridgeError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)

    def task_ended(self, key):
        # Runs even if a supervised task was cancelled before its first step.
        with self.bridge.ledger.connect() as db:
            db.execute("UPDATE direct_deliveries SET state='unknown',updated=?,error_code='TASK_ENDED' WHERE key=? AND state IN ('accepted','capturing','sending')",
                       (time.time(), key))

    def claim(self, identity):
        return self.claim_many([(identity, None)])[0]

    def claim_many(self, entries):
        if not 1 <= len(entries) <= 5:
            raise BridgeError("DIRECT_QUEUE_FULL")
        keys = [hashlib.sha256((principal_key(i) + ":" + i.message_id).encode()).hexdigest()
                for i, _ in entries]
        with self.bridge.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if any(db.execute("SELECT 1 FROM direct_deliveries WHERE key=?", (k,)).fetchone() for k in keys):
                raise BridgeError("DIRECT_ALREADY_RECORDED")
            # Unknown image sends are never replayed but do not occupy a live task.
            # An unresolved server operation independently retains the Ledger slot.
            self.reap_ended_owners(db)
            active = db.execute("SELECT count(*) FROM direct_deliveries WHERE state IN ('accepted','capturing','sending')").fetchone()[0]
            if active + len(entries) > 5:
                raise BridgeError("DIRECT_QUEUE_FULL")
            now = time.time()
            for key, (identity, shortcut) in zip(keys, entries):
                db.execute("INSERT INTO direct_deliveries (key,principal,state,created,updated,shortcut,owner) VALUES (?,?,?,?,?,?,?)",
                           (key, principal_key(identity), "accepted", now, now, shortcut, self.owner))
            db.commit()
        return keys

    def turn(self, key):
        with self.bridge.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.reap_ended_owners(db)
            first = db.execute("SELECT key FROM direct_deliveries WHERE state IN ('accepted','capturing','sending') ORDER BY created,rowid LIMIT 1").fetchone()
            if first is None or first[0] != key:
                return False
            return bool(db.execute("UPDATE direct_deliveries SET state='capturing',updated=? WHERE key=? AND state='accepted'",
                                   (time.time(), key)).rowcount)

    def update(self, key, state, *, image=None, capture=None, send=None, error=None):
        with self.bridge.ledger.connect() as db:
            db.execute("UPDATE direct_deliveries SET state=?,updated=?,image=COALESCE(?,image),capture_seconds=COALESCE(?,capture_seconds),send_seconds=COALESCE(?,send_seconds),error_code=? WHERE key=?",
                       (state, time.time(), image, capture, send, error, key))

    def status(self, identity):
        self.bridge.principal(identity)
        with self.bridge.ledger.connect() as db:
            rows = db.execute("SELECT shortcut,state FROM direct_deliveries WHERE principal=? ORDER BY created DESC,rowid DESC LIMIT 5",
                              (principal_key(identity),)).fetchall()
        labels = {"accepted": "排队中", "capturing": "截图中", "sending": "发送中",
                  "sent": "已发送", "failed": "失败", "unknown": "送达待确认（不自动重发）"}
        return "\n".join("BTC" + (r[0] or "?") + "：" + labels.get(r[1], "待确认") for r in reversed(rows)) or "当前没有截图请求。"

    def latest(self, identity):
        self.bridge.principal(identity)
        with self.bridge.ledger.connect() as db:
            row = db.execute("SELECT image FROM direct_deliveries WHERE principal=? AND state='sent' AND updated>? ORDER BY updated DESC LIMIT 1",
                             (principal_key(identity), time.time() - 540)).fetchone()
        if not row or not row[0]:
            raise BridgeError("RECENT_IMAGE_UNAVAILABLE", "最近截图已过期或尚未发送；请重新发送数字截图指令。")
        path = Path(row[0])
        if path.parent != self.bridge.artifacts or not re.fullmatch(r"[a-f0-9]{32}\.png", path.name):
            raise BridgeError("INVALID_ARTIFACT")
        private_file(path)
        validate_png(path.read_bytes())
        self.bridge.principal(identity)
        return {"ok": True, "image": str(path), "message": "对这张已发送的截图使用 vision_analyze；不要重新截图，不要重复发送图片。"}


async def capture_and_send(bridge, identity, shortcut, adapter, deliveries, key):
    started = time.monotonic()
    sending = False
    try:
        token = bridge.principal(identity)["bearer_token"]
        while not deliveries.turn(key):
            bridge.principal(identity, token)
            if time.monotonic() - started >= 600:
                raise BridgeError("DIRECT_QUEUE_TIMEOUT")
            await asyncio.sleep(0.1)
        started = time.monotonic()
        result = await asyncio.to_thread(bridge.invoke, identity, "screenshot.capture", {"shortcut": shortcut})
        if not result.get("ok") or not result.get("image") or result.get("duplicate"):
            raise BridgeError("DIRECT_CAPTURE_NOT_DELIVERABLE")
        await asyncio.to_thread(bridge.authorize_delivery, identity, token)
        image = result["image"]
        capture = time.monotonic() - started
        deliveries.update(key, "sending", image=image, capture=capture)
        sending = True  # unknown outcome is never automatically sent again
        result = await asyncio.wait_for(send_image(adapter,
            chat_id=identity.chat_id, image_path=image, caption=(result.get("label") or "BTC" + shortcut) + (" · " + str(result["capturedAt"]) if result.get("capturedAt") else ""),
            authorize=lambda: asyncio.to_thread(bridge.authorize_delivery, identity, token)), timeout=60)
        if not result or not result.success:
            deliveries.update(key, "unknown")
            return
        deliveries.update(key, "sent", send=time.monotonic() - started - capture)
        if message_id(getattr(result, "message_id", None)):
            # Cache failure never replays a successfully delivered image.
            try:
                bridge.principal(identity, token)
                QuoteStore(bridge).put(identity, adapter._account_id, result.message_id, image,
                                       expected_digest=result.image_digest)
            except Exception:
                pass
    except asyncio.CancelledError:
        deliveries.update(key, "unknown")
        raise
    except Exception as error:
        code = error.code if isinstance(error, BridgeError) else "DIRECT_ERROR"
        deliveries.update(key, "unknown" if sending else "failed", error=code)
        if not sending:
            # Only this admitted, still-authorized peer may receive the bounded
            # failure notice. No sensitive exception/HTTP body is forwarded.
            try:
                bridge.principal(identity)
                await asyncio.wait_for(adapter.send(identity.chat_id,
                    "BTC" + shortcut + {
                        "CAPTURE_FAILED": " 截图后端未完成本次任务；后续排队会继续。可重新发送此指令。",
                        "DIRECT_QUEUE_TIMEOUT": " 排队超时，尚未提交本次截图。",
                        "SUBMISSION_UNKNOWN": " 提交状态待确认，不会重复提交；请先查看截图状态。",
                        "POLL_PENDING": " 截图仍待确认，不会重复提交。",
                    }.get(code, " 截图未完成，请稍后重试；状态未知时不会自动重复提交。")), timeout=15)
            except Exception:
                pass


async def control_reply(bridge, identity, adapter, operation):
    try:
        if operation == "help":
            result = await asyncio.to_thread(bridge.service_get, identity, "screenshot-templates")
            reply = "\n".join("BTC" + str(t["shortcut"]) + "：" + t["name"] + ("" if t.get("available") else "（暂不可用）") for t in result.get("templates", [])[:10])
        elif operation == "quota":
            result = await asyncio.to_thread(bridge.service_get, identity, "me")
            reply = "\n".join(name + "：已用 " + str(q["used"]) + "，预占 " + str(q["reserved"]) + "，上限 " + str(q["limit"]) for name, q in result.get("quotas", {}).items())
            reply += "\n重置：" + str(result.get("resetAt", "未知"))
            if result.get("authorized") is not True:
                reply += "\n资格已停用：" + str(result.get("reasonCode", "UNKNOWN"))
        else:
            result = await asyncio.to_thread(bridge.invoke, identity, "economic.query", {"kind": operation.split(":", 1)[1]})
            reply = json.dumps(result.get("result", result), ensure_ascii=False, indent=2)[:12000]
        bridge.principal(identity)
        await asyncio.wait_for(adapter.send(identity.chat_id, reply or "当前无可用项目。"), 15)
    except Exception:
        try:
            bridge.principal(identity)
            await asyncio.wait_for(adapter.send(identity.chat_id, "AS-BOT 查询未完成，请检查资格或稍后重试。"), 15)
        except Exception:
            pass


def accept(ctx, config_path, profile, source, message_id, session_key, platform, text, *, adapter=None):
    if platform != "weixin":
        return None
    shortcuts = commands(text)
    operation = control(text)
    if shortcuts is None and operation is None:
        return None
    try:
        identity = admitted_identity(profile, platform, source, message_id, session_key)
        bridge = Bridge.from_file(config_path)
        bridge.principal(identity)
        adapter = adapter if adapter is not None else live_adapter(profile)
        deliveries = Deliveries(bridge)
        if operation:
            if operation == "status":
                return {"action": "handled", "reply": deliveries.status(identity)}
            if bridge.config.get("external_access") is not True and operation == "help":
                return {"action": "handled", "reply": HELP}
            task = control_reply(bridge, identity, adapter, operation)
            try:
                ctx.spawn_task(task, name="asbot-direct-control")
            except Exception:
                task.close()
                raise
            return {"action": "handled", "reply": "正在查询 AS-BOT。"}
        entries = [(identity if len(shortcuts) == 1 else replace(identity,
                    message_id="batch:" + hashlib.sha256(identity.message_id.encode()).hexdigest() + ":" + str(n)), shortcut)
                   for n, shortcut in enumerate(shortcuts)]
        keys = deliveries.claim_many(entries)
        for key, (item, shortcut) in zip(keys, entries):
            task = capture_and_send(bridge, item, shortcut, adapter, deliveries, key)
            try:
                spawned = ctx.spawn_task(task, name="asbot-direct-capture")
                if hasattr(spawned, "add_done_callback"):
                    spawned.add_done_callback(lambda _task, owned_key=key: deliveries.task_ended(owned_key))
            except Exception:
                task.close()
                deliveries.update(key, "failed")
                # No network work has started for these unspawned entries.
                for remaining in keys[keys.index(key) + 1:]:
                    deliveries.update(remaining, "failed")
                raise
        labels = "、".join("BTC" + s for s in shortcuts)
        return {"action": "handled", "reply": "已接收 " + labels + "，按顺序排队，完成后直接发送图片。"}
    except BridgeError as error:
        message = {"DIRECT_ALREADY_RECORDED": "该消息已处理，不重复截图或发送。",
                   "DIRECT_QUEUE_FULL": "截图队列繁忙，请稍后发送新的截图指令。"}.get(error.code,
                   "截图指令暂不可用，请检查当前微信的桥接授权与连接。")
        return {"action": "handled", "reply": message}
    except Exception:
        return {"action": "handled", "reply": "截图指令暂不可用，请稍后重试。"}
