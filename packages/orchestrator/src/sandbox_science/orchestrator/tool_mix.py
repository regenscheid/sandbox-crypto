# SPDX-License-Identifier: MIT
"""How a run computed: its kernel cells, its shell calls and the shell calls that ran an
interpreter on code, its jobs and kernel restarts (docs/specs/tools.md, Kernels 13).

Counted by code from the tool calls, never from the model's text: live, as a run's calls
return (``ToolMix``, kept in the run record's ``summaries.tool_mix``), and offline, over
the tool records a control store keeps (``scan``, for ``sandbox-science-harness
tool-mix``), with the same classifier, so a run recorded before the summary existed is
measured as one recorded after. A run that was cut short and kept no record is counted
from its events (``scan_events``): a ``tool`` event is a call that ran, paired by call ID
with the ``assistant`` event that made it.

The classifier reads only a shell call's script text. It finds a call that ran Python
or Sage on code: an interpreter (``python``, ``python3``, ``sage``, ``ipython``,
``pypy3``) given ``-c``, a ``.py`` or ``.sage`` file, ``-m`` with a module other than a
packaging or test tool, or code on its standard input (a heredoc, a pipe, a here-string
or a redirected file). Asking for a version or help, installing, testing and building
do not count, and neither does a script such as ``./run.sh`` that calls Python inside
it: the classifier does not read other files.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Final, Literal, cast

from pydantic import JsonValue

from .control import ControlStore

Interpreter = Literal["python", "sage"]

CLASSIFIER: Final = "shell-computation-1"
"""The classifier's version, kept beside the counts it made."""
_INTERPRETER: Final = re.compile(r"(?:python(?:3(?:\.\d+)?)?|ipython3?|pypy3|sage)")
_NOT_COMPUTATION: Final = frozenset({"pip", "pip3", "venv", "pytest", "build", "ensurepip"})
"""Modules ``-m`` runs that install, test or build rather than compute."""
_ASKING: Final = frozenset({"--version", "-V", "-VV", "-h", "--help", "-?"})
_SAGE_NOT_COMPUTATION: Final = frozenset(
    {"-t", "--test", "-tp", "-pip", "--pip", "-sh", "--sh", "-i", "-f", "-b", "--version", "-v"}
)
_TAKES_VALUE: Final = frozenset({"-W", "-X", "-Q"})
_WRAPPERS: Final = frozenset(
    {"env", "time", "nice", "nohup", "exec", "command", "stdbuf", "xvfb-run", "builtin"}
)
_KEYWORDS: Final = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"})
_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash"})
_HEREDOC: Final = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_FALLBACK: Final = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:python(?:3(?:\.\d+)?)?|ipython3?|pypy3|sage)"
    r"(?:\s+-[A-Za-z]+)*\s+(?:-c\b|[^\s;&|]+\.(?:py|sage)\b)"
)
_DEPTH: Final = 3


def shell_computation(script: str) -> Interpreter | None:
    """The interpreter a shell script ran on code, or ``None`` when it ran none: the
    classifier above, on the script's text alone."""
    return _classify(script, 0)


def _classify(script: str, depth: int) -> Interpreter | None:
    try:
        tokens = _tokens(_without_heredocs(script))
    except ValueError:
        # Unbalanced quotes: what the shell would refuse too. Read it line by line.
        match = _FALLBACK.search(script)
        if match is None:
            return None
        return "sage" if "sage" in match.group(0).split()[0] else "python"
    for words, piped, stdin in _commands(tokens):
        found = _command(words, piped or stdin, depth)
        if found is not None:
            return found
    return None


def _without_heredocs(script: str) -> str:
    """The script with each heredoc's body left out, its ``<<`` kept: the body is text
    for a command's standard input, not commands, and may hold unbalanced quotes."""
    lines = script.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in _HEREDOC.finditer(line):
            strip, delimiter = match.group(1) == "-", match.group(3)
            while index < len(lines):
                body = lines[index]
                index += 1
                if (body.lstrip("\t") if strip else body) == delimiter:
                    break
    return "\n".join(kept)


def _tokens(script: str) -> list[str]:
    lexer = shlex.shlex(script, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    found: list[str] = []
    comment = False
    for word in lexer:
        if word == "\n":
            comment = False
        elif comment:
            continue
        elif word.startswith("#"):
            comment = True
            continue
        found.append(word)
    return found


def _punctuation(token: str) -> bool:
    return bool(token) and all(char in "();<>|&\n" for char in token)


def _redirect(token: str) -> bool:
    return _punctuation(token) and ("<" in token or ">" in token)


def _commands(tokens: Sequence[str]) -> Iterator[tuple[list[str], bool, bool]]:
    """Each simple command's words, with whether a pipe feeds it and whether its
    standard input is redirected (``<``, ``<<``, ``<<<``)."""
    words: list[str] = []
    piped = stdin = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _redirect(token):
            stdin = stdin or "<" in token
            if words and words[-1].isdigit():
                words.pop()  # the descriptor a redirect names, as in 2>&1
            index += 2  # the redirect and its target
            continue
        if _punctuation(token):
            if words:
                yield words, piped, stdin
            words, stdin = [], False
            piped = token in ("|", "|&")
            index += 1
            continue
        words.append(token)
        index += 1
    if words:
        yield words, piped, stdin


def _command(words: list[str], fed: bool, depth: int) -> Interpreter | None:
    index = 0
    while index < len(words):
        word = words[index].lstrip("`")
        name = posixpath.basename(word)
        if "=" in word and not word.startswith("-") and name == word:
            index += 1  # an assignment before the command
        elif name in _KEYWORDS or name in _WRAPPERS:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1  # the wrapper's options
        elif name == "timeout":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            index += 1  # its duration
        elif name in ("uv", "poetry", "pixi", "pdm", "hatch") and words[index + 1 : index + 2] == [
            "run"
        ]:
            index += 2
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if "=" not in option and option in (
                    "--with",
                    "--python",
                    "-p",
                    "--project",
                    "--directory",
                    "--env-file",
                    "--extra",
                    "--group",
                    "--package",
                ):
                    index += 1
        elif name in ("conda", "mamba", "micromamba") and words[index + 1 : index + 2] == ["run"]:
            index += 2
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if option in ("-n", "--name", "-p", "--prefix"):
                    index += 1
        elif name in _SHELLS:
            rest = words[index + 1 :]
            if depth < _DEPTH and "-c" in rest and rest.index("-c") + 1 < len(rest):
                return _classify(rest[rest.index("-c") + 1], depth + 1)
            return None
        else:
            break
    if index >= len(words):
        return None
    name = posixpath.basename(words[index].lstrip("`"))
    if _INTERPRETER.fullmatch(name) is None:
        return None
    return _runs_code(words[index + 1 :], "sage" if name == "sage" else "python", fed)


def _runs_code(arguments: Sequence[str], language: Interpreter, fed: bool) -> Interpreter | None:
    """The interpreter, when its arguments have it run code; ``fed``: its standard input
    is a pipe or a redirect, from which it reads code when given no script."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument in _ASKING or (language == "sage" and argument in _SAGE_NOT_COMPUTATION):
            return None
        if language == "sage" and argument in ("-python", "--python", "-ipython", "--ipython"):
            language = "python"
            continue
        if argument == "-":
            return language  # the code is on standard input
        if argument.startswith("--"):
            continue
        if argument.startswith("-") and len(argument) > 1:
            letters = argument[1:]
            for position, letter in enumerate(letters):
                if letter == "c":
                    return language
                if letter == "m":
                    module = letters[position + 1 :] or (
                        arguments[index] if index < len(arguments) else ""
                    )
                    computes = bool(module) and module.split(".")[0] not in _NOT_COMPUTATION
                    return language if computes else None
                if f"-{letter}" in _TAKES_VALUE:
                    if position == len(letters) - 1:
                        index += 1
                    break
            continue
        # The first argument that is not an option is the script the interpreter runs.
        base = posixpath.basename(argument)
        if base == "setup.py":
            return None  # a build or an install
        return language if base.endswith((".py", ".sage")) else None
    return language if fed else None


@dataclass
class ToolMix:
    """One run's counts, kept as the run's calls return. A call counts once it ran: a
    call refused before it reached its tool (an error with no execution) does not."""

    python: int = 0
    sage: int = 0
    shell: int = 0
    shell_computation: int = 0
    """Shell calls that ran an interpreter on code (``shell_computation``)."""
    start_job: int = 0
    restart_kernel: int = 0
    state_lost: int = 0
    """Cells that reported their kernel's earlier state lost."""
    nudged: bool = False
    """Whether a shell call's result carried the platform's note about kernels (one a
    run, Kernels 13)."""

    def count(self, tool: str, arguments: object, record: Mapping[str, Any]) -> None:
        """Count one call from its tool record: its name, its arguments as the harness
        ran them, and its outcome's record."""
        ran = "execution" in record or "error" not in record
        if not ran:
            return
        if tool in ("python", "sage"):
            setattr(self, tool, getattr(self, tool) + 1)
            if _state_lost(record):
                self.state_lost += 1
        elif tool == "shell":
            self.shell += 1
            script = _script(arguments)
            if script is not None and shell_computation(script) is not None:
                self.shell_computation += 1
        elif tool in ("start_job", "restart_kernel"):
            setattr(self, tool, getattr(self, tool) + 1)

    def add(self, other: ToolMix) -> None:
        for name in _COUNTS:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def summary(self) -> dict[str, JsonValue]:
        """The run record's ``summaries.tool_mix``."""
        return {"classifier": CLASSIFIER, **{name: getattr(self, name) for name in _COUNTS}}

    def line(self) -> str:
        """The counts in one line, as the workspace's run view words them."""
        cells = self.python + self.sage
        languages = ", ".join(
            f"{name} {count}" for name, count in (("python", self.python), ("sage", self.sage))
        )
        parts = [
            f"{cells} {_plural(cells, 'cell')}" + (f" ({languages})" if cells else ""),
            f"{self.shell} {_plural(self.shell, 'shell call')}"
            + (f", {self.shell_computation} ran Python" if self.shell_computation else ""),
        ]
        if self.start_job:
            parts.append(f"{self.start_job} {_plural(self.start_job, 'job')}")
        if self.restart_kernel:
            parts.append(f"{self.restart_kernel} {_plural(self.restart_kernel, 'kernel restart')}")
        if self.state_lost:
            parts.append(f"kernel state lost {_times(self.state_lost)}")
        return " · ".join(parts)


_COUNTS: Final = tuple(field.name for field in fields(ToolMix) if field.name != "nudged")


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def _times(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def _script(arguments: object) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return None
    if not isinstance(arguments, dict):
        return None
    script = cast(dict[str, object], arguments).get("script")
    return script if isinstance(script, str) else None


def _state_lost(record: Mapping[str, Any]) -> bool:
    execution = record.get("execution")
    if not isinstance(execution, dict):
        return False
    result = cast(dict[str, object], execution).get("result")
    return isinstance(result, dict) and cast(dict[str, object], result).get("state_lost") is True


# Offline: the same counts over the tool records a control store keeps.

_FIELDS: Final = (
    "json_extract({0},'$.tool'),json_extract({0},'$.arguments'),"
    "json_type({0},'$.error'),json_type({0},'$.execution'),"
    "json_extract({0},'$.execution.result.state_lost'),"
    "json_extract({0},'$._record_storage.message')"
)


def scan(control: ControlStore, project: str, session: str, run: str) -> ToolMix:
    """One recorded run's counts, from its tool records: inline in its record, or in its
    stored history, with a preview's full record read from the tool journal. Only the
    fields the classifier reads are loaded, never a call's output."""
    mix = ToolMix()
    with control.read() as tx:
        db = tx.sql
        # A run with stored history keeps every record in its collection, and the first
        # of them inline too; a run without one keeps them inline alone.
        rows = db.execute(
            f"SELECT {_FIELDS.format('item')} FROM run_collections "  # noqa: S608 - fixed SQL
            "WHERE project=? AND session=? AND run=? AND collection='tools' ORDER BY position",
            (project, session, run),
        ).fetchall()
        if not rows:
            rows = db.execute(
                f"SELECT {_FIELDS.format('u.value')} FROM runs r,"  # noqa: S608 - fixed SQL
                "json_each(r.record,'$.tools') u WHERE r.project_id=? AND r.session_id=? "
                "AND r.name=? ORDER BY u.key",
                (project, session, run),
            ).fetchall()
        for tool, arguments, error, execution, lost, message in rows:
            if message is not None:
                full = db.execute(
                    f"SELECT {_FIELDS.format('record')} FROM archive_tool_records "  # noqa: S608
                    "WHERE project=? AND message_id=?",
                    (project, message),
                ).fetchone()
                if full is not None:
                    tool, arguments, error, execution, lost, _ = full
            record: dict[str, Any] = {}
            if error is not None:
                record["error"] = True
            if execution is not None:
                record["execution"] = {"result": {"state_lost": lost == 1}}
            mix.count(str(tool), arguments, record)
    return mix


def unkept_runs(control: ControlStore, project: str, session: str) -> list[str]:
    """The runs that wrote events but kept no run record (one cut short, such as by the
    loss of its agent VM), by name."""
    with control.read() as tx:
        rows = tx.sql.execute(
            "SELECT DISTINCT e.run FROM run_events e WHERE e.project_id=? AND e.session_id=? "
            "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.project_id=e.project_id "
            "AND r.session_id=e.session_id AND r.name=e.run) ORDER BY e.run",
            (project, session),
        ).fetchall()
    return [run for (run,) in rows]


_LOST: Final = re.compile(r'"state_lost":\s*(?:true|"the kernel restarted)')
"""A cell result's report of lost state, as the model reads it (a note, not a flag)."""


def scan_events(control: ControlStore, project: str, session: str, run: str) -> ToolMix:
    """One run's counts from its events, for a run with no kept record: each ``tool``
    event is a call that ran (one whose outcome is an error with no execution state was
    refused before it ran and is left out), named by the event or else by the
    ``assistant`` event's call with the same ID, whose arguments say what a shell call
    ran. A call that never returned has no ``tool`` event and is not counted."""
    calls: dict[str, tuple[object, object]] = {}
    mix = ToolMix()
    with control.read() as tx:
        rows = tx.sql.execute(
            "SELECT kind,data FROM run_events WHERE project_id=? AND session_id=? AND run=? "
            "AND kind IN ('assistant','tool') ORDER BY seq",
            (project, session, run),
        ).fetchall()
    for kind, data in rows:
        event = cast(dict[str, Any], json.loads(data))
        if kind == "assistant":
            for item in cast(list[object], event.get("tool_calls") or []):
                if isinstance(item, dict):
                    made = cast(dict[str, Any], item)
                    if isinstance(made.get("id"), str):
                        calls[made["id"]] = (made.get("name"), made.get("arguments"))
            continue
        name, arguments = calls.get(str(event.get("call_id")), (None, None))
        tool = event.get("name") or name
        found = event.get("outcome")
        outcome = cast(dict[str, Any], found) if isinstance(found, dict) else {}
        state = outcome.get("state")
        record: dict[str, Any] = {}
        if state is not None:
            record["execution"] = {
                "result": {"state_lost": _LOST.search(str(event.get("result") or "")) is not None}
            }
        elif outcome.get("error") is not None:
            record["error"] = True
        if isinstance(tool, str):
            mix.count(tool, arguments, record)
    return mix
