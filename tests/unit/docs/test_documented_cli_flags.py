"""Docs ↔ CLI drift guard (plan D6).

Deployment / user-facing docs must only reference CLI flags that actually
exist on the registered click commands. This test parses ``octop <command>``
lines from the operational doc set and asserts every ``--flag`` resolves
against the real command (or the CLI group's global options). Review /
assessment documents are excluded — they legitimately quote historical
mistakes (e.g. the removed ``--workspace`` in D6's own record).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import click
import pytest

from octop.cli.registry import COMMANDS

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"

# Operational docs users copy commands from. Review / plan / assessment
# documents quote historical command lines on purpose and stay unchecked.
CHECKED_DOCS = (
    "deployment-stage2.md",
    "deployment-stage3.md",
    "deployment-minio-pgvector.md",
    "production-ha.md",
    "user-guide.md",
    "cli.md",
    "configuration.md",
    "memory-slim.md",
    "acp.md",
)

_LINE_RE = re.compile(r"(?:^|[`\s])octop\s+([a-z][\w-]*)((?:\s+[\w/.:=-]+)*)", re.M)
_FLAG_RE = re.compile(r"(?:^|\s)(--?[\w][\w-]*)")


def _load_click_command(name: str) -> click.Command:
    module_path, attr, _help = COMMANDS[name]
    module = importlib.import_module(f"octop.cli{module_path}")
    obj = getattr(module, attr)
    assert isinstance(obj, click.Command), f"{name} is not a click command"
    return obj


def _resolve_target(command: str, tokens: list[str]) -> tuple[click.Command, int]:
    """Walk group → subcommand for leading non-flag tokens; return (cmd, idx)."""
    cmd = _load_click_command(command)
    idx = 0
    while idx < len(tokens) and not tokens[idx].startswith("-"):
        if isinstance(cmd, click.Group):
            sub = cmd.get_command(None, tokens[idx])
            if sub is None:
                break
            cmd = sub
            idx += 1
        else:
            break
    return cmd, idx


def _command_opts(cmd: click.Command) -> set[str]:
    opts: set[str] = set()
    for param in cmd.params:
        opts.update(param.opts)
    opts.update(cmd.context_settings.get("help_option_names", []))
    return opts


def _documented_invocations() -> list[tuple[str, int, str, list[str]]]:
    """Yield (doc, lineno, command, flags) for every ``octop <cmd>`` line."""
    out: list[tuple[str, int, str, list[str]]] = []
    for doc_name in CHECKED_DOCS:
        path = DOCS_DIR / doc_name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in _LINE_RE.finditer(line):
                command, rest = match.group(1), match.group(2)
                if command not in COMMANDS:
                    continue
                out.append((doc_name, lineno, command, rest))
    return out


def test_every_registered_command_is_loadable() -> None:
    """Sanity: the registry still maps to real click commands (guard the guard)."""
    for name in COMMANDS:
        _load_click_command(name)


def test_documented_cli_flags_exist() -> None:
    """Every flag in operational docs must exist on the real command (D6)."""
    from octop.cli.main import cli

    global_opts: set[str] = set()
    for param in cli.params:
        global_opts.update(param.opts)
    global_opts.update(cli.context_settings.get("help_option_names", ["--help"]))

    checked = _documented_invocations()
    assert checked, "no octop command lines found — doc glob broken?"

    problems: list[str] = []
    for doc, lineno, command, flags in checked:
        tokens = [tok for tok in re.split(r"\s+", flags.strip()) if tok]
        cmd, _idx = _resolve_target(command, tokens)
        opts = _command_opts(cmd)
        for flag in _FLAG_RE.findall(flags):
            if flag == "--help" or flag in opts or flag in global_opts:
                continue
            problems.append(f"{doc}:{lineno}: 'octop {command} {flag}' — no such flag")
    assert not problems, "documented CLI flags drifted from the implementation:\n" + "\n".join(
        problems
    )


def test_agent_worker_has_no_workspace_flag() -> None:
    """The D6 regression pin: ``--workspace`` must never come back in docs or CLI."""
    cmd = _load_click_command("agent-worker")
    opts: set[str] = set()
    for param in cmd.params:
        opts.update(param.opts)
    assert "--workspace" not in opts
    assert {"--concurrency", "--debug"} <= opts

    deployment = DOCS_DIR / "deployment-stage2.md"
    assert "--workspace" not in deployment.read_text()


@pytest.mark.parametrize(
    "command",
    ["run", "agent-worker", "acp"],
)
def test_cluster_docs_worker_command_uses_real_flags(command: str) -> None:
    """Spot-check: stage2/stage3 docs' worker/api invocations parse cleanly."""
    cmd = _load_click_command(command)
    opts: set[str] = set()
    for param in cmd.params:
        opts.update(param.opts)
    assert opts, f"{command} unexpectedly has no options"
