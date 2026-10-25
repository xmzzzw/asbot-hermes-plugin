#!/usr/bin/env python3
"""Install an Owner-checksummed distribution into selected EXISTING Hermes profiles.
No repository access, credential printing, login, gateway start, or model change.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asbot_bridge.bridge import Bridge, Identity, private_file, private_dir, validate_config
from asbot_bridge.private_io import secure_open, _windows_acl
spec = importlib.util.spec_from_file_location('asbot_client_profiles', ROOT/'bin/client-profile.py')
local = importlib.util.module_from_spec(spec); spec.loader.exec_module(local)


def atomic(path, data):
    path = Path(path)
    if path.exists() or path.is_symlink():
        private_file(path)
    temporary = path.parent / ('.asbot-'+uuid.uuid4().hex)
    fd = secure_open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_payload(root):
    manifest_path=root/'release.json'
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size>2*1024*1024:
        raise ValueError('INVALID_PACKAGE_MANIFEST')
    manifest = json.loads(manifest_path.read_text())
    if not re.fullmatch(r'[a-f0-9]{40}',manifest.get('sha','')):
        raise ValueError('EXACT_RELEASE_REQUIRED')
    if not isinstance(manifest.get('files'),dict):raise ValueError('INVALID_PACKAGE_MANIFEST')
    for name,digest in manifest['files'].items():
        if not isinstance(name,str) or '\\' in name or not re.fullmatch('[a-f0-9]{64}',str(digest)):
            raise ValueError('INVALID_PACKAGE_PATH')
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('INVALID_PACKAGE_PATH')
        path=root/relative
        if root.is_symlink() or any((root/parent).is_symlink() for parent in (relative,*relative.parents)):
            raise ValueError('PACKAGE_CHECKSUM_FAILED')
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('PACKAGE_CHECKSUM_FAILED')
    required={'asbot_bridge/bridge.py','asbot_bridge/private_io.py','asbot_bridge/plugin.yaml'}
    if not required <= manifest['files'].keys():
        raise ValueError('PACKAGE_INCOMPLETE')
    return manifest


def install(root, hermes, config_file, profiles, apply=False, *, installation_id=None, private_root=None):
    manifest=verify_payload(root)
    private_file(config_file)
    raw=config_file.read_bytes(); config=validate_config(json.loads(raw))
    if config.get('external_access') is not True:
        raise ValueError('INVITATION_CONFIGURATION_REQUIRED')
    per_install = installation_id is not None
    if per_install:
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', installation_id)
                or private_root is None or not Path(private_root).is_absolute()
                or config.get('installation_id') != installation_id):
            raise ValueError('INSTALLATION_ID_REQUIRED')
        private_root = Path(private_root)
        private_dir(private_root)
        if (private_root/'install-receipt.json').exists():
            raise ValueError('EXISTING_INSTALLATION_REQUIRES_UPDATE')
    state_dir = private_root/'bridge-state' if per_install else hermes/'asbot-bridge-state'
    if config['state_dir']!=str(state_dir):
        raise ValueError('STATE_PATH_MISMATCH')
    profiles=list(dict.fromkeys(profiles))
    if not profiles or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',p) for p in profiles):
        raise ValueError('INVALID_PROFILE_SELECTION')
    plans=[];selected=set()
    destination=private_root/'bridge.json' if per_install else hermes/'asbot-bridge.json'
    if per_install and (destination.exists() or destination.is_symlink()):
        raise ValueError('EXISTING_BRIDGE_CONFIG')
    for name in profiles:
        home=hermes if name=='default' else hermes/'profiles'/name
        path=home/'config.yaml'
        before=path.read_bytes();current=local.load_yaml(path)
        if per_install:
            plugins=current.get('plugins', {})
            if (not isinstance(plugins, dict) or 'asbot-bridge' in plugins.get('enabled', [])
                    or 'asbot-bridge' in plugins.get('entries', {})):
                raise ValueError('EXISTING_PLUGIN_REQUIRES_OWNING_UPGRADE_PATH')
        approved=local.approved_peers(home)
        peers={p['user_id'] for p in config['principals'] if p['enabled'] and p['profile']==name and p['user_id']==p['chat_id']}
        if not peers or not peers<=approved:
            raise ValueError('EXISTING_APPROVED_DM_REQUIRED')
        selected.update((name,p,p) for p in peers)
        after=local.dump_yaml(local.merge_profile(current,name,destination,preserve_search=True))
        plugin=home/'plugins/asbot-bridge'
        if plugin.exists() or plugin.is_symlink():
            raise ValueError('EXISTING_PLUGIN_REQUIRES_OWNING_UPGRADE_PATH')
        plans.append((home,path,before,after,plugin))
    actual={(p['profile'],p['user_id'],p['chat_id']) for p in config['principals'] if p['enabled']}
    if actual!=selected:
        raise ValueError('PROFILE_SELECTION_MISMATCH')
    report={'status':'PLAN_ONLY','sha':manifest['sha'],'profiles':profiles,'privateMappings':len(actual)}
    if not apply:return report
    private_dir(state_dir)
    backup=(private_root if per_install else hermes)/('asbot-package-backup-'+uuid.uuid4().hex);private_dir(backup)
    if destination.exists() or destination.is_symlink():private_file(destination)
    previous=destination.read_bytes() if destination.exists() else None
    if previous is not None:private_file(destination);atomic(backup/'bridge-config.json',previous)
    changed=[]
    try:
        atomic(destination,raw)
        for index,(home,path,before,after,plugin) in enumerate(plans):
            if path.read_bytes()!=before:raise ValueError('PROFILE_CHANGED')
            atomic(backup/(str(index)+'.yaml'),before)
            private_dir(plugin.parent);private_dir(plugin)
            changed.append((path,before,plugin))
            # Copy regular files works on native Windows without symlink privilege.
            for name,data in bridge_payload(root,manifest).items():
                atomic(plugin/name,data)
            atomic(path,after)
        receipt={'sha':manifest['sha'],'config':str(destination),'configHash':hashlib.sha256(raw).hexdigest(),
                 'hadConfig':previous is not None,'profiles':[{'path':str(p),'plugin':str(l),'installedHash':hashlib.sha256(a).hexdigest(),
                 'pluginHashes':plugin_hashes(l)} for h,p,b,a,l in plans]}
        if per_install:
            receipt.update({'installationId':installation_id,'privateRoot':str(private_root),'restoreBackup':str(backup)})
        atomic(backup/'receipt.json',json.dumps(receipt).encode())
        if per_install:atomic(private_root/'install-receipt.json',json.dumps(receipt).encode())
    except BaseException:
        for path,before,plugin in reversed(changed):
            atomic(path,before);shutil.rmtree(plugin)
        if previous is not None:atomic(destination,previous)
        else:destination.unlink(missing_ok=True)
        raise
    return dict(report,status='INSTALLED',backup=str(backup),restart='Use existing Hermes plugin reload; new gateway login is unnecessary')


def plugin_hashes(plugin):
    if plugin.is_symlink() or not plugin.is_dir():
        raise ValueError('INVALID_PLUGIN_PATH')
    result={}
    for path in plugin.iterdir():
        # Python may create this disposable cache after plugin load.
        if path.name == '__pycache__' and path.is_dir() and not path.is_symlink():continue
        if path.is_symlink() or not path.is_file():raise ValueError('PLUGIN_CHANGED_REQUIRES_REVIEW')
        result[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def bridge_payload(root, manifest):
    return {Path(name).name:(root/name).read_bytes() for name in manifest.get('files', {})
            if Path(name).parent == Path('asbot_bridge') and Path(name).suffix in ('.py','.yaml')}


def owned_receipt(path, installation_id):
    private_file(path)
    receipt=json.loads(path.read_text())
    if (receipt.get('installationId') != installation_id or
            Path(receipt.get('privateRoot','')) != path.parent or path.name != 'install-receipt.json'):
        raise ValueError('INSTALLATION_OWNERSHIP_MISMATCH')
    check_receipt(receipt)
    return receipt


def check_receipt(receipt):
    for item in receipt['profiles']:
        plugin=Path(item['plugin']);profile=Path(item['path'])
        if plugin.name!='asbot-bridge' or plugin.parent.name!='plugins' or plugin.parent.parent!=profile.parent:
            raise ValueError('INVALID_PLUGIN_PATH')
        private_file(profile)
        if hashlib.sha256(profile.read_bytes()).hexdigest()!=item['installedHash']:
            raise ValueError('PROFILE_CHANGED_REQUIRES_REVIEW')
        if 'pluginHashes' in item and plugin_hashes(plugin)!=item['pluginHashes']:
            raise ValueError('PLUGIN_CHANGED_REQUIRES_REVIEW')
    config=Path(receipt['config']);private_file(config)
    if hashlib.sha256(config.read_bytes()).hexdigest()!=receipt['configHash']:
        raise ValueError('BRIDGE_CONFIG_CHANGED')


def rotate_credentials(receipt_file, installation_id, bundle):
    """CAS only this install's credentials, preserving profile/plugin/ledger state.

    The private intent makes a config/receipt interruption recoverable only when
    both files still match the exact old or new hashes; later edits fail closed.
    """
    intent_file=receipt_file.parent/'credential-update.json'
    if intent_file.exists() or intent_file.is_symlink():
        private_file(intent_file)
        intent=json.loads(intent_file.read_text())
        if (intent.get('installationId')!=installation_id or
                intent.get('credentialVersion')!=bundle['credentialVersion']):
            raise ValueError('CREDENTIAL_UPDATE_REQUIRES_RECONCILIATION')
        replacement=json.loads(intent['newConfig'])
        if (replacement.get('account_id')!=bundle['accountId'] or replacement.get('installation_id')!=installation_id
                or replacement.get('origin')!=bundle['origin'] or replacement.get('access')!=bundle['access']
                or len(replacement['principals'])!=1 or replacement['principals'][0]['bearer_token']!=bundle['bearerToken']):
            raise ValueError('CREDENTIAL_UPDATE_REQUIRES_RECONCILIATION')
        private_file(receipt_file)
        receipt_raw=receipt_file.read_bytes()
        config_file=receipt_file.parent/'bridge.json';private_file(config_file)
        current=config_file.read_bytes()
        if (hashlib.sha256(receipt_raw).hexdigest() not in (intent['oldReceiptHash'],intent['newReceiptHash'])
                or hashlib.sha256(current).hexdigest() not in (intent['oldConfigHash'],intent['newConfigHash'])):
            raise ValueError('BRIDGE_CONFIG_CHANGED')
        check_copy=json.loads(intent['newReceipt'])
        check_copy['configHash']=hashlib.sha256(current).hexdigest()
        check_receipt(check_copy)
        atomic(config_file,intent['newConfig'].encode())
        atomic(receipt_file,intent['newReceipt'].encode())
        intent_file.unlink()
        return
    receipt=owned_receipt(receipt_file,installation_id)
    config_file=Path(receipt['config']);before=config_file.read_bytes()
    config=json.loads(before)
    if (config.get('account_id')!=bundle['accountId'] or config.get('installation_id')!=installation_id
            or bundle['installationId']!=installation_id or config.get('origin')!=bundle['origin']
            or len(config['principals'])!=1):
        raise ValueError('INSTALLATION_OWNERSHIP_MISMATCH')
    if (config['access']==bundle['access'] and config['principals'][0]['bearer_token']==bundle['bearerToken']
            and receipt.get('credentialVersion')==bundle['credentialVersion']):return
    config['access']=bundle['access']
    config['principals'][0]['bearer_token']=bundle['bearerToken']
    after=json.dumps(config,ensure_ascii=False).encode()
    old_receipt=receipt_file.read_bytes()
    receipt['configHash']=hashlib.sha256(after).hexdigest()
    receipt['credentialVersion']=bundle['credentialVersion']
    new_receipt=json.dumps(receipt).encode()
    backup=receipt_file.parent/('asbot-credential-backup-'+uuid.uuid4().hex);private_dir(backup)
    atomic(backup/'bridge.json',before);atomic(backup/'receipt.json',old_receipt)
    intent={'installationId':installation_id,'credentialVersion':bundle['credentialVersion'],
            'oldConfigHash':hashlib.sha256(before).hexdigest(),'newConfigHash':hashlib.sha256(after).hexdigest(),
            'oldReceiptHash':hashlib.sha256(old_receipt).hexdigest(),'newReceiptHash':hashlib.sha256(new_receipt).hexdigest(),
            'newConfig':after.decode(),'newReceipt':new_receipt.decode()}
    atomic(intent_file,json.dumps(intent).encode())
    # Recheck all owned files after durable intent, before changing credentials.
    owned_receipt(receipt_file,installation_id)
    atomic(config_file,after)
    atomic(receipt_file,new_receipt)
    intent_file.unlink()


def update(root, receipt_file, installation_id):
    """Upgrade only a privately receipted own install; preserve later edits by refusal."""
    manifest=verify_payload(root)
    receipt=owned_receipt(receipt_file,installation_id)
    if not bridge_payload(root,manifest):raise ValueError('PACKAGE_INCOMPLETE')
    backup=receipt_file.parent/('asbot-update-backup-'+uuid.uuid4().hex);private_dir(backup)
    before=receipt_file.read_bytes();atomic(backup/'receipt.json',before)
    saved=[]
    try:
        for index,item in enumerate(receipt['profiles']):
            plugin=Path(item['plugin']);folder=backup/str(index);private_dir(folder)
            original={name:(plugin/name).read_bytes() for name in item['pluginHashes']}
            for name,data in original.items():atomic(folder/name,data)
            saved.append((plugin,original))
            incoming=bridge_payload(root,manifest)
            # Recheck after backup and before the first mutation.
            check_receipt(receipt)
            for name,data in incoming.items():atomic(plugin/name,data)
            for name in original.keys()-incoming.keys():(plugin/name).unlink()
            item['pluginHashes']=plugin_hashes(plugin)
        receipt['sha']=manifest['sha']
        atomic(receipt_file,json.dumps(receipt).encode())
    except BaseException:
        for plugin,original in reversed(saved):
            for name in plugin_hashes(plugin).keys()-original.keys():(plugin/name).unlink()
            for name,data in original.items():atomic(plugin/name,data)
        atomic(receipt_file,before)
        raise
    return {'status':'UPDATED','sha':manifest['sha'],'backup':str(backup),'qualified':False}


def uninstall(backup):
    receipt_file=backup if backup.is_file() else backup/'receipt.json'
    private_file(receipt_file)
    receipt=json.loads(receipt_file.read_text())
    if receipt.get('installationId'):
        receipt=owned_receipt(receipt_file,receipt['installationId'])
        backup=Path(receipt['restoreBackup'])
    # Refuse to overwrite any later user/Agent configuration change.
    for item in receipt['profiles']:
        plugin=Path(item['plugin'])
        if plugin.name!='asbot-bridge' or plugin.parent.name!='plugins' or plugin.is_symlink():
            raise ValueError('INVALID_PLUGIN_PATH')
        private_file(Path(item['path']))
        if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()!=item['installedHash']:
            raise ValueError('PROFILE_CHANGED_REQUIRES_MANUAL_PLUGIN_REMOVAL')
    config=Path(receipt['config']);private_file(config)
    if hashlib.sha256(config.read_bytes()).hexdigest()!=receipt['configHash']:
        raise ValueError('BRIDGE_CONFIG_CHANGED')
    for index,item in enumerate(receipt['profiles']):
        atomic(Path(item['path']),(backup/(str(index)+'.yaml')).read_bytes())
        plugin=Path(item['plugin'])
        if plugin.name!='asbot-bridge' or plugin.parent.name!='plugins' or plugin.is_symlink():
            raise ValueError('INVALID_PLUGIN_PATH')
        shutil.rmtree(plugin)
    if receipt['hadConfig']:atomic(config,(backup/'bridge-config.json').read_bytes())
    else:config.unlink()
    if receipt.get('installationId'):receipt_file.unlink()
    return {'status':'UNINSTALLED','stateRetained':True}


def doctor(config_file):
    bridge=Bridge.from_file(config_file)
    results=[]
    for p in bridge.config['principals']:
        if not p['enabled']:continue
        identity=Identity(p['profile'],'weixin',p['user_id'],p['chat_id'],'doctor')
        me=bridge.service_get(identity,'me')
        templates=bridge.service_get(identity,'screenshot-templates') if me.get('authorized') else {}
        results.append({'profile':p['profile'],'authorized':me.get('authorized') is True,'reasonCode':me.get('reasonCode'),
                        'visibleTemplates':len(templates.get('templates',[])),'nativeMedia':'PENDING_REAL_RECEIPT','busyCommandLane':'PENDING_REAL_TEST'})
    return {'status':'API_CHECKED','platform':sys.platform,'checks':results,'qualified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['install','doctor','uninstall'])
    parser.add_argument('--hermes-root',type=Path,default=Path.home()/'.hermes')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--profile',action='append')
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='doctor':result=doctor(args.config.absolute())
        elif args.action=='uninstall':
            if not args.apply:raise ValueError('UNINSTALL_REQUIRES_APPLY')
            result=uninstall(args.backup.absolute())
        else:result=install(ROOT,args.hermes_root.absolute(),args.config.absolute(),args.profile,args.apply)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as error:
        code=str(error) if type(error) is ValueError and re.fullmatch('[A-Z_]+',str(error)) else getattr(error,'code','CLIENT_SETUP_FAILED')
        print(json.dumps({'status':'BLOCKED','code':code}));return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
