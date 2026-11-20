"""Recipient-owned assisted plugin enrollment and fixed signed jobs."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import uuid

from asbot_bridge.bridge import Bridge
from asbot_bridge.managed import admission
from asbot_bridge.private_io import secure_dir

PROTOCOL = 'as-bot.hermes-assist/v1'
RECEIPT = 'as-bot.hermes-assist-receipt/v1'
ACTIONS = {'install': 'INSTALLED', 'update': 'UPDATED', 'diagnose': 'DIAGNOSED', 'repair': 'REPAIRED'}
TERMINAL = {'expired', 'revoked', 'denied', 'uncertain'}
DAY = 86400


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def key_for(assist):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise ValueError('EXISTING_HERMES_CRYPTO_REQUIRED') from None
    return Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(assist['seed']+'='))


def public(assist):
    key=key_for(assist)
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(assist, fields):
    message = json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return encode(key_for(assist).sign(message))


def descriptor(state):
    assist = state['assisted']
    key = encode(public(assist))
    fields = [PROTOCOL, state['enrollmentId'], state['profile'], assist['maintenance'], assist['restart'], key]
    return {'profile':state['profile'], 'publicKey':key, 'maintenance':assist['maintenance'],
            'restart':assist['restart'], 'signature':sign(assist,fields)}


def card(state):
    return {'schema':'as-bot.hermes-device/v1', 'enrollmentId':state['enrollmentId'],
            'fingerprint':hashlib.sha256(public(state['assisted'])).hexdigest(), 'profile':state['profile']}


def profile_binding(client, root, profile, choose=None):
    hermes, homes = client.profile_homes(root,all_profiles=True)
    options = {}
    for name, home in sorted(homes.items()):
        if profile is not None and name != profile or not (home/'config.yaml').is_file():
            continue
        config = client.installer.local.load_yaml(home/'config.yaml')
        account = client.configured_wechat(home,config)
        if not account:
            continue
        if not client.plugin_available(home,config):
            if profile == name:
                raise ValueError('EXISTING_PLUGIN_REQUIRES_OWNING_UPGRADE_PATH')
            continue
        try:
            peers = client.installer.local.approved_bindings(home)
        except ValueError as error:
            if str(error) != 'NO_EXISTING_APPROVED_WEIXIN_PEERS':
                raise
            continue
        options[name] = (account, sorted(peers), peers)
    if not options:
        raise ValueError('NO_AVAILABLE_WEIXIN_BINDINGS')
    def one(values, labels, question):
        if len(values) == 1:
            return values[0]
        if choose is None:
            raise ValueError('LOCAL_SELECTION_REQUIRED')
        return values[choose(labels,question)]
    names = list(options)
    name = one(names,names,'选择已有的 Hermes 配置')
    account, peers, labels = options[name]
    # Only the recipient can see local display names. Never display a raw peer.
    names = [f'微信私聊 {i}'+(' · '+labels[p][:60] if labels[p] and labels[p] not in peers else '')
             for i,p in enumerate(peers,1)]
    peer = one(peers,names,'选择一个已批准的微信私聊')
    return hermes, name, account, peer


def setup(client, directory, args, transport, choose=None):
    package = client.installer.verify_payload(client.ROOT)
    if package.get('client')!='hermes' or package.get('clientVersion')!=client.CLIENT_VERSION or 'release-key.pem' not in package['files']:
        raise ValueError('ASSISTED_BOOTSTRAP_PACKAGE_REQUIRED')
    state = client.load_state(directory)
    if state is None:
        hermes,name,account,peer = profile_binding(client,args.hermes_root,args.profile,choose)
        version = client.native_compatibility()
        platform='wsl' if sys.platform=='linux' and 'microsoft' in os.uname().release.lower() else sys.platform
        state = {'enrollmentId':str(uuid.uuid4()), 'recovery':secrets.token_urlsafe(32),
                 'pairingCode':''.join(secrets.choice(client.PAIR_ALPHABET) for _ in range(12)),
                 'hermesRoot':str(hermes), 'profile':name, 'peer':peer, 'deviceLabel':'Hermes '+platform,
                 'hermesVersion':version, 'platform':platform, 'transportMode':'official',
                 'clientVersion':client.CLIENT_VERSION+'+'+package['sha'], 'created':False,
                 'assisted':{'seed':secrets.token_urlsafe(32), 'account':account, 'sourceSha':package['sha'],
                             'maintenance':args.allow_maintenance, 'restart':args.allow_idle_restart,
                             'createdAt':int(time.time())}}
        descriptor(state)  # Validate the local crypto implementation before persisting an enrollment.
        client.save_state(directory,state)
    assist = state.get('assisted')
    if not assist:
        raise ValueError('EXISTING_INSTALLATION_REQUIRES_OWNING_UPGRADE_PATH')
    if (assist.get('withdrawn') or assist['sourceSha']!=package['sha'] or
            args.profile is not None and args.profile != state['profile'] or
            args.hermes_root is not None and str(Path(args.hermes_root).expanduser().absolute())!=state['hermesRoot'] or
            (args.allow_maintenance,args.allow_idle_restart)!=(assist['maintenance'],assist['restart'])):
        raise ValueError('EXISTING_ENROLLMENT_SELECTION_MISMATCH')
    if not state.get('createAttempted'):
        state['createAttempted'] = True
        client.save_state(directory,state)
        payload = {k:state[k] for k in ('enrollmentId','pairingCode','platform','clientVersion','deviceLabel','hermesVersion')}
        payload['assisted'] = descriptor(state)
        transport.request('POST','/connect/v1/enrollments',state['recovery'],payload)
    refresh(client,directory,state,transport)  # An unknown create is reconciled by GET only.
    if assist.get('withdrawn') or state.get('remoteState') in TERMINAL:
        raise ValueError('ASSISTED_ENROLLMENT_CLOSED')
    if args.action=='assist-welcome' and not assist.get('withdrawn') and (
            assist['maintenance'] or not assist.get('last') and time.time()-assist['createdAt']<DAY):
        schedule = client.helper('assist-schedule')
        assist['householdTimer']=True
        client.save_state(directory,state)
        schedule.enable(client,directory,package)
        assist['scheduled'] = True
        client.save_state(directory,state)
    elif assist['maintenance'] and not assist.get('scheduled'):
        # SSH may stage an enrollment before its job is issued or canceled.
        # Reserve the verified helper, without scheduling any future execution.
        client.helper('assist-schedule').bootstrap(client,directory,package)
    result = card(state)
    client.installer.atomic(directory/'device-card.json',json.dumps(result,ensure_ascii=False,indent=2).encode())
    return result


def status_view(state):
    assist = state['assisted']
    result = {'card':card(state),'qualified':False,'stage':state.get('remoteState','local_setup').upper(),
              'maintenance':assist['maintenance'] and not assist.get('withdrawn',False),
              'restartAllowed':assist['restart'] and not assist.get('withdrawn',False),
              'installed':state.get('installed',False),'scheduled':assist.get('scheduled',False)}
    if assist.get('withdrawn'):
        result['stage'] = 'WITHDRAWN' if assist.get('withdrawConfirmed') else 'LOCAL_WITHDRAWN_SERVER_PENDING'
    if assist.get('cleanupPending'):
        result['schedulerCleanupPending']=True
    if assist.get('last'):
        result['last'] = assist['last']
    if assist.get('intent'):
        result['pending'] = assist['intent'].get('pending','RECONCILIATION_REQUIRED')
    return result


def receipt(client,state,transport,job,event,code):
    job_id, epoch = (job['id'], job['epoch']) if job else (None,0)
    fields = [RECEIPT,state['enrollmentId'],job_id,epoch,event,code]
    payload = {'jobId':job_id,'epoch':epoch,'event':event,'code':code,'signature':sign(state['assisted'],fields)}
    return client.request(transport,state,'/assisted','POST',payload)


def unschedule(client,directory,state):
    assist = state['assisted']
    if assist.get('scheduled') or (directory/'assist-scheduler.json').exists():
        # launchctl can terminate this very tick during bootout. Record the
        # stop first; the marker allows an interrupted cleanup to be reconciled.
        assist['scheduled'] = False
        assist['cleanupPending'] = True
        client.save_state(directory,state)
        client.helper('assist-schedule').disable(client,directory)
        assist.pop('cleanupPending',None)
        client.save_state(directory,state)


def enable_after_install(client,directory,state):
    assist=state['assisted']
    if not assist['maintenance'] or assist.get('scheduled') or assist.get('withdrawn'):
        return
    package=client.installer.verify_payload(directory/'assist-bootstrap')
    if package.get('sha')!=assist['sourceSha']:
        raise ValueError('ASSIST_BOOTSTRAP_CHANGED')
    client.helper('assist-schedule').enable(client,directory,package)
    assist['scheduled']=True
    client.save_state(directory,state)


def withdraw(client,directory,state,transport):
    assist = state['assisted']
    assist['withdrawn'] = True
    client.save_state(directory,state)
    # The local guard is durable before either the network or scheduler is touched.
    try:
        unschedule(client,directory,state)
    except (OSError,ValueError):
        # A missing user scheduler must not prevent server-side withdrawal.
        # The retained task can only encounter the durable local guard.
        assist['cleanupPending']=True
    if not assist.get('withdrawConfirmed'):
        answer = receipt(client,state,transport,None,'withdraw','WITHDRAWN')
        if answer.get('withdrawn') is not True:
            raise ValueError('WITHDRAWAL_RECEIPT_REQUIRED')
        assist['withdrawConfirmed'] = True
        client.save_state(directory,state)
    return status_view(state)


def refresh(client,directory,state,transport):
    answer=client.status(directory,state,transport)
    if answer.get('assistedWithdrawn') is True:
        state['assisted'].update({'withdrawn':True,'withdrawConfirmed':True})
        client.save_state(directory,state)
        unschedule(client,directory,state)
    return answer


def validate_job(client,state,job,managed,*,binding_only=False):
    assist = state['assisted']
    keys = {'id','action','accountId','enrollmentId','fingerprint','profile','channel','sourceSha','version',
            'zipSha256','payload','signature','epoch','deadlineAt','state','restart'}
    if (not isinstance(job,dict) or set(job)!=keys or not isinstance(job['id'],str)
            or not client.UUID4.fullmatch(job['id']) or job['action'] not in ACTIONS
            or not isinstance(job['accountId'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',job['accountId'])
            or (job['profile'], job['enrollmentId'], job['fingerprint']) !=
               (state['profile'],state['enrollmentId'],card(state)['fingerprint'])
            or type(job['epoch']) is not int or job['epoch']<1 or type(job['restart']) is not bool
            or job['restart'] and not assist['restart'] or job['state'] not in ('waiting','executing')
            or type(job['deadlineAt']) is not int or job['deadlineAt']<=time.time()*1000):
        raise ValueError('ASSISTED_JOB_INVALID')
    if binding_only:return
    if job['action']!='install' and not assist['maintenance']:
        raise ValueError('MAINTENANCE_CONSENT_REQUIRED')
    if job['action']=='install' and (state.get('installed') or assist['sourceSha']!=job['sourceSha']):
        raise ValueError('ASSISTED_INSTALL_PIN_MISMATCH')
    envelope = {k:job[k] for k in ('payload','signature')}
    manifest = managed.manifest(envelope,(client.ROOT/'release-key.pem').read_bytes(),'0.0.0',
                                client.native_compatibility(),job['channel']=='pilot',int(time.time()*1000))
    if any(job[k]!=manifest[k] for k in ('version','sourceSha','zipSha256','channel')):
        raise ValueError('ASSISTED_RELEASE_PIN_MISMATCH')
    return manifest


def check_binding(client,state):
    _,homes = client.profile_homes(state['hermesRoot'])
    home = homes.get(state['profile'])
    if home is None:
        raise ValueError('WECHAT_BINDING_CHANGED')
    config = client.installer.local.load_yaml(home/'config.yaml')
    if (client.configured_wechat(home,config)!=state['assisted']['account'] or
            state['peer'] not in client.installer.local.approved_peers(home)):
        raise ValueError('WECHAT_BINDING_CHANGED')
    return home, config


def owned(client,directory,state,job):
    home,_ = check_binding(client,state)
    install = client.installer.owned_receipt(directory/'install-receipt.json',state.get('installationId'))
    if ([Path(p['path']) for p in install['profiles']]!=[home/'config.yaml']
            or Path(install['config'])!=directory/'bridge.json'):
        raise ValueError('INSTALLATION_OWNERSHIP_MISMATCH')
    bridge = Bridge.from_file(directory/'bridge.json')
    matches = [(p['profile'],p['user_id'],p['chat_id']) for p in bridge.config['principals'] if p['enabled']]
    if (bridge.config['account_id']!=job['accountId'] or bridge.config['installation_id']!=state['installationId']
            or matches!=[(state['profile'],state['peer'],state['peer'])]):
        bridge.transport.close()
        raise ValueError('INSTALLATION_OWNERSHIP_MISMATCH')
    return install,bridge


def acknowledge_install(client,directory,state,transport,bridge,job):
    answer=client.status(directory,state,transport)
    if (answer.get('installationId')!=state.get('installationId')
            or state.get('remoteCredentialVersion')!=state.get('credentialVersion')):
        return False
    client.local_doctor(directory,state)
    if state['remoteState']=='ready' and state.get('ackAttemptedVersion')!=state['credentialVersion']:
        confirm_execution(client,state,transport,job)
        state['ackAttemptedVersion']=state['credentialVersion']
        client.save_state(directory,state)
        client.request(transport,state,'/ack','POST',{'installationToken':bridge.config['principals'][0]['bearer_token']})
        client.status(directory,state,transport)
    if state['remoteState']!='active':return False
    (directory/'credentials.json').unlink(missing_ok=True)
    return True


def confirm_execution(client,state,transport,job):
    answer=client.request(transport,state)
    if (answer.get('assistedWithdrawn') is True or answer.get('state') not in ('ready','active')
            or answer.get('assistedJob')!={**job,'state':'executing'} or time.time()*1000>=job['deadlineAt']):
        raise ValueError('ASSISTED_JOB_CHANGED')


def perform(client,directory,state,transport,job,package,managed):
    action = job['action']
    home, config = check_binding(client,state)
    confirm_execution(client,state,transport,job)
    if action=='install':
        if state.get('remoteState')!='ready' or not client.plugin_available(home,config):
            raise ValueError('LOCAL_GATE')
        # Existing activation handles the private credential bundle, backup, own
        # install receipt and reconciles a lost activation ack on the same id.
        client.activate(directory,state,transport,package=package,expected_account=job['accountId'],
                        authorize_mutation=lambda:confirm_execution(client,state,transport,job))
    install,bridge = owned(client,directory,state,job)
    try:
        old = managed.installer_version(install)
        gateway = managed.Gateway(state,client,install,before_restart=lambda:confirm_execution(client,state,transport,job))
        config_path = directory/'bridge.json'
        if action=='diagnose':
            if install['sha']!=job['sourceSha'] or old!=job['version']:
                raise ValueError('LOCAL_GATE')
            client.local_doctor(directory,state)
            if not gateway.ready(config_path,old):
                raise ValueError('LOCAL_GATE')
            return
        if action=='install':
            if install['sha']!=job['sourceSha'] or old!=job['version']:
                raise ValueError('ASSISTED_INSTALL_PIN_MISMATCH')
            if not gateway.wait(config_path,old,8):
                confirm_execution(client,state,transport,job)
                if not job['restart'] or not gateway.restart(bridge) or not gateway.wait(config_path,old):
                    raise ValueError('INSTALLED_GATEWAY_BINDING_PENDING')
            if not acknowledge_install(client,directory,state,transport,bridge,job):
                raise ValueError('ACTIVATION_RECEIPT_REQUIRED')
            return
        if action=='update' and tuple(map(int,job['version'].split('.')))<=tuple(map(int,old.split('.'))):
            raise ValueError('LOCAL_GATE')
        if action=='repair' and (install['sha']!=job['sourceSha'] or old!=job['version']):
            raise ValueError('LOCAL_GATE')
        if not gateway.idle(bridge):
            raise ValueError('BUSY')
        client.local_doctor(directory,state)
        if action=='update' and not gateway.ready(config_path,old):
            raise ValueError('LOCAL_GATE')
        with admission(directory,exclusive=True):
            if not gateway.idle(bridge):
                raise ValueError('BUSY')
            confirm_execution(client,state,transport,job)
            manifest=client.installer.verify_payload(package)
            incoming=client.installer.bridge_payload(package,manifest)
            attempt={'jobId':job['id'],'digest':job['zipSha256']}
            intent=managed.backup(directory,install,incoming,client.installer,attempt,old,config_path)
            try:
                client.installer.update(package,directory/'install-receipt.json',state['installationId'])
                confirm_execution(client,state,transport,job)
                gateway.reload()
                if not gateway.wait(config_path,job['version'],8):
                    if not job['restart']:
                        raise ValueError('GATEWAY_BINDING_PENDING')
                    confirm_execution(client,state,transport,job)
                    intent['forwardRestart']=True
                    managed.atomic_json(directory/'managed-intent.json',intent)
                    if not gateway.restart(bridge) or not gateway.wait(config_path,job['version']):
                        raise ValueError('GATEWAY_BINDING_PENDING')
                client.local_doctor(directory,state)
                managed.finish(directory,intent,'applied')
            except Exception:
                if not gateway.idle(bridge):raise
                allow_recovery_restart=False
                if job['restart']:
                    try:
                        confirm_execution(client,state,transport,job)
                        allow_recovery_restart=True
                    except (OSError, ValueError, client.ClientError):
                        pass
                managed.recover(directory,intent,client.installer,gateway,bridge,{'restart':allow_recovery_restart})
                result=managed.read(directory/'managed-result.json',{})
                if result.get('state')=='restored':
                    raise ValueError('RESTORED') from None
                raise
    finally:
        bridge.transport.close()


def publish(client,directory,state,transport):
    assist = state['assisted']
    intent = assist['intent']
    answer = receipt(client,state,transport,intent,intent['event'],intent['code'])
    terminal='succeeded' if intent['event']=='complete' else 'uncertain' if intent['code']=='UNCERTAIN' else 'failed'
    if (answer.get('code')!=intent['code'] or answer.get('authorized') is not False
            or answer.get('state')!=terminal):
        raise ValueError('ASSISTED_RECEIPT_PENDING')
    assist['last'] = {'action':intent['action'],'code':intent['code']}
    assist['lastJob'] = intent['id']
    del assist['intent']
    client.save_state(directory,state)
    if intent['action']=='install' and intent['code']=='INSTALLED':
        enable_after_install(client,directory,state)
    if not assist['maintenance'] and intent['code'] in ('INSTALLED','UNCERTAIN'):
        unschedule(client,directory,state)
    return status_view(state)


def finish(client,directory,state,transport,event,code):
    state['assisted']['intent'].update({'event':event,'code':code})
    client.save_state(directory,state)
    return publish(client,directory,state,transport)


def reconcile(client,directory,state,transport,job,managed):
    assist = state['assisted']
    intent = assist['intent']
    if 'event' in intent:
        return publish(client,directory,state,transport)
    if not job or (job.get('id'),job.get('epoch'))!=(intent['id'],intent['epoch']):
        return status_view(state)
    if job.get('state')=='waiting':
        # The server has no execution; close this exact job without replaying begin.
        return finish(client,directory,state,transport,'fail','LOCAL_GATE')
    if job.get('state')!='executing':
        return status_view(state)
    if (intent['action']=='install' and state.get('installed') and
            all(job.get(k)==intent.get(k) for k in ('accountId','sourceSha','version'))):
        install,bridge=owned(client,directory,state,job)
        try:
            if install['sha']==intent['sourceSha'] and managed.installer_version(install)==intent['version']:
                gateway=managed.Gateway(state,client,install)
                if gateway.ready(directory/'bridge.json',intent['version']) and acknowledge_install(client,directory,state,transport,bridge,job):
                    return finish(client,directory,state,transport,'complete','INSTALLED')
                intent['pending']='PLUGIN_INSTALLED_GATEWAY_OR_ACK_PENDING'
                client.save_state(directory,state)
                return status_view(state)
        finally:
            bridge.transport.close()
    rollback = managed.read(directory/'managed-intent.json')
    code = 'UNCERTAIN'
    if (rollback and rollback.get('jobId')==intent['id'] and rollback.get('digest')==intent['digest']
            and intent['action'] in ('update','repair')):
        # Partial plugin writes invalidate the normal receipt. The rollback
        # journal checks each owned path and every byte against old/new hashes.
        if not re.fullmatch(r'managed-rollback-[A-Za-z0-9_-]+',rollback.get('backup','')):
            raise ValueError('UPDATE_BACKUP_INVALID')
        before = managed.read(directory/rollback['backup']/'receipt.json')
        home,_=check_binding(client,state)
        if (before.get('installationId')!=state.get('installationId') or before.get('privateRoot')!=str(directory)
                or before.get('config')!=str(directory/'bridge.json') or
                [Path(p['path']) for p in before['profiles']]!=[home/'config.yaml']):
            raise ValueError('INSTALLATION_OWNERSHIP_MISMATCH')
        bridge=Bridge.from_file(directory/'bridge.json')
        try:
            gateway=managed.Gateway(state,client,before)
            with admission(directory,exclusive=True):
                if not gateway.idle(bridge):return status_view(state)
                managed.recover(directory,rollback,client.installer,gateway,bridge,{'restart':False})
            if managed.read(directory/'managed-result.json',{}).get('state')=='restored':
                code='RESTORED'
        finally:
            bridge.transport.close()
    return finish(client,directory,state,transport,'fail',code)


def tick(client,directory,state,transport):
    assist = state['assisted']
    if assist.get('withdrawn'):
        if assist.get('cleanupPending'):unschedule(client,directory,state)
        return status_view(state)
    if not assist['maintenance'] and (assist.get('last',{}).get('code') in ('INSTALLED','UNCERTAIN') or
                                      time.time()-assist['createdAt']>=DAY):
        unschedule(client,directory,state)
        return status_view(state)
    answer = refresh(client,directory,state,transport)
    if assist.get('withdrawn') or state['remoteState'] in TERMINAL:
        unschedule(client,directory,state)
        return status_view(state)
    if state['remoteState']=='active' and assist.get('last',{}).get('code')=='INSTALLED':
        enable_after_install(client,directory,state)
    job = answer.get('assistedJob')
    managed = client.helper('managed-update')
    if assist.get('intent'):
        return reconcile(client,directory,state,transport,job,managed)
    if not job:
        return status_view(state)
    validate_job(client,state,job,managed,binding_only=True)
    if job['state']!='waiting' or job['id']==assist.get('lastJob'):
        return {'stage':'RECONCILIATION_REQUIRED','qualified':False}
    # No untrusted command, argument, URL, home or destination is accepted.
    # Each fixed action writes intent before begin; a lost reply never replays.
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        package=Path(temporary)
        summary={**{k:job[k] for k in ('id','epoch','action','accountId','version','sourceSha')},'digest':job['zipSha256']}
        try:
            m=validate_job(client,state,job,managed)
            home,config=check_binding(client,state)
            if job['action']=='install' and not client.plugin_available(home,config):
                raise ValueError('LOCAL_GATE')
            if job['action']!='install':
                install,bridge=owned(client,directory,state,job)
                try:
                    old=managed.installer_version(install)
                    if job['action'] in ('repair','diagnose') and (install['sha']!=job['sourceSha'] or old!=job['version']):
                        raise ValueError('LOCAL_GATE')
                    if job['action']=='update' and tuple(map(int,old.split('.')))>=tuple(map(int,job['version'].split('.'))):
                        raise ValueError('LOCAL_GATE')
                    if job['action'] in ('update','repair') and not managed.Gateway(state,client,install).idle(bridge):
                        return {**status_view(state),'pending':'GATEWAY_BUSY'}
                finally:
                    bridge.transport.close()
            if job['action']!='diagnose':
                managed.download(m,package,client.installer)
        except ValueError as exc:
            reason='PACKAGE_INVALID' if str(exc).startswith(('UPDATE_', 'PACKAGE_','ASSISTED_RELEASE_')) else 'LOCAL_GATE'
            assist['intent']=summary
            return finish(client,directory,state,transport,'fail',reason)
        if time.time()*1000>=job['deadlineAt']:
            raise ValueError('ASSISTED_JOB_EXPIRED')
        assist['intent']=summary
        client.save_state(directory,state)
        begun=receipt(client,state,transport,job,'begin','READY')
        if begun.get('authorized') is not True or begun.get('state')!='executing':
            return status_view(state)
        try:
            perform(client,directory,state,transport,job,package,managed)
        except Exception as exc:
            code=str(exc)
            if code=='ASSISTED_JOB_CHANGED':
                refresh(client,directory,state,transport)
                return status_view(state)
            if job['action']=='install' and state.get('installed'):
                assist['intent']['pending']='PLUGIN_INSTALLED_GATEWAY_OR_ACK_PENDING'
                client.save_state(directory,state)
                return status_view(state)
            rollback=managed.read(directory/'managed-intent.json')
            if rollback and rollback.get('jobId')==job['id'] and rollback.get('digest')==job['zipSha256']:
                assist['intent']['pending']='OWNED_ROLLBACK_PENDING'
                client.save_state(directory,state)
                return status_view(state)
            known={'BUSY','LOCAL_GATE','RESTORED','WECHAT_BINDING_CHANGED','INSTALLATION_OWNERSHIP_MISMATCH'}
            if code not in known:
                code='UNCERTAIN'
            elif code=='WECHAT_BINDING_CHANGED':
                code='PROFILE_UNAVAILABLE'
            elif code=='INSTALLATION_OWNERSHIP_MISMATCH':
                code='LOCAL_GATE' if job['action']!='install' or not state.get('installed') else 'UNCERTAIN'
            return finish(client,directory,state,transport,'fail',code)
        return finish(client,directory,state,transport,'complete',ACTIONS[job['action']])


def run(args,client):
    if args.peer or args.manage or args.device_label or args.pilot_updates or args.restart_gateway or args.auth!='discord' or args.wait:
        raise ValueError('ASSISTED_OPTIONS_INVALID')
    if args.action!='assist-setup' and (args.allow_maintenance or args.allow_idle_restart):
        raise ValueError('ASSISTED_CONSENT_SETUP_ONLY')
    path=Path(args.state_dir).expanduser().absolute()
    if args.action=='assist-status' and not path.exists() and not path.is_symlink():
        return {'stage':'ABSENT','qualified':False}
    if args.action=='assist-welcome':
        return client.helper('assist-welcome').welcome(args,client)
    directory=client.private_directory(path)
    pending=directory/'enrollment.json'
    if args.action=='assist-status' and not pending.exists() and not pending.is_symlink():
        return {'stage':'ABSENT','qualified':False}
    with client.state_lock(directory):
        transport=client.ConnectTransport()
        if args.action=='assist-setup':
            return setup(client,directory,args,transport)
        state=client.load_state(directory)
        if not state:
            if args.action=='assist-status':return {'stage':'ABSENT','qualified':False}
            raise ValueError('ASSISTED_SETUP_REQUIRED')
        if not state.get('assisted'):
            if args.action=='assist-status':return {'stage':'EXISTING_UNMANAGED','qualified':False}
            raise ValueError('EXISTING_INSTALLATION_REQUIRES_OWNING_UPGRADE_PATH')
        if args.profile is not None and args.profile!=state['profile']:
            raise ValueError('EXISTING_ENROLLMENT_SELECTION_MISMATCH')
        if args.action=='assist-status':
            if not state['assisted'].get('withdrawn'):
                try:refresh(client,directory,state,transport)
                except client.ClientError as exc:
                    if exc.code not in ('CONNECT_UNAVAILABLE','CONNECT_DEADLINE','CONNECT_HTTP_502','CONNECT_HTTP_503','CONNECT_HTTP_504'):raise
                    view=status_view(state)
                    return {**view,'stage':'OFFLINE','lastKnownStage':view['stage']}
            return status_view(state)
        if args.action=='assist-revoke':
            return withdraw(client,directory,state,transport)
        lock=Path(state['hermesRoot'])/'asbot-managed-update'
        secure_dir(lock)
        with client.state_lock(lock):
            return tick(client,directory,state,transport)
