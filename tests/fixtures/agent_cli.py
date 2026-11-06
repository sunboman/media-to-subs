"""Synthetic installed-agent protocols. No inference or network requests."""
import json
import os
from pathlib import Path
import sys
import time


def main(provider, root):
    root = Path(root)
    args = sys.argv[1:]
    scenario = (root / 'scenario').read_text() if (root / 'scenario').exists() else 'success'
    if '--version' in args:
        print({'codex': 'codex-cli 0.166.0', 'claude': '2.1.300 (Claude Code)',
               'opencode': 'v2.2.5' if scenario != 'old' else 'v1.2.3',
               'agy': '1.4.12' if scenario != 'old' else '1.4.7'}[provider])
        return
    if '--help' in args:
        if scenario == 'unsupported':
            print('unsupported old CLI')
            return
        print('--ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox '
              '--json --output-schema --output-last-message --disable --enable --config --model --color '
              '--print --output-format --json-schema --tools --disallowedTools --safe-mode '
              '--setting-sources --strict-mcp-config --mcp-config --no-session-persistence --verbose '
              '--max-turns --standalone --format --agent --input-format --disable-slash-commands '
              '--print-timeout --settings', file=sys.stderr if provider == 'agy' else sys.stdout)
        return
    if provider == 'agy' and args[-1] == '/hooks':
        hooks = json.loads(Path('.agents/hooks.json').read_text())
        if scenario != 'missing_hook':
            for name, spec in hooks.items():
                handler = spec['PreToolUse'][0]
                print('\t'.join((name, 'enabled', 'PreToolUse', '*', 'command', handler['hooks'][0]['command'])))
        return
    prompt = sys.stdin.read()
    records = root / 'records.jsonl'
    prior = records.read_text().splitlines() if records.exists() else []
    record = {'args': args, 'prompt': prompt, 'cwd': os.getcwd(), 'pid': os.getpid(),
              'mode': os.stat('.').st_mode & 0o777, 'env': dict(os.environ)}
    config = Path('opencode.jsonc')
    if config.exists():
        record['config'] = json.loads(config.read_text())
    if provider == 'agy':
        name = args[args.index('--agent') + 1]
        record['agent_definition'] = Path('.agents/agents', name + '.md').read_text()
        record['hooks'] = json.loads(Path('.agents/hooks.json').read_text())
        record['settings'] = json.loads(Path(args[args.index('--settings') + 1]).read_text())
        record['file_modes'] = {str(p): p.stat().st_mode & 0o777 for p in Path('.').rglob('*')}
    with records.open('a') as stream:
        stream.write(json.dumps(record) + '\n')
    if scenario == 'hang':
        time.sleep(30)
    if scenario == 'flood':
        while True:
            sys.stdout.write('x' * 65536)
            sys.stdout.flush()
    if scenario == 'exit':
        print('synthetic-private-error', file=sys.stderr)
        sys.exit(23)
    if scenario == 'malformed':
        print('synthetic-private-error')
        return
    messages = json.loads(json.loads(prompt)['message']['content']) if provider == 'agy' else json.loads(prompt)
    data = json.loads(messages[1]['content'])
    translation = {'translations': [{'id': item['id'], 'text': '示例中文'} for item in reversed(data['subtitles'])]}
    if scenario == 'invalid' or (scenario == 'correct' and not prior):
        translation = {'translations': []}
    text = json.dumps(translation, ensure_ascii=False)
    def emit(data):
        print(json.dumps(data, ensure_ascii=False))
    if provider == 'codex':
        output = Path(args[args.index('--output-last-message') + 1])
        output.write_text(text if scenario != 'mismatch' else 'different')
        emit({'type': 'thread.started', 'thread_id': 'fixture-thread'})
        if scenario == 'warning':
            emit({'type': 'item.completed', 'item': {'type': 'error', 'message': 'synthetic-private-error'}})
        emit({'type': 'turn.started'})
        if scenario == 'tool':
            emit({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'private'}})
        emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})
        if scenario != 'incomplete':
            emit({'type': 'turn.failed' if scenario == 'failed' else 'turn.completed'})
    elif provider == 'claude':
        emit({'type': 'system', 'subtype': 'init', 'session_id': 'fixture-session'})
        blocks = [{'type': 'text', 'text': text}]
        if scenario == 'tool':
            blocks.append({'type': 'tool_use', 'id': 'fixture-tool', 'name': 'Bash'})
        emit({'type': 'assistant', 'message': {'content': blocks, 'stop_reason': 'end_turn'}})
        if scenario != 'incomplete':
            emit({'type': 'result', 'subtype': 'success', 'is_error': scenario == 'failed',
                  'result': text, 'stop_reason': 'end_turn'})
    elif provider == 'agy':
        # Real 1.4.12 reports a registry inventory even for a tool-free agent.
        captured_init = json.loads(Path(__file__).with_name('agy-1.4.12-text.jsonl').read_text().splitlines()[0])['init']
        emit({'event': 'init', 'conversation_id': 'fixture-session', 'init': {
            'cwd': os.getcwd(), 'tools': captured_init['tools'] if scenario != 'invalid_inventory' else [None],
            'agent': name, 'permission_mode': 'strict',
            **({'model': args[args.index('--model') + 1]} if '--model' in args else {})}})
        def step(index, kind, **fields):
            emit({'event': 'step_update', 'step_update': {'conversation_id': 'fixture-session',
                  'step_index': index, 'step_type': kind, 'state': 'DONE', **fields}})
        step(0, 'user_input')
        if scenario == 'tool':
            step(1, 'tool', tool_name='run_command', tool_info={'error': {'message': 'denied'}})
        step(2, 'agent_response', text_delta=text)
        if scenario != 'incomplete':
            emit({'event': 'result', 'result': {'conversation_id': 'fixture-session',
                  'status': 'ERROR' if scenario in ('failed', 'auth', 'quota') else 'SUCCESS',
                  'response': text, 'num_turns': 4 if scenario == 'hidden_retry' else 1,
                  **({'error': 'synthetic-private-error'} if scenario in ('auth', 'quota') else {})}})
    else:
        def event(kind, part):
            emit({'type': kind, 'sessionID': 'fixture-session', 'part':
                  {'sessionID': 'fixture-session', 'messageID': 'fixture-message', **part}})
        event('step_start', {'type': 'step-start'})
        if scenario == 'tool':
            event('tool_use', {'type': 'tool', 'state': {'status': 'completed'}})
        event('text', {'type': 'text', 'text': text})
        if scenario != 'incomplete':
            event('step_finish', {'type': 'step-finish', 'reason': 'length' if scenario == 'failed' else 'stop'})

