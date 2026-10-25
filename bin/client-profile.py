"""Client-only access to existing Hermes profile settings and admitted peers.

There is deliberately no login, model, search, approval or gateway management.
"""
import copy
import json

from asbot_bridge.bridge import private_file


def load_yaml(path):
    import yaml  # use the user's existing Hermes environment
    private_file(path)
    value = yaml.safe_load(path.read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError('PROFILE_CONFIG_INVALID')
    return value


def dump_yaml(value):
    import yaml
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).encode()


def approved_peers(home):
    peers = set()
    for path in (home/'platforms/pairing/weixin-approved.json', home/'pairing/weixin-approved.json'):
        if not path.exists():
            continue
        private_file(path)
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or any(not isinstance(key, str) or not key or
                len(key) > 512 or any(ord(c) < 32 for c in key) or not isinstance(info, dict)
                for key, info in value.items())):
            raise ValueError('APPROVED_PAIRING_STORE_INVALID')
        peers.update(value)
    if not peers:
        raise ValueError('NO_EXISTING_APPROVED_WEIXIN_PEERS')
    return peers


def merge_profile(config, profile, config_path, preserve_search=True):
    """Only add the named plugin and its Weixin toolset to a deep copy."""
    result = copy.deepcopy(config)
    plugins = result.setdefault('plugins', {})
    if not isinstance(plugins, dict):
        raise ValueError('PLUGIN_CONFIG_INVALID')
    enabled, disabled = plugins.setdefault('enabled', []), plugins.get('disabled', [])
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise ValueError('PLUGIN_ALLOWLIST_INVALID')
    if 'asbot-bridge' in disabled:
        raise ValueError('PLUGIN_EXPLICITLY_DISABLED')
    if 'asbot-bridge' not in enabled:
        enabled.append('asbot-bridge')
    entries = plugins.setdefault('entries', {})
    if not isinstance(entries, dict):
        raise ValueError('PLUGIN_ENTRIES_INVALID')
    entry = entries.setdefault('asbot-bridge', {})
    if not isinstance(entry, dict):
        raise ValueError('BRIDGE_PLUGIN_ENTRY_INVALID')
    settings = entry.setdefault('settings', {})
    if not isinstance(settings, dict):
        raise ValueError('BRIDGE_PLUGIN_SETTINGS_INVALID')
    settings.update({'profile': profile, 'config_path': str(config_path)})
    selections = result.setdefault('platform_toolsets', {})
    if not isinstance(selections, dict):
        raise ValueError('PLATFORM_TOOLSETS_INVALID')
    toolsets = selections.setdefault('weixin', ['hermes-weixin'])
    if not isinstance(toolsets, list) or any(not isinstance(item, str) for item in toolsets):
        raise ValueError('WEIXIN_TOOLSETS_INVALID')
    agent = result.get('agent') or {}
    if not isinstance(agent, dict) or 'asbot_bridge' in agent.get('disabled_toolsets', []):
        raise ValueError('BRIDGE_TOOLSET_EXPLICITLY_DISABLED')
    if 'asbot_bridge' not in toolsets:
        toolsets.append('asbot_bridge')
    return result
