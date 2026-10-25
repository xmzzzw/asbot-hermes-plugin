"""Exact server-ID image references; no raw IDs or cross-conversation fallback."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .private_io import secure_open
from .bridge import BridgeError, private_dir, private_file, validate_png

RETENTION_SECONDS = 7 * 86400


def message_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,128}", value) or int(value) == 0:
        return None
    return value


def reference_ids(raw):
    items = raw.get("item_list") if isinstance(raw, dict) else None
    result = []
    for item in (items if isinstance(items, list) else [])[:16]:
        ref = item.get("ref_msg") if isinstance(item, dict) else None
        if not isinstance(ref, dict):
            continue
        nested = ref.get("message_item")
        candidates = [message_id(ref.get("svr_id")),
                      message_id(nested.get("msg_id")) if isinstance(nested, dict) else None]
        result.extend(v for v in candidates if v)
    return list(dict.fromkeys(result))


class QuoteStore:
    def __init__(self, bridge):
        self.bridge = bridge

    def key(self, identity, account, server_id):
        self.bridge.principal(identity)
        value = message_id(server_id)
        if not value or not isinstance(account, str) or not 0 < len(account) <= 512:
            raise BridgeError("INVALID_REFERENCE_ID")
        fields = [identity.profile, identity.platform, identity.user_id, identity.chat_id, account, value]
        namespace = self.bridge.installation_namespace(identity)
        if namespace is not None:
            fields.insert(0, namespace)
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    def image(self, path, *, cached=False):
        path = Path(path)
        parent = self.bridge.ledger.root / "quoted-artifacts" if cached else self.bridge.artifacts
        if path.parent != parent or not re.fullmatch(r"[0-9a-f]{32}\.png", path.name):
            raise BridgeError("INVALID_ARTIFACT")
        private_file(path)
        data = path.read_bytes()
        validate_png(data)
        return str(path), hashlib.sha256(data).hexdigest()

    def put(self, identity, account, server_id, image, *, expected_digest=None):
        key = self.key(identity, account, server_id)
        path, digest = self.image(image)
        if expected_digest is not None and digest != expected_digest:
            raise BridgeError("REFERENCE_IMAGE_CHANGED")
        cache = self.bridge.ledger.root / "quoted-artifacts"
        private_dir(cache)
        target = cache / (uuid.uuid4().hex + ".png")
        removals = []
        created = False
        try:
            with self.bridge.ledger.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS quoted_images (key TEXT PRIMARY KEY,image TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)")
                existing = db.execute("SELECT image,digest,created FROM quoted_images WHERE key=?", (key,)).fetchone()
                if existing:
                    if existing[2] <= time.time()-RETENTION_SECONDS:
                        raise BridgeError("REFERENCE_EXPIRED")
                    if existing[1] != digest:
                        raise BridgeError("REFERENCE_ID_CONFLICT")
                    self.image(existing[0], cached=True)
                    return existing[0]
                data = Path(path).read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise BridgeError("REFERENCE_IMAGE_CHANGED")
                fd = secure_open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                created = True
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                db.execute("INSERT INTO quoted_images VALUES (?,?,?,?)", (key, str(target), digest, time.time()))
                removals = db.execute("SELECT key,image FROM quoted_images WHERE created<? OR key NOT IN (SELECT key FROM quoted_images ORDER BY created DESC LIMIT 256)", (time.time()-RETENTION_SECONDS,)).fetchall()
                removed = {r[0] for r in removals}
                retained_bytes = 0
                for candidate in db.execute("SELECT key,image FROM quoted_images ORDER BY created DESC,key DESC").fetchall():
                    if candidate[0] in removed:
                        continue
                    candidate_path = Path(candidate[1])
                    if candidate_path.parent != cache or not re.fullmatch(r"[0-9a-f]{32}\.png", candidate_path.name):
                        raise BridgeError("INVALID_ARTIFACT")
                    if not candidate_path.exists() and not candidate_path.is_symlink():
                        removals.append(candidate)
                        continue
                    private_file(candidate_path)
                    retained_bytes += candidate_path.stat().st_size
                    if retained_bytes > 512 * 1024 * 1024:
                        removals.append(candidate)
                db.executemany("DELETE FROM quoted_images WHERE key=?", [(r[0],) for r in removals])
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        for _, name in removals:
            candidate = Path(name)
            if candidate.parent == cache and re.fullmatch(r"[0-9a-f]{32}\.png", candidate.name):
                candidate.unlink(missing_ok=True)
        self.bridge.cleanup_artifacts()
        return str(target)

    def find(self, identity, account, server_id):
        key = self.key(identity, account, server_id)
        with self.bridge.ledger.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='quoted_images'").fetchone()
            row = db.execute("SELECT image,digest FROM quoted_images WHERE key=? AND created>?", (key, time.time()-RETENTION_SECONDS)).fetchone() if exists else None
        if not row:
            return None
        try:
            path, digest = self.image(row[0], cached=True)
        except (BridgeError, OSError):
            return None
        self.bridge.principal(identity)
        return path if digest == row[1] else None
