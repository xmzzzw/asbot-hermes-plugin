#!/usr/bin/env python3
"""Private, recoverable AS-BOT onboarding for an existing Hermes installation."""
from __future__ import annotations

import argparse
import contextlib
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import html
import http.client
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.parse
import uuid
import webbrowser
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asbot_bridge.private_io import secure_file, secure_dir, secure_open, lock_exclusive

spec = importlib.util.spec_from_file_location('asbot_package_installer', ROOT/'bin/install-package.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
CONNECT_ORIGIN = 'https://asbot-connect.zhouzhou.edu.kg'
BUSINESS_ORIGIN = 'https://asbot.zhouzhou.edu.kg'
CLIENT_VERSION = '1.3.0'
PAIR_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
STATES = {'awaiting_owner', 'awaiting_identity', 'awaiting_eligibility', 'provisioning',
          'ready', 'active', 'uncertain', 'expired', 'revoked', 'denied'}
UUID4 = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}')


class ClientError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def clean_text(value, limit=512):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def private_directory(path):
    path = Path(path).expanduser().absolute()
    # Never follow an existing symlink while creating private credential state.
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ClientError('PRIVATE_DIRECTORY_REQUIRED')
    if path == ROOT or ROOT in path.parents:
        raise ClientError('STATE_OUTSIDE_PACKAGE_REQUIRED')
    for ancestor in (path, *path.parents):
        if (ancestor/'.git').exists():
            raise ClientError('STATE_OUTSIDE_REPOSITORY_REQUIRED')
    secure_dir(path)
    return path


def default_state_dir():
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))
    elif sys.platform == 'darwin':
        base = Path.home()/'Library/Application Support'
    else:
        base = Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))
    return base/'asbot-client'


@contextmanager
def state_lock(directory):
    fd = secure_open(directory/'client.lock', os.O_CREAT | os.O_RDWR)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        try:
            lock_exclusive(fd)
        except BlockingIOError:
            raise ClientError('CLIENT_BUSY') from None
        yield
    finally:
        os.close(fd)


def load_state(directory):
    path = directory/'enrollment.json'
    if not path.exists() and not path.is_symlink():
        return None
    secure_file(path)
    if path.stat().st_size > 65536:
        raise ClientError('LOCAL_STATE_INVALID')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or not UUID4.fullmatch(value.get('enrollmentId', ''))
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value.get('recovery', ''))
            or not re.fullmatch(r'[A-HJ-NP-Z2-9]{12}', value.get('pairingCode', ''))
            or not clean_text(value.get('peer')) or not clean_text(value.get('profile'), 64)
            or not Path(value.get('hermesRoot', '')).is_absolute()):
        raise ClientError('LOCAL_STATE_INVALID')
    return value


def save_state(directory, state):
    installer.atomic(directory/'enrollment.json', json.dumps(state, ensure_ascii=False).encode())


class ConnectTransport:
    """Fixed TLS origin, no ambient proxy, redirects, body logging or write replay."""
    def request(self, method, path, recovery, payload=None):
        if (method not in ('GET', 'POST') or not re.fullmatch(
                r'/connect/v1/enrollments(?:/[a-f0-9-]{36}(?:/(?:claim|oauth|credentials|ack|proof-code))?)?', path)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', recovery)):
            raise ClientError('INVALID_CONNECT_REQUEST')
        body = None if payload is None else json.dumps(payload, separators=(',', ':')).encode()
        if body is not None and len(body) > 4096:
            raise ClientError('INVALID_CONNECT_REQUEST')
        deadline = time.monotonic() + 15
        for attempt in range(2 if method == 'GET' else 1):
            connection = None
            try:
                if time.monotonic() >= deadline:
                    raise ClientError('CONNECT_DEADLINE')
                connection = http.client.HTTPSConnection('asbot-connect.zhouzhou.edu.kg',
                    timeout=max(0.1, deadline-time.monotonic()), context=ssl.create_default_context())
                connection.request(method, path, body, {'Authorization': 'Enrollment '+recovery,
                    'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': 'asbot-client/'+CLIENT_VERSION})
                if connection.sock:
                    connection.sock.settimeout(max(0.1, deadline-time.monotonic()))
                response = connection.getresponse()
                if response.status in (502, 503, 504) and method == 'GET' and attempt == 0:
                    continue
                if response.status not in (200, 201, 202):
                    raise ClientError('CONNECT_HTTP_'+str(response.status))
                if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    raise ClientError('CONNECT_RESPONSE_INVALID')
                length = response.getheader('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) > 65536):
                    raise ClientError('CONNECT_RESPONSE_TOO_LARGE')
                data = bytearray()
                while True:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise ClientError('CONNECT_DEADLINE')
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(8192, 65537-len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise ClientError('CONNECT_RESPONSE_TOO_LARGE')
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ClientError('CONNECT_RESPONSE_INVALID')
                return value
            except ClientError:
                raise
            except (OSError, http.client.HTTPException):
                if method != 'GET' or attempt or time.monotonic() >= deadline:
                    raise ClientError('CONNECT_TRANSPORT_UNCERTAIN' if method == 'POST' else 'CONNECT_UNAVAILABLE') from None
            except (ValueError, UnicodeError):
                raise ClientError('CONNECT_RESPONSE_INVALID') from None
            finally:
                if connection is not None:
                    connection.close()
        raise ClientError('CONNECT_UNAVAILABLE')


def native_compatibility():
    """Probe public extension capabilities, never an exact Hermes source revision."""
    probe = r'''
import contextlib, importlib.metadata, io, json
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from hermes_cli.plugins import PluginContext, VALID_HOOKS
    from gateway.session_context import get_session_env
    compatible = (all(callable(getattr(PluginContext, k, None)) for k in ('get_config','register_tool','register_hook'))
                  and {'post_gateway_admission','transform_llm_output'} <= set(VALID_HOOKS)
                  and callable(get_session_env))
    version = 'unknown'
    for name in ('hermes-agent','hermes_agent'):
        try:
            version = importlib.metadata.version(name)
            break
        except importlib.metadata.PackageNotFoundError:
            pass
print(json.dumps({'compatible': compatible, 'version': version}))
'''
    try:
        result = subprocess.run([sys.executable, '-c', probe], cwd=hermes_python_source(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=20, check=False)
        value = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        value = {}
    if value.get('compatible') is not True:
        raise ClientError('HERMES_PLUGIN_API_UNAVAILABLE_USE_EXISTING_HERMES_PYTHON')
    version = value.get('version', 'unknown')
    return version if clean_text(version, 100) else 'unknown'


def hermes_python_source():
    """Official source installs keep the chosen Python venv under their checkout.

    Resolve only from that interpreter, not the current directory or a download.
    Packaged Python installs can import Hermes normally without a cwd override.
    """
    candidate = Path(sys.prefix).parent
    if all((candidate/name).is_file() for name in ('pyproject.toml', 'hermes_cli/plugins.py',
                                                   'gateway/session_context.py')):
        return candidate
    return None


def choose(values, label, explicit=None):
    values = sorted(values)
    if explicit is not None:
        if explicit not in values:
            raise ClientError('SELECTED_'+label+'_NOT_AVAILABLE')
        return explicit
    if len(values) == 1:
        return values[0]
    if not values:
        raise ClientError('NO_EXISTING_'+label)
    if not sys.stdin.isatty():
        raise ClientError(label+'_SELECTION_REQUIRED')
    # Peer IDs stay local; this list is never sent to the connect service.
    for index, value in enumerate(values, 1):
        print(str(index)+'. '+value, file=sys.stderr)
    answer = input('选择 '+label+' 的序号：').strip()
    if not answer.isdecimal() or not 1 <= int(answer) <= len(values):
        raise ClientError('INVALID_SELECTION')
    return values[int(answer)-1]


def select_peer(hermes_root=None, profile=None, peer=None):
    hermes = Path(hermes_root or os.environ.get('HERMES_HOME') or Path.home()/'.hermes').expanduser().absolute()
    if hermes.is_symlink() or not hermes.is_dir():
        raise ClientError('EXISTING_HERMES_REQUIRED')
    homes = {'default': hermes}
    profiles = hermes/'profiles'
    if profiles.is_dir() and not profiles.is_symlink():
        homes.update({p.name:p for p in profiles.iterdir() if p.is_dir() and not p.is_symlink()
                      and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', p.name) and p.name != 'default'})
    admitted = {}
    for name, home in homes.items():
        if not (home/'config.yaml').is_file():
            continue
        installer.local.load_yaml(home/'config.yaml')
        try:
            admitted[name] = installer.local.approved_peers(home)
        except ValueError as error:
            if str(error) != 'NO_EXISTING_APPROVED_WEIXIN_PEERS':
                raise
    selected = choose(admitted, 'PROFILE', profile)
    selected_peer = choose(admitted[selected], 'PEER', peer)
    return hermes, selected, selected_peer


def base_path(state):
    return '/connect/v1/enrollments/'+state['enrollmentId']


def request(transport, state, suffix='', method='GET', payload=None):
    return transport.request(method, base_path(state)+suffix, state['recovery'], payload)


def status(directory, state, transport):
    answer = request(transport, state)
    stage = answer.get('state')
    if stage not in STATES:
        raise ClientError('CONNECT_STATE_INVALID')
    state['remoteState'] = stage
    state['created'] = True
    state['invitationClaimed'] = answer.get('invitationClaimed') is True
    state['identityVerified'] = answer.get('identityVerified') is True
    state['identityDeadlineAt'] = answer.get('identityDeadlineAt')
    version=answer.get('credentialVersion')
    if stage in ('ready','active'):
        if type(version) is not int or version<1:
            raise ClientError('CREDENTIAL_VERSION_INVALID')
        state['remoteCredentialVersion']=version
    if state['identityVerified'] or time.time()-state.get('browserHandoffCreatedAt',0) >= 600:
        (directory/'browser-handoff.html').unlink(missing_ok=True)
    if state.get('proofRefreshPending') and state['identityDeadlineAt'] != state.get('proofRefreshDeadline'):
        state['proofRefreshPending'] = False
    save_state(directory, state)
    return answer


def report(state, answer=None):
    answer = answer or {}
    result = {'stage': state.get('remoteState', 'local_setup').upper(), 'enrollmentId': state['enrollmentId'],
              'clientVersion': CLIENT_VERSION, 'qualified': False,
              'product': 'AS-BOT Hermes 插件',
              'nativeMedia': 'PENDING_REAL_RECEIPT',
              'transportMode': state.get('transportMode', 'legacy'),
              'busyCommandLane': 'HERMES_NATIVE_POLICY',
              'repeatedInboundCommands': 'HERMES_NATIVE_POLICY',
              'oldImageRecovery': 'HERMES_NATIVE_POLICY'}
    if state.get('remoteState') in ('awaiting_owner', 'awaiting_identity') and not state.get('identityVerified'):
        result['identityCommand'] = '/接入确认 '+state['pairingCode']
        result['identityDeadlineAt'] = state.get('identityDeadlineAt')
    if state.get('installed'):
        result['localInstall'] = 'INSTALLED'
        result['pluginReload'] = state.get('pluginReload','PLUGIN_RELOAD_PENDING')
        result['gateway'] = 'PLUGIN_RELOADED' if result['pluginReload']=='RELOADED' else 'PLUGIN_RELOAD_PENDING'
        result['credentialVersion']=state.get('credentialVersion')
        if state.get('credentialVersion')!=state.get('remoteCredentialVersion'):
            result['credentialUpdate']='PENDING_ACTIVATION'
    # Do not echo arbitrary service strings, account metadata, peer IDs or secrets.
    reason = answer.get('reasonCode') or answer.get('code')
    if reason and re.fullmatch(r'[A-Z0-9_]{1,100}', str(reason)):
        result['code'] = reason
    if state.get('ackAttemptedVersion')==state.get('remoteCredentialVersion') and state.get('remoteState') == 'ready':
        result['acknowledgement'] = 'UNCERTAIN_CONTACT_OWNER'
    return result


def refresh_proof(directory, state, transport):
    expiry = state.get('identityDeadlineAt')
    if (state.get('remoteState') not in ('awaiting_owner', 'awaiting_identity')
            or state.get('identityVerified') or not isinstance(expiry, str)):
        return
    try:
        expired = datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        raise ClientError('CONNECT_STATE_INVALID') from None
    if not expired:
        return
    # Persist intent before the write. A lost reply keeps the same challenge.
    if state.get('proofRefreshPending'):
        raise ClientError('PROOF_REFRESH_UNCERTAIN_CONTACT_OWNER')
    state['pairingCode'] = ''.join(secrets.choice(PAIR_ALPHABET) for _ in range(12))
    state['proofRefreshPending'] = True
    state['proofRefreshDeadline'] = expiry
    save_state(directory, state)
    request(transport, state, '/proof-code', 'POST', {'pairingCode': state['pairingCode']})
    state['proofRefreshPending'] = False
    save_state(directory, state)
    status(directory, state, transport)


def hidden_invite(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ClientError('HIDDEN_TERMINAL_INPUT_REQUIRED') from None


def setup(directory, transport, *, hermes_root=None, profile=None, peer=None, device_label=None, invite_input=None):
    manifest = installer.verify_payload(ROOT)
    state = load_state(directory)
    if state is None:
        hermes, selected, selected_peer = select_peer(hermes_root, profile, peer)
        home = hermes if selected == 'default' else hermes/'profiles'/selected
        current = installer.local.load_yaml(home/'config.yaml')
        plugins = current.get('plugins', {})
        if (not isinstance(plugins, dict) or 'asbot-bridge' in plugins.get('enabled', [])
                or 'asbot-bridge' in plugins.get('entries', {})
                or (home/'plugins/asbot-bridge').exists() or (home/'plugins/asbot-bridge').is_symlink()):
            raise ClientError('EXISTING_PLUGIN_REQUIRES_OWNING_UPGRADE_PATH')
        version = native_compatibility()
        label = device_label or 'Hermes '+sys.platform
        if not clean_text(label, 100):
            raise ClientError('DEVICE_LABEL_INVALID')
        state = {'enrollmentId': str(uuid.uuid4()), 'recovery': secrets.token_urlsafe(32),
                 'pairingCode': ''.join(secrets.choice(PAIR_ALPHABET) for _ in range(12)),
                 'hermesRoot': str(hermes), 'profile': selected, 'peer': selected_peer,
                 'deviceLabel': label, 'hermesVersion': version, 'platform': sys.platform,
                 'transportMode': 'official',
                 'clientVersion': CLIENT_VERSION+'+'+manifest['sha'], 'created': False}
        save_state(directory, state)
    else:
        for supplied, stored in ((profile, state['profile']), (peer, state['peer']),
                                 (str(Path(hermes_root).expanduser().absolute()) if hermes_root else None, state['hermesRoot'])):
            if supplied is not None and supplied != stored:
                raise ClientError('EXISTING_ENROLLMENT_SELECTION_MISMATCH')
    if not state.get('createAttempted'):
        state['createAttempted'] = True
        save_state(directory, state)
        payload = {key:state[key] for key in ('enrollmentId','deviceLabel','platform','hermesVersion','clientVersion','pairingCode')}
        transport.request('POST', '/connect/v1/enrollments', state['recovery'], payload)
    answer = status(directory, state, transport)
    if not state.get('invitationClaimed') and not state.get('claimAttempted') and state['remoteState'] == 'awaiting_owner':
        # Never accept invitation/recovery/credentials as command line arguments.
        code = (invite_input or hidden_invite)('一次性邀请码（隐藏输入；直接回车可由管理员审批）：').strip()
        if code:
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', code):
                raise ClientError('INVITATION_CODE_INVALID')
            state['claimAttempted'] = True
            save_state(directory, state)
            request(transport, state, '/claim', 'POST', {'code': code})
            answer = status(directory, state, transport)
    refresh_proof(directory, state, transport)
    return report(state, answer)


def validate_credentials(value):
    if (not isinstance(value, dict) or value.get('origin') != BUSINESS_ORIGIN
            or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value.get(k, '')) for k in ('accountId','installationId'))
            or type(value.get('credentialVersion')) is not int or value['credentialVersion']<1
            or not isinstance(value.get('access'), dict)
            or not all(clean_text(value['access'].get(k), 4096) for k in ('client_id','client_secret'))
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value.get('bearerToken',''))):
        raise ClientError('CREDENTIAL_BUNDLE_INVALID')
    try:
        expires = datetime.fromisoformat(value['expiresAt'].replace('Z', '+00:00'))
        if expires <= datetime.now(timezone.utc):
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ClientError('CREDENTIAL_BUNDLE_EXPIRED') from None
    return value


def local_doctor(directory, state):
    if not state.get('installed'):
        raise ClientError('ACTIVATION_REQUIRED')
    installer.owned_receipt(directory/'install-receipt.json', state['installationId'])
    result = installer.doctor(directory/'bridge.json')
    if not result.get('checks') or not all(c.get('authorized') is True for c in result['checks']):
        raise ClientError('API_AUTHORIZATION_PENDING')
    return result


def reload_existing_plugins(state):
    """Use the already-running Hermes control socket; never start or restart it."""
    hermes=Path(state['hermesRoot'])
    home=hermes if state['profile']=='default' else hermes/'profiles'/state['profile']
    try:
        source = hermes_python_source()
        if source is not None and str(source) not in sys.path:
            sys.path.insert(0, str(source))
        # Native diagnostic output may contain local private details.
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            from gateway.control_socket import reload_gateway_plugins
            answer=reload_gateway_plugins(home)
            if answer is None and home!=hermes:
                answer=reload_gateway_plugins(hermes,profile_home=home)
        return 'RELOADED' if isinstance(answer,dict) and answer.get('reloaded') is True else 'PLUGIN_RELOAD_PENDING'
    except Exception:
        return 'PLUGIN_RELOAD_PENDING'


def activate(directory, state, transport, *, wait=0, auth='discord'):
    if state.get('transportMode') != 'official':
        raise ClientError('LEGACY_PLUGIN_MIGRATION_REQUIRED')
    native_compatibility()
    deadline = time.monotonic()+wait
    opened = False
    while True:
        answer = status(directory, state, transport)
        refresh_proof(directory, state, transport)
        stage = state['remoteState']
        if auth == 'web' and not opened and stage in ('awaiting_owner', 'awaiting_identity') and not state.get('identityVerified'):
            oauth = request(transport, state, '/oauth', 'POST', {})
            url = oauth.get('url')
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
            if (parsed is None or parsed.scheme != 'https' or parsed.netloc != 'asbot-connect.zhouzhou.edu.kg'
                    or parsed.username or parsed.password or parsed.fragment or not parsed.path.startswith('/connect/')):
                raise ClientError('OAUTH_URL_INVALID')
            # Give the browser a private local file path. The one-use ticket is
            # inside that file, never a browser process's command arguments.
            handoff = directory/'browser-handoff.html'
            target = html.escape(url, quote=True)
            installer.atomic(handoff, ('<!doctype html><meta charset="utf-8">'
                '<meta name="referrer" content="no-referrer">'
                '<meta http-equiv="refresh" content="0;url='+target+'">'
                '<a href="'+target+'">继续 Discord 身份验证</a>').encode())
            state['browserHandoffCreatedAt'] = time.time()
            save_state(directory,state)
            if not webbrowser.open(handoff.as_uri()):
                raise ClientError('BROWSER_OPEN_FAILED_USE_DISCORD_COMMAND')
            opened = True
        if stage in ('ready','active'):
            break
        if stage in ('uncertain','expired','revoked','denied') or time.monotonic() >= deadline:
            return report(state, answer)
        time.sleep(min(3, max(0, deadline-time.monotonic())))
    if stage=='ready':
        # Each ready generation is read fresh: a cached previous credential
        # bundle must never hide an Owner rotation of this same installation.
        bundle=validate_credentials(request(transport,state,'/credentials'))
        if bundle['credentialVersion']!=state['remoteCredentialVersion']:
            raise ClientError('CREDENTIAL_VERSION_CHANGED_RETRY_STATUS')
        if bundle['credentialVersion']<state.get('credentialVersion',0):
            raise ClientError('CREDENTIAL_VERSION_ROLLBACK_REFUSED')
        if state.get('installed') and bundle['installationId']!=state.get('installationId'):
            raise ClientError('INSTALLATION_OWNERSHIP_MISMATCH')
        installer.atomic(directory/'credentials.json',json.dumps(bundle).encode())
        if state.get('installed'):
            installer.rotate_credentials(directory/'install-receipt.json',state['installationId'],bundle)
        if state.get('credentialVersion')!=bundle['credentialVersion']:
            state['ackAttempted']=False
            state.pop('ackAttemptedVersion',None)
            state['pluginReload']='PLUGIN_RELOAD_PENDING'
        state['credentialVersion']=bundle['credentialVersion']
        save_state(directory,state)
    if not state.get('installed'):
        if stage != 'ready':
            raise ClientError('ACTIVE_DEVICE_LOCAL_CREDENTIALS_MISSING_CONTACT_OWNER')
        state['installationId'] = bundle['installationId']
        config = {'external_access': True, 'account_id': bundle['accountId'],
                  'installation_id': bundle['installationId'], 'origin': bundle['origin'],
                  'state_dir': str(directory/'bridge-state'), 'access': bundle['access'],
                  'principals': [{'profile': state['profile'], 'user_id': state['peer'], 'chat_id': state['peer'],
                                  'bearer_token': bundle['bearerToken'], 'enabled': True}]}
        pending = directory/'pending-bridge.json'
        installer.atomic(pending, json.dumps(config, ensure_ascii=False).encode())
        receipt = directory/'install-receipt.json'
        if receipt.exists():
            installer.owned_receipt(receipt, bundle['installationId'])
        else:
            installer.install(ROOT, Path(state['hermesRoot']), pending, [state['profile']], True,
                              installation_id=bundle['installationId'], private_root=directory)
        state['installed'] = True
        save_state(directory, state)
        pending.unlink(missing_ok=True)
    if state.get('credentialVersion')!=state.get('remoteCredentialVersion'):
        raise ClientError('LOCAL_CREDENTIAL_GENERATION_MISMATCH_CONTACT_OWNER')
    if state.get('pluginReload')!='RELOADED':
        state['pluginReload']=reload_existing_plugins(state)
        save_state(directory,state)
    if state['pluginReload']!='RELOADED':
        return report(state,answer)
    local_doctor(directory, state)  # authenticated /me must pass before acknowledgement
    if stage != 'active' and state.get('ackAttemptedVersion')!=state['credentialVersion']:
        config = json.loads((directory/'bridge.json').read_text())
        state['ackAttempted'] = True
        state['ackAttemptedVersion']=state['credentialVersion']
        save_state(directory, state)
        request(transport, state, '/ack', 'POST', {'installationToken': config['principals'][0]['bearer_token']})
    answer = status(directory, state, transport)
    if state['remoteState'] == 'active':
        (directory/'credentials.json').unlink(missing_ok=True)
    return report(state, answer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup','activate','status','doctor','update','uninstall'])
    parser.add_argument('--state-dir', type=Path, default=default_state_dir())
    parser.add_argument('--hermes-root', type=Path)
    parser.add_argument('--profile')
    parser.add_argument('--peer', help='Exact already-approved local DM peer; never sent to AS-BOT')
    parser.add_argument('--device-label')
    parser.add_argument('--auth', choices=['discord','web'], default='discord')
    parser.add_argument('--wait', type=int, default=0, help='Bounded activation polling, 0..120 seconds')
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.wait <= 120:
            raise ClientError('WAIT_OUT_OF_RANGE')
        directory = private_directory(args.state_dir)
        with state_lock(directory):
            transport = ConnectTransport()
            if args.action == 'setup':
                result = setup(directory, transport, hermes_root=args.hermes_root,
                               profile=args.profile, peer=args.peer, device_label=args.device_label)
            else:
                state = load_state(directory)
                if state is None:
                    raise ClientError('SETUP_REQUIRED')
                if args.action == 'status':
                    answer = status(directory,state,transport)
                    refresh_proof(directory,state,transport)
                    result = report(state,answer)
                elif args.action == 'activate':
                    result = activate(directory,state,transport,wait=args.wait,auth=args.auth)
                elif args.action == 'doctor':
                    native_compatibility()
                    result = local_doctor(directory,state)
                elif args.action == 'update':
                    if state.get('transportMode') != 'official':
                        raise ClientError('LEGACY_PLUGIN_MIGRATION_REQUIRED')
                    native_compatibility()
                    result = installer.update(ROOT,directory/'install-receipt.json',state.get('installationId'))
                    state['pluginReload']=reload_existing_plugins(state)
                    save_state(directory,state)
                    result['pluginReload']=state['pluginReload']
                else:
                    installer.owned_receipt(directory/'install-receipt.json',state.get('installationId'))
                    result = installer.uninstall(directory/'install-receipt.json')
                    state['installed'] = False
                    state['uninstalled'] = True
                    save_state(directory,state)
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        code = getattr(error,'code',None)
        if code is None and type(error) is ValueError and re.fullmatch(r'[A-Z_]+',str(error)):
            code = str(error)
        print(json.dumps({'stage':'BLOCKED','code':code or 'CLIENT_ACTION_FAILED',
                          'qualified':False,'recovery':'LOCAL_STATE_RETAINED'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
