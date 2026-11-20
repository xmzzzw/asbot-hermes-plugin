"""Signed standalone updater; runs outside Hermes and never changes credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import ssl
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from asbot_bridge.bridge import Bridge, private_file, private_dir
from asbot_bridge.managed import admission, atomic_json, device, receipt_path, version

REPO="https://github.com/xmzzzw/asbot-hermes-plugin"
MAX_ZIP=4*1024*1024
VERSION=r"\d{1,4}\.\d{1,4}\.\d{1,4}"
HEX=r"[a-f0-9]{64}"


def decode(text):
    return base64.urlsafe_b64decode(text+"="*(-len(text)%4))


def manifest(offer,key,current,hermes,pilot,now):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    if not isinstance(offer,dict) or set(offer)!={"payload","signature"}:
        raise ValueError("UPDATE_SIGNATURE_INVALID")
    payload,signature=offer["payload"],offer["signature"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}",payload) or not re.fullmatch(r"[A-Za-z0-9_-]{86}",signature):
        raise ValueError("UPDATE_SIGNATURE_INVALID")
    try:
        body=decode(payload)
        load_pem_public_key(key).verify(decode(signature),body)
        m=json.loads(body)
    except Exception:
        raise ValueError("UPDATE_SIGNATURE_INVALID") from None
    if (set(m)!={"schema","version","sourceSha","zipSha256","channel","hermesVersion","expiresAt"}
            or m["schema"]!="as-bot.hermes-release/v1" or not re.fullmatch(VERSION,m["version"])
            or not re.fullmatch(r"[a-f0-9]{40}",m["sourceSha"]) or not re.fullmatch(HEX,m["zipSha256"])
            or m["hermesVersion"]!=hermes or m["channel"] not in (("stable","pilot") if pilot else ("stable",))
            or type(m["expiresAt"]) is not int or not now<m["expiresAt"]<=now+90*86400000
            or tuple(map(int,m["version"].split(".")))<=tuple(map(int,current.split(".")))):
        raise ValueError("UPDATE_MANIFEST_INVALID")
    return m


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections=2
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        url=urllib.parse.urlsplit(newurl)
        if url.scheme!="https" or url.netloc not in ("release-assets.githubusercontent.com","objects.githubusercontent.com"):
            raise ValueError("UPDATE_DOWNLOAD_REDIRECT_DENIED")
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(m,target,installer):
    tag="v"+m["version"]
    url=REPO+"/releases/download/"+tag+"/asbot-hermes-plugin-"+tag+".zip"
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),AssetRedirect())
    archive=target/"payload.zip"
    with opener.open(url,timeout=10) as remote, archive.open("xb") as out:
        size=0;digest=hashlib.sha256();deadline=time.monotonic()+45
        while True:
            chunk=remote.read(65536)
            if not chunk:break
            size+=len(chunk)
            if size>MAX_ZIP or time.monotonic()>deadline:raise ValueError("UPDATE_DOWNLOAD_LIMIT")
            digest.update(chunk);out.write(chunk)
    if digest.hexdigest()!=m["zipSha256"]:raise ValueError("UPDATE_CHECKSUM_INVALID")
    return unpack(archive,target,installer,m)


def unpack(archive,target,installer,m):
    with zipfile.ZipFile(archive) as z:
        names=set();total=0
        for info in z.infolist():
            name=info.filename;parts=name.split("/")
            if (len(names)>=128 or name.casefold() in {n.casefold() for n in names} or len(parts)>2 or
                    any(not re.fullmatch(r"[A-Za-z0-9._-]{1,80}",p) or p.endswith(".") or
                        re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])",p.split(".")[0]) for p in parts)
                    or stat.S_IFMT(info.external_attr>>16) not in (0,stat.S_IFREG)):
                raise ValueError("UPDATE_ARCHIVE_INVALID")
            total+=info.file_size
            if total>MAX_ZIP:raise ValueError("UPDATE_ARCHIVE_LIMIT")
            names.add(name)
            path=target/name;path.parent.mkdir(mode=0o700,exist_ok=True)
            with path.open("xb") as f:f.write(z.read(info))
    p=installer.verify_payload(target)
    if p.get("clientVersion")!=m["version"] or p["sha"]!=m["sourceSha"] or names!=set(p["files"])|{"release.json"}:
        raise ValueError("UPDATE_PACKAGE_IDENTITY_MISMATCH")
    return p


def read(path, default=None):
    if not path.exists() and not path.is_symlink():return default
    private_file(path)
    return json.loads(path.read_text())


def installer_version(receipt):
    versions={version(Path(r["plugin"])) for r in receipt["profiles"]}
    if len(versions)!=1:raise ValueError("PROFILE_VERSION_MISMATCH")
    return versions.pop()


def enable(args,directory,state,installer):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    root=installer.ROOT
    package=installer.verify_payload(root)
    if "release-key.pem" not in package["files"]:raise ValueError("UPDATE_BOOTSTRAP_PACKAGE_REQUIRED")
    load_pem_public_key((root/"release-key.pem").read_bytes())
    receipt=installer.owned_receipt(directory/"install-receipt.json",state["installationId"])
    if tuple(map(int,installer_version(receipt).split(".")))<(1,6,0):raise ValueError("BOOTSTRAP_UPDATE_REQUIRED")
    boot=directory/"managed-bootstrap"
    if not boot.exists():
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            staged=Path(tmp)/"bootstrap";private_dir(staged)
            for name in [*package["files"],"release.json"]:
                dest=staged/name;dest.parent.mkdir(mode=0o700,exist_ok=True,parents=True)
                shutil.copyfile(root/name,dest)
            installer.verify_payload(staged)
            staged.rename(boot)
    else:
        private_dir(boot)
        installer.verify_payload(boot)
    prefs={"auto":True,"restart":args.restart_gateway,"pilot":args.pilot_updates}
    atomic_json(directory/"managed-settings.json",prefs)
    label="com.asbot.hermes-update."+hashlib.sha256(str(directory).encode()).hexdigest()[:20]
    agents=Path.home()/"Library/LaunchAgents"
    agents.mkdir(mode=0o700,exist_ok=True)
    installer.plugin_parent(agents)
    plist=agents/(label+".plist")
    definition={"Label":label,"ProgramArguments":[sys.executable,"-B",str(boot/"bin/plugin.py"),
        "managed-tick","--state-dir",str(directory)],"RunAtLoad":True,"StartInterval":25}
    if not plist.exists():installer.atomic(plist,plistlib.dumps(definition))
    private_file(plist)
    if plistlib.loads(plist.read_bytes())!=definition:raise ValueError("UPDATE_SCHEDULER_CHANGED")
    domain="gui/"+str(os.getuid())
    result=subprocess.run(["/bin/launchctl","bootstrap",domain,str(plist)],capture_output=True,timeout=10)
    if result.returncode and subprocess.run(["/bin/launchctl","print",domain+"/"+label],capture_output=True,timeout=5).returncode:
        raise ValueError("UPDATE_SCHEDULER_UNAVAILABLE")
    return {"stage":"UPDATER_ENABLED","restartAllowed":prefs["restart"],"pilot":prefs["pilot"]}


class Gateway:
    def __init__(self,state,client,receipt,*,before_restart=None):
        source=client.hermes_python_source()
        if source is not None and str(source) not in sys.path:sys.path.insert(0,str(source))
        from gateway.control_socket import query_gateway_control
        self.query=query_gateway_control
        self.source=source
        self.state=state
        self.client=client
        self.before_restart=before_restart
        self.home=Path(state["hermesRoot"])
        self.profiles={}
        for item in receipt["profiles"]:
            home=Path(item["path"]).parent
            name="default" if home==self.home else home.name
            if home!=self.home and home.parent!=self.home/"profiles":
                raise ValueError("INVALID_PROFILE")
            self.profiles[name]=home

    def status(self):
        output={}
        for name,home in self.profiles.items():
            row=self.query(home,"status",timeout=2)
            if not row and home!=self.home:
                row=self.query(self.home,"status",timeout=2)
                served=row and row.get("served_profiles")
                if not served or name not in served:row=None
            output[name]=row or {}
        return output

    def observations(self,config):
        return {name:read(receipt_path(config,name),{}) for name in self.profiles}

    def ready(self,config,want):
        statuses=self.status()
        rows=self.observations(config)
        return bool(rows) and all(rows[n].get("connected") is True and rows[n].get("version")==want and
            rows[n].get("pid")==s.get("answering_pid") and 0<=time.time()-rows[n].get("checked",0)<35
            for n,s in statuses.items())

    def idle(self,bridge):
        status=self.status()
        if not status or any(s.get("gateway_state")!="running" or s.get("active_agents")!=0 or s.get("restart_requested") is not False for s in status.values()):
            return False
        if bridge is None:
            return True  # first install has no plugin ledger
        with bridge.ledger.connect() as db:
            if db.execute("SELECT 1 FROM requests WHERE state IN ('queued','submitting','pending','unknown') LIMIT 1").fetchone():return False
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='direct_deliveries'").fetchone():
                if db.execute("SELECT 1 FROM direct_deliveries WHERE state IN ('accepted','capturing','sending') OR physical_pending=1 LIMIT 1").fetchone():return False
        return True

    def restart(self,bridge):
        statuses=self.status()
        if not self.idle(bridge) or len({s.get("answering_pid") for s in statuses.values()})!=1:return False
        pid=next(iter(statuses.values())).get("answering_pid")
        if type(pid) is not int or pid<=1:return False
        # The serving process can be a root multiplex Gateway or an independent
        # profile. Never restart a shared host which serves another profile.
        for home in dict.fromkeys([*self.profiles.values(),self.home]):
            identity=self.query(home,"identify",timeout=2) or {}
            if (identity.get("pid")==pid and identity.get("hermes_home")
                    and Path(identity["hermes_home"])==home):
                break
        else:return False
        own_profile="default" if home==self.home else home.name
        served=(self.query(home,"status",timeout=2) or {}).get("served_profiles")
        if served is None:served=[identity.get("profile")]
        if not isinstance(served,(dict,list)) or not served or not {own_profile,*served}<=self.profiles.keys():return False
        if os.name=="nt":
            return self.windows_restart(home,pid,bridge)
        if sys.platform not in ("linux","darwin"):return False
        suffix="" if own_profile=="default" else "-"+own_profile
        def supervised_pid():
            if sys.platform=="darwin":
                label="gui/"+str(os.getuid())+"/ai.hermes.gateway"+suffix
                value=subprocess.run(["/bin/launchctl","print",label],capture_output=True,text=True,timeout=5)
                found=re.search(r"(?m)^\s*pid = ([0-9]+)\s*$",value.stdout) if value.returncode==0 else None
                return int(found[1]) if found else None
            value=subprocess.run(["systemctl","--user","show","hermes-gateway"+suffix+".service",
                                  "--property=MainPID","--value"],capture_output=True,text=True,timeout=5)
            return int(value.stdout.strip()) if value.returncode==0 and value.stdout.strip().isdigit() else None
        if supervised_pid()!=pid or not self.idle(bridge):return False
        if self.before_restart:self.before_restart()
        # SIGUSR1 asks Hermes to drain and restart under its existing supervisor.
        os.kill(pid,signal.SIGUSR1)
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            next_pid=supervised_pid()
            if next_pid is not None and next_pid!=pid:
                return True
            time.sleep(1)
        return False

    def windows_restart(self,home,pid,bridge):
        # The native Gateway's login task is the only relaunch path here. A
        # detached/custom Gateway or unsupported upstream API stays manual.
        from hermes_cli import gateway_windows as win
        from gateway.status import get_running_pid
        if Path(win._hermes_home())!=home or not win.is_task_registered():return False
        name=win.get_task_name()
        if win.scheduled_task_drift(name) or get_running_pid(home/"gateway.pid",cleanup_stale=False)!=pid:return False
        if not self.idle(bridge):return False
        if self.before_restart:self.before_restart()
        if not win._drain_gateway_pid(pid,30):return False
        if get_running_pid(home/"gateway.pid",cleanup_stale=False) or self.query(home,"status",timeout=2):return False
        started=subprocess.run(["schtasks.exe","/Run","/TN",name],capture_output=True,timeout=10)
        if started.returncode:return False
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            live=(self.query(home,"status",timeout=2) or {}).get("answering_pid")
            if type(live) is int and live>1 and live!=pid:return True
            time.sleep(1)
        return False

    def reload(self):
        self.client.reload_existing_plugins(self.state)

    def wait(self,config,want,seconds=30):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if self.ready(config,want):return True
            time.sleep(1)
        return False


def backup(directory,receipt,incoming,installer,attempt,old,config):
    location=Path(tempfile.mkdtemp(prefix="managed-rollback-",dir=directory))
    before=directory/"install-receipt.json"
    installer.atomic(location/"receipt.json",before.read_bytes())
    new={name:hashlib.sha256(data).hexdigest() for name,data in incoming.items()}
    for index,item in enumerate(receipt["profiles"]):
        folder=location/str(index);private_dir(folder)
        for name in item["pluginHashes"]:
            installer.atomic(folder/name,(Path(item["plugin"])/name).read_bytes())
    intent={**attempt,"backup":location.name,"old":old,"newHashes":new,"phase":"applying",
            "config":str(config),"forwardRestart":False,"recoveryRestart":False}
    atomic_json(directory/"managed-intent.json",intent)
    return intent


def restore(directory,intent,installer):
    name=intent["backup"]
    if not re.fullmatch("managed-rollback-[A-Za-z0-9_-]+",name):raise ValueError("UPDATE_BACKUP_INVALID")
    location=directory/name;private_dir(location)
    before=location/"receipt.json";receipt=read(before)
    receipt_file=directory/"install-receipt.json"
    current=read(receipt_file)
    for field in ("installationId","privateRoot","config","configHash"):
        if current[field]!=receipt[field]:raise ValueError("UPDATE_RECOVERY_DRIFT")
    # Inspect every target before restoring any of them. Only old/new bytes
    # covered by this attempt's journal can be removed or replaced.
    for index,item in enumerate(receipt["profiles"]):
        plugin=Path(item["plugin"]);private_dir(plugin)
        hashes=item["pluginHashes"]
        actual=installer.plugin_hashes(plugin)
        if any(value not in (hashes.get(n),intent["newHashes"].get(n)) for n,value in actual.items()):
            raise ValueError("UPDATE_RECOVERY_DRIFT")
        for n,h in hashes.items():
            saved=location/str(index)/n;private_file(saved)
            if hashlib.sha256(saved.read_bytes()).hexdigest()!=h:raise ValueError("UPDATE_BACKUP_INVALID")
    for index,item in enumerate(receipt["profiles"]):
        plugin=Path(item["plugin"]);old=item["pluginHashes"]
        for n in installer.plugin_hashes(plugin).keys()-old.keys():(plugin/n).unlink()
        for n in old:installer.atomic(plugin/n,(location/str(index)/n).read_bytes())
        cache=plugin/"__pycache__"
        if cache.is_dir() and not cache.is_symlink():shutil.rmtree(cache)
    installer.atomic(receipt_file,before.read_bytes())
    installer.check_receipt(receipt)


def finish(directory,intent,result,code=None):
    atomic_json(directory/"managed-result.json",{"backup":intent.get("backup"),"digest":intent["digest"],"jobId":intent.get("jobId"),"state":result,"code":code})
    (directory/"managed-intent.json").unlink(missing_ok=True)


def recover(directory,intent,installer,gateway,bridge,prefs):
    restore(directory,intent,installer)
    gateway.reload()
    want=intent["old"]
    ok=gateway.wait(intent["config"],want,8) if tuple(map(int,want.split(".")))>=(1,6,0) else False
    if not ok and prefs["restart"] and not intent["recoveryRestart"]:
        intent["recoveryRestart"]=True
        atomic_json(directory/"managed-intent.json",intent)
        try:ok=gateway.restart(bridge) and gateway.wait(intent["config"],want)
        except (OSError,subprocess.SubprocessError):ok=False
    finish(directory,intent,"restored" if ok else "failed","UPDATE_RESTORED" if ok else "OLD_FILES_RESTORED_CHECK_GATEWAY")


def tick(directory,state,installer,client,prefs):
    hermes=client.native_compatibility()
    if hermes!="0.22.2":raise ValueError("HERMES_VERSION_NOT_QUALIFIED")
    file=directory/"install-receipt.json"
    intent=read(directory/"managed-intent.json")
    config=directory/"bridge.json"
    bridge=Bridge.from_file(config);token=device(bridge)
    try:
        if intent:
            previous=read(directory/"managed-result.json",{})
            if previous.get("backup")==intent["backup"] and previous.get("digest")==intent["digest"]:
                (directory/"managed-intent.json").unlink()
            else:
                receipt=read(file);gateway=Gateway(state,client,receipt)
                with admission(directory,exclusive=True):
                    if not gateway.idle(bridge):return {"stage":"RECOVERY_WAITING_FOR_IDLE"}
                    recover(directory,intent,installer,gateway,bridge,prefs)
        receipt=installer.owned_receipt(file,state["installationId"])
        old=installer_version(receipt);gateway=Gateway(state,client,receipt)
        result=read(directory/"managed-result.json",{})
        rows=gateway.observations(config)
        current=next(iter(rows.values()),{})
        report={"installedVersion":old,"runningVersion":old if gateway.ready(config,old) else None,
                "hermesVersion":hermes,"platform":"darwin-arm64","updater":True,"auto":prefs["auto"],"pilot":prefs["pilot"],
                "catalogRevision":current.get("catalogRevision"),"syncedAt":current.get("syncedAt"),
                "state":result.get("state","idle"),"code":result.get("code"),"jobId":result.get("jobId")}
        _,status=bridge.transport.request("POST","/api/v1/device/check-in",token,report,timeout=5)
        if not status.get("release"):return {"stage":"CHECKED"}
        m=manifest(status["release"],(installer.ROOT/"release-key.pem").read_bytes(),old,hermes,prefs["pilot"],int(time.time()*1000))
        job=status.get("request")
        last=read(directory/"managed-last-check.json",{})
        if not job and (not prefs["auto"] or last.get("digest")==m["zipSha256"] and time.time()-last["at"]<21600):
            return {"stage":"CHECKED"}
        if result.get("digest")==m["zipSha256"] and result.get("state") in ("failed","restored"):return {"stage":"HELD_FAILED_RELEASE"}
        if not gateway.idle(bridge):return {"stage":"GATEWAY_BUSY"}
        if not gateway.ready(config,old):return {"stage":"PREDECESSOR_NOT_READY"}
        attempt={"digest":m["zipSha256"],"jobId":job and job["id"]}
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            root=Path(tmp)
            try:p=download(m,root,installer)
            except (OSError,ValueError,zipfile.BadZipFile) as error:
                atomic_json(directory/"managed-last-check.json",{"digest":m["zipSha256"],"at":time.time()})
                code=str(error)
                if code in ("UPDATE_CHECKSUM_INVALID","UPDATE_ARCHIVE_INVALID","UPDATE_PACKAGE_IDENTITY_MISMATCH"):
                    finish(directory,attempt,"failed",code)
                raise
            if not gateway.idle(bridge):return {"stage":"GATEWAY_BUSY"}
            _,fresh=bridge.transport.request("POST","/api/v1/device/check-in",token,report,timeout=5)
            if fresh.get("release")!=status["release"] or fresh.get("request")!=job:return {"stage":"UPDATE_OFFER_CHANGED"}
            with admission(directory,exclusive=True):
                if not gateway.idle(bridge):return {"stage":"GATEWAY_BUSY"}
                _,confirmed=bridge.transport.request("POST","/api/v1/device/check-in",token,
                    {**report,"state":"updating","jobId":attempt["jobId"]},timeout=5)
                if confirmed.get("release")!=status["release"] or confirmed.get("request")!=job:
                    return {"stage":"UPDATE_OFFER_CHANGED"}
                receipt=installer.owned_receipt(file,state["installationId"])
                intent=backup(directory,receipt,installer.bridge_payload(root,p),installer,attempt,old,config)
                atomic_json(directory/"managed-last-check.json",{"digest":m["zipSha256"],"at":time.time()})
                try:
                    installer.update(root,file,state["installationId"])
                    gateway.reload()
                    ok=gateway.wait(config,m["version"])
                    if not ok and prefs["restart"]:
                        intent["forwardRestart"]=True;atomic_json(directory/"managed-intent.json",intent)
                        ok=gateway.restart(bridge) and gateway.wait(config,m["version"])
                    if not ok:raise ValueError("UPDATE_RUNTIME_NOT_READY")
                except Exception:
                    recover(directory,intent,installer,gateway,bridge,prefs)
                    return {"stage":"UPDATE_RECOVERY_RECORDED"}
                finish(directory,intent,"applied")
                return {"stage":"UPDATED"}
    finally:
        bridge.transport.close()


def run(args,directory,state,installer,client):
    if sys.platform!="darwin" or platform.machine()!="arm64":
        raise ValueError("MANAGED_UPDATE_PLATFORM_NOT_QUALIFIED")
    if state.get("transportMode")!="official" or not state.get("installed"):
        raise ValueError("ACTIVATION_REQUIRED")
    if args.action=="enable-updates":return enable(args,directory,state,installer)
    settings=directory/"managed-settings.json"
    prefs=read(settings)
    if prefs is None:raise ValueError("UPDATE_BOOTSTRAP_REQUIRED")
    if args.action in ("pause-updates","resume-updates"):
        prefs["auto"]=args.action=="resume-updates";atomic_json(settings,prefs)
        return {"stage":"UPDATED_PREFERENCE","auto":prefs["auto"]}
    lock=Path(state["hermesRoot"])/"asbot-managed-update"
    private_dir(lock)
    with client.state_lock(lock):
        return tick(directory,state,installer,client,prefs)
