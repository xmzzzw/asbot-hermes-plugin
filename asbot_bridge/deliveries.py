"""Persistent delivery ownership shared by the public and legacy transports.

This module imports no Hermes adapter, private protocol or native upload helper.
Existing rows and owner locks remain authoritative across plugin upgrades.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
import weakref
from .private_io import secure_open, lock_exclusive
from .bridge import BridgeError, private_file, validate_png


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
            if "physical_pending" not in columns:
                db.execute("ALTER TABLE direct_deliveries ADD COLUMN physical_pending INTEGER NOT NULL DEFAULT 0")

    def reap_ended_owners(self, db):
        # A free OS lock proves that owner's in-process tasks have ended. Never
        # infer death from wall time/PID or delete/replace lockfiles to acquire it.
        for row in db.execute("SELECT DISTINCT owner FROM direct_deliveries WHERE state IN ('accepted','capturing','sending') OR physical_pending=1").fetchall():
            owner = row[0]
            if owner == self.owner or not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
                continue  # pre-ownership records require explicit operator evidence
            path = self.bridge.ledger.root / ("direct-owner-" + owner + ".lock")
            fd = None
            try:
                private_file(path)
                fd = secure_open(path, os.O_RDWR)
                lock_exclusive(fd)
                db.execute("UPDATE direct_deliveries SET state='unknown',physical_pending=0,updated=?,error_code='OWNER_ENDED' WHERE owner=? AND (state IN ('accepted','capturing','sending') OR physical_pending=1)",
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
            active = db.execute("SELECT count(*) FROM direct_deliveries WHERE state IN ('accepted','capturing','sending') OR physical_pending=1").fetchone()[0]
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
            first = db.execute("SELECT key FROM direct_deliveries WHERE state IN ('accepted','capturing','sending') OR physical_pending=1 ORDER BY created,rowid LIMIT 1").fetchone()
            if first is None or first[0] != key:
                return False
            return bool(db.execute("UPDATE direct_deliveries SET state='capturing',updated=? WHERE key=? AND state='accepted'",
                                   (time.time(), key)).rowcount)

    def update(self, key, state, *, image=None, capture=None, send=None, error=None, physical_pending=None):
        with self.bridge.ledger.connect() as db:
            db.execute("UPDATE direct_deliveries SET state=?,updated=?,image=COALESCE(?,image),capture_seconds=COALESCE(?,capture_seconds),send_seconds=COALESCE(?,send_seconds),error_code=?,physical_pending=COALESCE(?,physical_pending) WHERE key=?",
                       (state, time.time(), image, capture, send, error, physical_pending, key))

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
