"""Refresh private directory metadata and publish process-bound channel readiness."""
import asyncio
from contextlib import contextmanager
from .private_io import secure_open
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .bridge import Bridge, BridgeError, Identity, private_dir


def version(root):
    found=re.search(r"(?m)^version: (\d{1,4}\.\d{1,4}\.\d{1,4})$", (root/"plugin.yaml").read_text())
    if not found:
        raise ValueError("INVALID_PLUGIN_VERSION")
    return found[1]


def receipt_path(config,profile):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",profile):
        raise ValueError("INVALID_PROFILE")
    return Path(config).parent/("managed-ready-"+profile+".json")


def atomic_json(path,value):
    private_dir(path.parent)
    fd,temp=tempfile.mkstemp(prefix=".managed-",dir=path.parent)
    try:
        with os.fdopen(fd,"w") as out:
            json.dump(value,out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextmanager
def admission(root, *, exclusive=False):
    if os.name!="posix":
        if exclusive:raise BridgeError("UPDATE_LOCK_UNAVAILABLE")
        yield
        return
    import fcntl
    fd=secure_open(root/"managed-admission.lock",os.O_CREAT|os.O_RDWR)
    try:
        try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        except BlockingIOError:raise BridgeError("UPDATE_IN_PROGRESS") from None
        yield
    finally:
        os.close(fd)


def device(bridge, profile=None):
    rows=[p for p in bridge.config["principals"] if p["enabled"] and (profile is None or p["profile"]==profile)]
    if not rows or bridge.config.get("external_access") is not True:
        raise BridgeError("MANAGED_ACCESS_REQUIRED")
    p=rows[0]
    identity=Identity(p["profile"],"weixin",p["user_id"],p["chat_id"],"managed")
    return bridge.principal(identity)["bearer_token"]


def lookup(bridge,identity,args):
    if not isinstance(args,dict) or len(args)!=1 or set(args)-{"query","shortcut"}:
        raise BridgeError("INVALID_INPUT")
    token=bridge.principal(identity)["bearer_token"]
    try:
        _,result=bridge.transport.request("POST","/api/v1/knowledge/search",token,args,timeout=5)
        bridge.principal(identity,token)
        return result
    finally:
        bridge.transport.close()


async def monitor(lane):
    observed_version=version(Path(__file__).parent)
    path=receipt_path(lane.config_path,lane.profile)
    revision=synced=None

    def refresh():
        bridge=Bridge.from_file(lane.config_path)
        try:
            return bridge.transport.request("GET","/api/v1/catalog",device(bridge,lane.profile),revision=revision,timeout=4)
        finally:
            bridge.transport.close()

    while not lane.closed:
        try:
            status,data=await asyncio.to_thread(refresh)
            if status==200:
                if data.get("schema")!="as-bot.external-catalog/v1" or not re.fullmatch(r"[a-f0-9]{64}",data.get("revision","")):
                    raise BridgeError("INVALID_CATALOG")
                revision=data["revision"]
                lane.catalog=data
            synced=int(time.time()*1000)
            lane.catalog_synced=synced
        except BridgeError as e:
            if e.code in ("HTTP_401","HTTP_403"):
                revision=synced=None
                lane.catalog=None
                lane.catalog_synced=None
        except Exception:
            pass
        if not lane.closed:
            try:
                atomic_json(path,{"version":observed_version,"pid":os.getpid(),"checked":time.time(),
                    "connected":bool(lane.adapter and lane.adapter.is_connected),"busy":bool(lane.jobs),
                    "catalogRevision":revision,"syncedAt":synced})
            except (OSError,ValueError):
                pass
        await asyncio.sleep(20)
