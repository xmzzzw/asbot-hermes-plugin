"""Install/remove one current-user timer for the fixed private device client."""
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from asbot_bridge.private_io import secure_dir, secure_file


def run(argv, *, env=None):
    return subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          env=env,timeout=20,check=False)


def command(argv):
    if run(argv).returncode:
        raise ValueError('ASSIST_SCHEDULER_UNAVAILABLE')


def bootstrap(client,directory,package):
    target=directory/'assist-bootstrap'
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            stage=Path(temp)/'app'
            secure_dir(stage)
            for name in [*package['files'],'release.json']:
                dest=stage/name
                if dest.parent!=stage and not dest.parent.exists():
                    secure_dir(dest.parent)
                client.installer.atomic(dest,(client.ROOT/name).read_bytes())
            if client.installer.verify_payload(stage)!=package:
                raise ValueError('ASSIST_BOOTSTRAP_CHANGED')
            stage.rename(target)
    secure_dir(target)
    expected=set(package['files'])|{'release.json'}
    folders={str(Path(name).parent) for name in expected}
    for item in target.rglob('*'):
        name=item.relative_to(target).as_posix()
        known=(name in folders) if item.is_dir() else (item.is_file() and name in expected)
        if item.is_symlink() or not known:
            raise ValueError('ASSIST_BOOTSTRAP_CHANGED')
    if client.installer.verify_payload(target)!=package:
        raise ValueError('ASSIST_BOOTSTRAP_CHANGED')
    return target


def unit_arg(value):
    if not value or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('ASSIST_SCHEDULER_PATH_INVALID')
    # systemd specifiers, environment variables and words all stay literal.
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def definitions(kind,label,argv):
    if kind=='darwin':
        return {label+'.plist':plistlib.dumps({'Label':label,'ProgramArguments':argv,'RunAtLoad':True,
                                               'StartInterval':60,'ProcessType':'Background'})}
    service=('[Unit]\nDescription=AS-BOT Hermes plugin device\n[Service]\nType=oneshot\nExecStart='+
             ' '.join(map(unit_arg,argv))+'\n')
    timer=('[Unit]\nDescription=AS-BOT Hermes plugin contact\n[Timer]\nOnStartupSec=15s\n'
           'OnUnitInactiveSec=60s\nAccuracySec=5s\n[Install]\nWantedBy=timers.target\n')
    return {label+'.service':service.encode(),label+'.timer':timer.encode()}


WINDOWS = r'''
$ErrorActionPreference='Stop'
$name=$env:ASBOT_TASK_NAME
$cmd=if ($env:ASBOT_TASK_WSL -eq '1') {Join-Path $env:SystemRoot 'System32\wsl.exe'} else {$env:ASBOT_TASK_EXE}
$argsText=$env:ASBOT_TASK_ARGS
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
# Enumerating the root folder distinguishes an absent task from a read error.
$task=@(Get-ScheduledTask -TaskPath '\' | Where-Object {$_.TaskName -ceq $name})
if ($task.Count -gt 1) {exit 3}
if ($task.Count) {
 $t=$task[0]
 $p=$t.Principal.UserId
 if ($p -ne $sid) {try {$p=([Security.Principal.NTAccount]$p).Translate([Security.Principal.SecurityIdentifier]).Value} catch {exit 3}}
 if ($t.Actions.Count -ne 1 -or $t.Actions[0].Execute -ne $cmd -or $t.Actions[0].Arguments -cne $argsText -or
     $t.Description -cne 'AS-BOT Hermes device contact v1' -or $p -ne $sid -or $t.Principal.RunLevel -ne 'Limited' -or
     $t.Principal.LogonType -ne 'Interactive' -or $t.Triggers.Count -ne 1 -or $t.Triggers[0].Repetition.Interval -ne 'PT1M') {exit 3}
 if ($env:ASBOT_TASK_MODE -eq 'remove') {Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false}
} elseif ($env:ASBOT_TASK_MODE -eq 'install') {
 $a=New-ScheduledTaskAction -Execute $cmd -Argument $argsText
 $t=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(15)) -RepetitionInterval (New-TimeSpan -Minutes 1)
 $p=New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
 $s=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
 Register-ScheduledTask -TaskPath '\' -TaskName $name -Description 'AS-BOT Hermes device contact v1' -Action $a -Trigger $t -Principal $p -Settings $s | Out-Null
}
'''


def windows(mode,label,argv,wsl=None):
    arguments=argv[1:]
    if wsl:
        arguments=['--distribution',wsl['distro'],'--user',wsl['user'],'--exec',*argv]
    env=dict(os.environ,ASBOT_TASK_NAME=label,ASBOT_TASK_MODE=mode,ASBOT_TASK_EXE=argv[0],
             ASBOT_TASK_WSL='1' if wsl else '0',ASBOT_TASK_ARGS=subprocess.list2cmdline(arguments))
    if wsl:
        env['WSLENV']=':'.join(name+'/w' for name in ('ASBOT_TASK_NAME','ASBOT_TASK_MODE','ASBOT_TASK_EXE','ASBOT_TASK_WSL','ASBOT_TASK_ARGS'))
    code=base64.b64encode(WINDOWS.encode('utf-16le')).decode('ascii')
    result=run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',code],env=env)
    if result.returncode:
        raise ValueError('ASSIST_SCHEDULER_CHANGED' if result.returncode==3 else 'ASSIST_SCHEDULER_UNAVAILABLE')


def change(client,directory,record,enable):
    kind,label,argv=record['kind'],record['label'],record['argv']
    if kind in ('win32','wsl-task'):
        windows('install' if enable else 'remove',label,argv,record.get('wsl'))
        return
    if kind=='darwin':
        folder=Path.home()/'Library/LaunchAgents'
    else:
        folder=Path(os.environ.get('XDG_CONFIG_HOME',Path.home()/'.config'))/'systemd/user'
    if not folder.is_absolute() or any(p.is_symlink() for p in (folder,*folder.parents)):
        raise ValueError('ASSIST_SCHEDULER_PATH_INVALID')
    client.installer.plugin_parent(folder)
    files=definitions(kind,label,argv)
    if kind=='linux':
        for name in files:
            result=run(['systemctl','--user','show',name,'--property=LoadState,FragmentPath'])
            fields=dict(line.split('=',1) for line in result.stdout.decode().splitlines() if '=' in line)
            if fields.get('LoadState')=='not-found':continue
            if result.returncode:raise ValueError('ASSIST_SCHEDULER_UNAVAILABLE')
            if fields.get('FragmentPath')!=str(folder/name):raise ValueError('ASSIST_SCHEDULER_CHANGED')
    for name,data in files.items():
        path=folder/name
        if path.exists() or path.is_symlink():
            secure_file(path)
            if path.read_bytes()!=data:
                raise ValueError('ASSIST_SCHEDULER_CHANGED')
        elif enable:
            client.installer.atomic(path,data)
    if kind=='darwin':
        domain='gui/'+str(os.getuid())
        plist=folder/(label+'.plist')
        active=run(['/bin/launchctl','print',domain+'/'+label])
        if not active.returncode and not any(line.strip()==('path = '+str(plist))
                for line in active.stdout.decode(errors='replace').splitlines()):
            raise ValueError('ASSIST_SCHEDULER_CHANGED')
        if enable and active.returncode:
            command(['/bin/launchctl','bootstrap',domain,str(plist)])
        if not enable:
            plist.unlink(missing_ok=True)
            if not active.returncode:
                command(['/bin/launchctl','bootout',domain+'/'+label])
        return
    if enable:
        command(['systemctl','--user','daemon-reload'])
        command(['systemctl','--user','enable','--now',label+'.timer'])
    else:
        if (folder/(label+'.timer')).exists():
            command(['systemctl','--user','disable','--now',label+'.timer'])
        for name in files:
            (folder/name).unlink(missing_ok=True)
        command(['systemctl','--user','daemon-reload'])


def enable(client,directory,package):
    kind=sys.platform
    if kind not in ('darwin','linux','win32'):
        raise ValueError('ASSIST_SCHEDULER_UNAVAILABLE')
    boot=bootstrap(client,directory,package)
    python=Path(sys.executable)
    if kind=='win32' and python.with_name('pythonw.exe').is_file():
        python=python.with_name('pythonw.exe')
    argv=[str(python),'-I','-B',str(boot/'bin/plugin.py'),'assist-tick','--state-dir',str(directory)]
    label='com.asbot.hermes-assist.'+hashlib.sha256(str(directory).encode()).hexdigest()[:20]
    record={'kind':kind,'argv':argv,'label':label}
    path=directory/'assist-scheduler.json'
    if kind=='linux' and 'microsoft' in os.uname().release.lower() and os.environ.get('WSL_DISTRO_NAME'):
        import pwd
        distro=os.environ['WSL_DISTRO_NAME']
        user=pwd.getpwuid(os.getuid()).pw_name
        existing=None
        if path.exists():
            secure_file(path)
            existing=json.loads(path.read_text()).get('kind')
        native=existing=='wsl-task' or existing is None and (not shutil.which('systemctl') or
            run(['systemctl','--user','show','--property=Version','--value']).returncode!=0)
        if native:
            if (not os.getuid() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,63}',distro)
                    or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}',user) or not shutil.which('powershell.exe')):
                raise ValueError('WSL_USER_SCHEDULER_UNAVAILABLE')
            record.update({'kind':'wsl-task','wsl':{'distro':distro,'user':user}})
    if path.exists():
        secure_file(path)
        if json.loads(path.read_text())!=record:
            raise ValueError('ASSIST_SCHEDULER_CHANGED')
    else:
        client.installer.atomic(path,json.dumps(record).encode())
    change(client,directory,record,True)


def disable(client,directory):
    path=directory/'assist-scheduler.json'
    if not path.exists():
        return
    secure_file(path)
    record=json.loads(path.read_text())
    label='com.asbot.hermes-assist.'+hashlib.sha256(str(directory).encode()).hexdigest()[:20]
    kind=record.get('kind')
    if record.get('label')!=label or not (kind==sys.platform or kind=='wsl-task' and sys.platform=='linux'):
        raise ValueError('ASSIST_SCHEDULER_CHANGED')
    change(client,directory,record,False)
    path.unlink()
