"""Antigravity's private per-request custom agent and CLI-native tool guard."""
import json

from .errors import ConfigurationError

AGY_VERSION = '1.4.12'
# Entirely constant shell text: no subtitle, model, path or environment interpolation.
DENY_COMMAND = "printf '%s\\n' '{\"decision\":\"deny\",\"reason\":\"Subtitle translation cannot use tools\"}'"


def prepare_agy(cwd, name, system_prompt, private_file):
    agents = cwd / '.agents' / 'agents'
    agents.parent.mkdir(mode=0o700)
    agents.mkdir(mode=0o700)
    hooks_path = (agents.parent / 'hooks.json').resolve()
    private_file(agents / (name + '.md'),
                 f'---\nname: {name}\ndescription: Translate supplied subtitle text only.\n'
                 'excludeDefaultComponents: true\ninheritCustomizations: false\n'
                 # hooks selects files, not entry names. Discovery alone does
                 # not attach the guard when customization inheritance is off.
                 f'hooks: {json.dumps([str(hooks_path)])}\n'
                 'tools: []\nmainAgent: true\nsubagent: false\ninheritMcp: false\n'
                 'mcpServers: []\nskills: []\nplugins: []\ncommandExecutionPolicy: "off"\n'
                 '---\n# System Prompt\n' + system_prompt + '\nDo not invoke tools.\n')
    private_file(cwd / 'settings.json', json.dumps({
        'agentMode': '', 'toolPermission': 'strict', 'allowNonWorkspaceAccess': False,
        'useG1Credits': False,
    }))
    private_file(hooks_path, json.dumps({name: {
        'enabled': True, 'PreToolUse': [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': DENY_COMMAND, 'timeout': 5}]}],
    }}))
    return ['--settings', str(cwd / 'settings.json'), '--agent', name]


def verify_agy_guard(raw, name):
    """Check discovery; prepare_agy must separately select the guard's file.

    /hooks does not prove attachment to the selected agent or tool execution.
    """
    rows = [line.split('\t') for line in raw.splitlines()]
    expected = [name, 'enabled', 'PreToolUse', '*', 'command', DENY_COMMAND]
    matching = [row for row in rows if row and row[0] == name]
    if matching != [expected]:
        raise ConfigurationError('Antigravity translation tool guard was not registered; no translation was attempted')
