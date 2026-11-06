"""Explicit installed-CLI translation. No shell, sessions, token copying or fallback.

These are CLI-native restrictions, not an OS containment boundary. The selected
CLI keeps access to its login store; managed policy continues to apply.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from uuid import uuid4

from ._agy import AGY_VERSION, prepare_agy, verify_agy_guard
from ._agy_protocol import agy_content
from ._agent_protocol import claude_content, codex_content, opencode_content
from .config import LLMConfig
from .errors import ConfigurationError, ProcessTimeoutError, ProviderError
from .runtime import Cancellation, run_process
from .types import Progress

_OUTPUT_LIMIT = 2 * 1024 * 1024
_MESSAGE_LIMIT = 1024 * 1024
_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["translations"],
           "properties": {"translations": {"type": "array", "items": {
               "type": "object", "additionalProperties": False, "required": ["id", "text"],
               "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}}
_CODEX_FEATURES = ("shell_tool", "apps", "plugins", "hooks", "multi_agent", "browser_use", "view_image", "skill_search")
_HELP_FLAGS = {
    "codex": ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
              "--sandbox", "--json", "--output-schema", "--output-last-message", "--disable", "--enable", "--config", "--model", "--color"),
    "claude": ("--print", "--output-format", "--tools", "--disallowedTools",
               "--safe-mode", "--setting-sources", "--strict-mcp-config", "--mcp-config",
               "--no-session-persistence", "--verbose", "--max-turns", "--model"),
    "opencode": ("--standalone", "--format", "--agent", "--model"),
    "agy": ("--input-format", "--output-format", "--disable-slash-commands",
            "--agent", "--model", "--print", "--print-timeout", "--settings"),
}


def agent_environment(provider: str, cwd: Path) -> dict[str, str]:
    """Keep ordinary OS/login locations, never inherit provider keys or CLI flags.

    An allowlist also excludes future MEDIA_TO_SUBS variables, shell/node/python
    startup injection, remote server URLs, session IDs, and other harness homes.
    Authentication files are never read or copied by this application.
    """
    names = {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
             "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
             "SSL_CERT_FILE", "SSL_CERT_DIR"}
    if provider == "codex":
        names.add("CODEX_HOME")
    elif provider == "claude":
        names.add("CLAUDE_CONFIG_DIR")
    env = {name: value for name, value in os.environ.items() if name in names}
    env.update(PWD=str(cwd), TMPDIR=str(cwd), NO_COLOR="1")
    if provider == "agy":
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    return env


def _private_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def _last_message(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MESSAGE_LIMIT or info.st_nlink != 1:
                raise ValueError
            data = stream.read(_MESSAGE_LIMIT + 1)
            if len(data) > _MESSAGE_LIMIT:
                raise ValueError
            return data.decode("utf-8")
    except (OSError, ValueError):
        raise ProviderError("Agent final response file is missing, unsafe or exceeds its size limit") from None


class AgentClient:
    def __init__(self, config: LLMConfig, cancellation: Cancellation, progress: Progress):
        if config.mode != "agent" or config.agent not in _HELP_FLAGS:
            raise ConfigurationError("Agent translation requires an explicit supported agent")
        if os.name != "posix":
            raise ConfigurationError("Agent translation currently requires macOS or Linux for owned process cleanup")
        binary = shutil.which(config.agent_binary or config.agent)
        if not binary:
            raise ConfigurationError("Translation agent executable unavailable; install and log into it separately or set --agent-binary")
        self.binary = str(Path(binary).absolute())
        self.config, self.cancellation, self.progress = config, cancellation, progress
        self.capable = False

    def _run(self, args, cwd, env, deadline, *, prompt=None, probe=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessTimeoutError("Agent exceeded its execution timeout")
        return run_process([self.binary, *args], cwd=cwd, env=env, cancellation=self.cancellation,
                           timeout=min(remaining, 10) if probe else remaining,
                           input_text=prompt, max_output_bytes=128 * 1024 if probe else _OUTPUT_LIMIT)

    def _check_capabilities(self, cwd, env, deadline):
        if self.capable:
            return
        provider = self.config.agent
        args = ["exec", "--help"] if provider == "codex" else ["run", "--help"] if provider == "opencode" else ["--help"]
        help_response = self._run(args, cwd, env, deadline, probe=True)
        help_text = help_response.stdout + (help_response.stderr if provider == "agy" else "")
        if any(not re.search(re.escape(flag) + r"(?=[\s=,<]|$)", help_text) for flag in _HELP_FLAGS[provider]):
            raise ConfigurationError("Installed agent lacks required isolation/output flags; update that CLI or choose another --agent")
        if provider in {"codex", "opencode"}:
            version = self._run(["--version"], cwd, env, deadline, probe=True).stdout
            match = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)\b", version)
            numbers = tuple(map(int, match.groups())) if match else ()
            minimum = (0, 166, 0) if provider == "codex" else (2, 2, 5)
            if not numbers or numbers < minimum or numbers[0] != minimum[0]:
                label = "Codex 0.166.0+ (0.x)" if provider == "codex" else "OpenCode V2 2.2.5+ (2.x)"
                raise ConfigurationError("Agent translation requires " + label + "; unsupported versions are not invoked")
        if provider == "agy":
            version = self._run(["--version"], cwd, env, deadline, probe=True).stdout.strip()
            if version != AGY_VERSION:
                raise ConfigurationError("Agent translation requires inspected Antigravity " + AGY_VERSION
                                         + "; unsupported versions are not invoked")
        self.capable = True

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.cancellation.raise_if_cancelled()
        deadline = time.monotonic() + self.config.agent_timeout
        # Do not inherit a caller-selected repository as TMPDIR or cwd.
        with tempfile.TemporaryDirectory(prefix="media-to-subs-agent-", dir="/tmp") as directory:
            cwd = Path(directory).resolve()
            env = agent_environment(self.config.agent, cwd)
            self._check_capabilities(cwd, env, deadline)
            args = self._arguments(cwd, messages)
            prompt = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            if self.config.agent == "agy":
                prompt = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
            if len(prompt.encode("utf-8")) > _MESSAGE_LIMIT:
                raise ConfigurationError("Agent translation prompt exceeds its size limit")
            if self.config.agent == "agy":
                guard_args = args[:4] + ["--print-timeout", "5s", "-p", "/hooks"]
                registration = self._run(guard_args, cwd, env, deadline, probe=True)
                verify_agy_guard(registration.stdout, args[3])
            self.progress("Translating through installed " + self.config.agent + " CLI (its login and quota apply)")
            response = self._run(args, cwd, env, deadline, prompt=prompt)
            if self.config.agent == "codex":
                return codex_content(response.stdout, _last_message(cwd / "response.json"))
            if self.config.agent == "claude":
                return claude_content(response.stdout)
            if self.config.agent == "agy":
                return agy_content(response.stdout, cwd=str(cwd), agent=args[3], model=self.config.agent_model)
            return opencode_content(response.stdout)

    def _arguments(self, cwd, messages):
        provider = self.config.agent
        if provider == "codex":
            schema, output = cwd / "schema.json", cwd / "response.json"
            _private_file(schema, json.dumps(_SCHEMA))
            _private_file(output, "")
            args = ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--json", "--color", "never",
                    "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0",
                    "-c", 'web_search="disabled"', "-c", "suppress_unstable_features_warning=true",
                    "--output-schema", str(schema), "--output-last-message", str(output)]
            for feature in _CODEX_FEATURES:
                args.extend(("--disable", feature))
            args.extend(("--enable", "skip_host_skill_discovery"))
        elif provider == "claude":
            mcp = cwd / "mcp.json"
            _private_file(mcp, '{"mcpServers":{}}')
            # --json-schema adds a StructuredOutput tool and internal retries.
            # Keep this a text-only single turn; parse_translations owns validation.
            args = ["--print", "--output-format", "stream-json", "--verbose",
                    "--tools", "", "--disallowedTools", "*", "--safe-mode", "--setting-sources", "",
                    "--strict-mcp-config", "--mcp-config", str(mcp), "--no-session-persistence", "--max-turns", "1"]
        elif provider == "agy":
            name = "media-to-subs-" + uuid4().hex
            args = prepare_agy(cwd, name, messages[0]["content"], _private_file)
            args.extend(("--input-format", "stream-json", "--output-format", "stream-json",
                         "--disable-slash-commands", "--print-timeout", f"{self.config.agent_timeout}s"))
        else:
            name = "media-to-subs-" + uuid4().hex
            config = {"$schema": "https://opencode.ai/config.json", "share": "disabled", "snapshots": False,
                      "agents": {name: {"mode": "primary", "system": messages[0]["content"], "steps": 1,
                                         "permissions": [{"action": "*", "resource": "*", "effect": "deny"}]}},
                      "policies": [{"action": action, "resource": "*", "effect": "deny"}
                                   for action in ("tool.use", "mcp.use", "skill.use", "plugin.use")],
                      "plugins": ["-opencode.config.instruction", "-opencode.config.compatibility"]}
            _private_file(cwd / "opencode.jsonc", json.dumps(config, ensure_ascii=False))
            args = ["run", "--standalone", "--format", "json", "--agent", name]
        if self.config.agent_model:
            args.extend(("--model", self.config.agent_model))
        if provider == "codex":
            args.append("-")
        return args
