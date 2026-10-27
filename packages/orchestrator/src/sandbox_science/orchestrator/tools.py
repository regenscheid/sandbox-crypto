# SPDX-License-Identifier: MIT
"""The agent's execution tools (docs/specs/tools.md), all running on the worker.

Each tool call becomes a worker operation whose ID is derived from the call,
so a repeated call is idempotent. Results go back to the model as compact
JSON, bounded in size as a whole; the full execution records stay in the
transcript. Definite failures become errors the model can correct. An unknown
external effect stops the attempt with its durable tool intent still pending.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import json
import logging
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    MAX_ADMISSION_HOSTS,
    CellRequest,
    EditFileRequest,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    GlobRequest,
    GrepRequest,
    HostPort,
    ListFilesRequest,
    PdfRequest,
    PublishRequest,
    ReadFileRequest,
    ShellRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import (
    DEFAULT_GREP_RESULTS,
    MAX_GLOB_RESULTS,
    MAX_GREP_CONTEXT,
    MAX_GREP_RESULTS,
    Language,
    SearchStop,
)

from . import capabilities as caps
from .agent_messages import AgentMailbox
from .artifact_publisher import ArtifactPublisher
from .connectors import (
    MAX_LOAD,
    MAX_LOADED_CHARS,
    MAX_QUERY,
    Connectors,
)
from .consistency.controller import ReviewController
from .context.history import HistoryTool
from .continuity.kernels import MAX_SAVED_FILES, LostGeneration, LostKernels
from .documents.errors import DocumentError
from .documents.tool import DocumentTool, with_notices
from .effects import UnknownEffectError
from .hooks import Effect, TurnHook
from .jobs import BACKGROUND, STOP_AT_FAILURE, JobTools
from .kernel_check import KernelCheck
from .ledger.tool import LedgerTool
from .mathchecks.controller import MathController
from .memory_tools import MemoryTool
from .model import ToolCall, ToolSpec
from .plan_tools import Plans, PlanTools
from .provision import PROVISION_SPECS, PROVISIONING, ProvisionTools
from .purpose import COLLISION, PurposeError, collides, offered, split, without
from .question_tool import QuestionTool
from .recording import FileRecorder, Written, with_recorded
from .resource_hooks import Resources
from .skills.tool import SkillTool
from .tool_entry import Handler as Handler
from .tool_entry import Request as Request
from .tool_entry import ToolEntry as ToolEntry
from .tool_entry import ToolOutcome as ToolOutcome
from .tool_entry import group as _group
from .tool_entry import plain as _plain
from .tool_mix import Interpreter, ToolMix, shell_computation
from .view_image import ImageViewer
from .worker_client import WorkerAPIError, WorkerClient, dump

if TYPE_CHECKING:
    from .delegation.hook import Delegations
    from .delegation.tracks import TrackTools

_log = logging.getLogger(__name__)


SAVED_OUTPUT: Final = "call-output"
"""The session workspace's directory for output too long for its result, saved whole and
recorded like any file a call writes (tools spec Shell 5)."""
LONG_OUTPUT: Final = (
    " Output too long for the result comes back as its head and tail, marked as a sample, "
    "and is saved whole to a file the result names."
)
"""Said by each tool whose output is saved when it is too long (lane K's triage, P4)."""
TRACEBACK_LINES: Final = 20
SHOWN_OUTPUTS: Final = 20
"""Rich outputs shown to the model, the first and last half; a note counts the rest."""
SHOWN_ENTRIES: Final = 200
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]")

_TIMEOUT = {"type": "number", "exclusiveMinimum": 0, "description": "Wall-time limit in seconds"}

SHARED_IN_CODE: Final = (
    " Code runs in the session's workspace, so a relative shared/ there is a folder of the "
    "session's own. The project's shared area, shared/… in the file and PDF tools, is "
    "$PROJECT_SHARED (os.environ['PROJECT_SHARED'])."
)
"""Said by each tool that runs code: the file tools' shared/ is not the code's (lab 38)."""

ADMIT_HOSTS: Final = ToolSpec(
    "admit_hosts",
    "Make sites reachable before your code contacts them. Give host names, or host:port "
    "(the port is 443 unless given). The project's policy decides each: a host it allows "
    "becomes reachable at once, and a refused one comes back with the reason. Use it before "
    "downloading, cloning or calling an API on a site this session has not reached yet; "
    "otherwise the first connection to such a site fails, and it becomes reachable some "
    "seconds later. Being reachable does not mean every request is allowed: the policy still "
    "decides each one. A refused host is the policy's decision: do not ask for it again, or "
    "fetch its content from another host.",
    {
        "type": "object",
        "properties": {
            "hosts": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_ADMISSION_HOSTS,
            }
        },
        "required": ["hosts"],
        "additionalProperties": False,
    },
)

SAGE: Final = ToolSpec(
    "sage",
    "Run SageMath code in the session's persistent Sage kernel inside the project sandbox: "
    "variables, imports and loaded data persist between calls until that kernel restarts. "
    "The default for algebra, number theory and other computation in Sage, and for iterative "
    "exploration. Sage syntax applies: ^ is exponentiation, 1/3 is an exact rational, and "
    "integer literals are Sage Integers (convert them with int() before json.dumps). Results "
    "come back as plain text; run %display latex first, or call latex(x), to get LaTeX."
    + LONG_OUTPUT
    + SHARED_IN_CODE,
    {
        "type": "object",
        "properties": {"code": {"type": "string"}, "timeout_seconds": _TIMEOUT},
        "required": ["code"],
        "additionalProperties": False,
    },
)

_PATH = {"type": "string", "description": "A PDF in the workspace, or shared/… for the project's"}

PDF_SPECS: Final = (
    ToolSpec(
        "pdf_info",
        "Describe a PDF: its pages, numbered physically from 1, with the labels the document "
        "prints (such as iv or 12); which pages have extractable text; its outline; and its "
        "SHA-256. The PDF is parsed inside the project sandbox.",
        {
            "type": "object",
            "properties": {"path": _PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "pdf_pages",
        'Read pages of a PDF as numbered lines, at most 20 pages a call, such as "3-5,9". '
        "Cite a line as p<page>:<line>, with the physical page number. Lines are numbered as "
        "pdf_search numbers them: running headers, footers and page numbers count, though "
        "they are left out unless include_artifacts. Extracted text of equations, matrices "
        "and figures can be wrong: render the page to check it.",
        {
            "type": "object",
            "properties": {
                "path": _PATH,
                "pages": {"type": "string", "description": "Physical page numbers, from 1"},
                "include_artifacts": {
                    "type": "boolean",
                    "description": "Keep running headers, footers and page numbers",
                },
                "boxes": {
                    "type": "boolean",
                    "description": "Show each line's box [x0, y0, x1, y1], to render a region",
                },
            },
            "required": ["path", "pages"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "pdf_search",
        "Find a case-insensitive string in a PDF. Each hit gives its page, the page's printed "
        "label, and the line's number and text. No hit means only that this string is not in "
        "the extractable text: pages without text (pdf_info) are not searched, so render them, "
        "and another spelling or notation may be there.",
        {
            "type": "object",
            "properties": {"path": _PATH, "query": {"type": "string"}},
            "required": ["path", "query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "pdf_render",
        "Render a PDF page, or a region of it, to a PNG in the workspace, to check equations, "
        "matrices and figures against the extracted text. A region is [x0, y0, x1, y1] in PDF "
        "points from the page's bottom left: read the page with pdf_pages and boxes true to see "
        "each line's box, and cover the lines you want, with a margin for what is drawn "
        "between them; pdf_info gives the page's size. Publish the PNG to keep it as evidence.",
        {
            "type": "object",
            "properties": {
                "path": _PATH,
                "page": {"type": "integer", "minimum": 1},
                "region": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                },
                "dpi": {"type": "integer", "minimum": 18, "maximum": 300},
            },
            "required": ["path", "page"],
            "additionalProperties": False,
        },
    ),
)
PDF_READS: Final = frozenset({"pdf_info", "pdf_pages", "pdf_search"})
"""The PDF tools that only read (D99); ``pdf_render`` writes a PNG in the session."""

FIND_ARTIFACTS: Final = ToolSpec(
    "find_artifacts",
    "Find the project's artifacts: files published, or recorded from calls, in this run or "
    "any earlier one. Each comes at its latest revision with its artifact ID, revision and "
    "SHA-256, which pin it as ledger evidence, and its path, size, media type and whether "
    "it is a result (published or declared). The most recently changed come first, ten at "
    "a time; give before (the next_before you got) for the next page.",
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "A file's path as you published it, in the workspace or "
                "shared/…, or as this tool lists it, sessions/<session>/…",
            },
            "contains": {
                "type": "string",
                "description": "Text in the artifact's path, such as a file name; another "
                "session's files are under sessions/<session>/",
            },
            "media_type": {
                "type": "string",
                "description": "A media type, or its start, such as application/pdf or image/",
            },
            "results_only": {"type": "boolean", "description": "Only published or declared"},
            "before": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
)

SPECS: Final = (
    ToolSpec(
        "python",
        "Run Python code in the session's persistent kernel inside the project sandbox: "
        "variables, imports and loaded data persist between calls until the kernel restarts. "
        "The default for computation, analysis and iterative exploration. Returns printed "
        "output, the value of the last expression and any exception."
        + LONG_OUTPUT
        + SHARED_IN_CODE,
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout_seconds": _TIMEOUT,
                "replayable": {
                    "type": "boolean",
                    "description": "Permit automatic restoration after kernel loss only for "
                    "assignments of fixed integer, string, boolean or None literals. "
                    "All other code needs explicit recovery.",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "shell",
        "Run a bash script in the session's workspace inside the project sandbox, for "
        "commands: builds, installs, file operations, and running existing scripts and "
        "command-line tools. Not for computation that belongs in a kernel cell (python or "
        "sage, where offered). Each call starts afresh in the workspace (no state carries "
        "over except files); stdin is empty. Returns the exit code and the output. If a "
        "command might fail, keep its diagnostics visible or inspect its redirected log in "
        "the same call. To search or find files, use grep and glob, whose output is "
        "bounded, rather than grep or find here; to change part of a file, edit_file."
        + LONG_OUTPUT
        + STOP_AT_FAILURE
        + SHARED_IN_CODE,
        {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "cwd": {"type": "string", "description": "Directory relative to the workspace"},
                "timeout_seconds": _TIMEOUT,
            },
            "required": ["script"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "read_file",
        "Read a file from the workspace (or shared/… for the project's shared area).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "write_file",
        "Write a text file in the workspace. For shared/ paths, set create_only=true "
        "to create a new file, or expected_sha256 to the current hash from read_file "
        "when replacing it. Other paths can omit a precondition. To change part of an "
        "existing file, use edit_file instead of writing it whole.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "create_only": {"type": "boolean"},
                "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "edit_file",
        "Change part of a text file in the workspace (or shared/…) by replacing an exact "
        "string, instead of rewriting the file with write_file. old_string must occur "
        "exactly once, so include enough of the surrounding text, unless replace_all "
        "replaces every occurrence; whitespace and line breaks count. For shared/ paths, "
        "set expected_sha256 to the current hash from read_file. Returns the file's new "
        "sha256 and the number of replacements.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "minLength": 1},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
                "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "list_files",
        "List a directory in the workspace (or shared/…).",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "glob",
        "Find files and directories by path pattern below a directory of the workspace "
        "(or shared/…): * and ? match within a name, [ab] one of a set, and ** any number "
        f"of directories, as in **/*.py. Newest first, at most {MAX_GLOB_RESULTS}; "
        "symlinks are listed, not followed.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1},
                "path": {"type": "string", "description": "Directory to search; default ."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_GLOB_RESULTS},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "grep",
        "Search the text files below a directory of the workspace (or shared/…), or one "
        "file, for a Python regular expression, with bounded output: use it rather than "
        "grep in the shell. mode files (the default) lists the matching files, count the "
        "matching lines in each, and content the lines as path:line: text, with "
        "context_lines around each. glob narrows the files, by name (*.py) or by path "
        "(src/**/*.py). Binary files and symlinks are skipped.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1},
                "path": {"type": "string", "description": "Directory or file; default ."},
                "glob": {"type": "string"},
                "mode": {"type": "string", "enum": ["files", "count", "content"]},
                "case_insensitive": {"type": "boolean"},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": MAX_GREP_CONTEXT},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_GREP_RESULTS,
                    "description": f"Files, or lines in content mode; default "
                    f"{DEFAULT_GREP_RESULTS}",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "publish",
        "Publish a workspace file as an immutable project artifact, recording its hash.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "media_type": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
)


def operation_id(prefix: str, call: ToolCall) -> str:
    """A worker operation ID for a tool call: stable, path-safe and at most 128 characters.

    It ends with a digest of the whole call, its name and arguments included, but
    not its purpose (Purpose 3), so a call's identity does not depend on it. A
    different call never meets an earlier call's operation, however long the
    prefix; the same call at the same place in a run with the same ID does, and
    gets the earlier result instead of running again.
    """
    whole = json.dumps([prefix, call.id, call.name, without(call.arguments)])
    digest = hashlib.sha256(whole.encode()).hexdigest()[:16]
    readable = _UNSAFE.sub("-", f"{prefix}-{call.id}").lstrip("-._")[:111].rstrip("-._")
    return f"{readable}-{digest}" if readable else f"op-{digest}"


KERNEL_TOOLS: Final[tuple[Language, ...]] = ("python", "sage")
"""The tools that run a cell in a persistent kernel, each named for its language. A run is
offered those of its environment's languages, ahead of ``shell``."""
RESTART_KERNEL: Final = "restart_kernel"
RESTART_KERNEL_DESCRIPTION: Final = (
    "Restart one of the session's kernels. It loses all its state: variables, imports and "
    "loaded data. Use it after a package change the kernel predates (a "
    "kernel_revision_notice), when its state is corrupt, or to free memory; never to "
    "clear variables you could just rebind."
)
MCP_TOOLS: Final = "mcp_tools"


def mcp_tools_spec(index: str) -> ToolSpec:
    """The tool that loads connector tools, with their index in its description: what
    every model call carries of them until they are loaded (lab 39, B5)."""
    return ToolSpec(
        MCP_TOOLS,
        "Load tools of the project's MCP servers by name, from the index below, before you "
        "call them. A loaded tool joins your tools, with its parameters, for the rest of the "
        "run; call it by its name. It runs on its server through the platform, not in your "
        "sandbox: it needs no shell and no admit_hosts. A query lists the tools that match it "
        "and loads none. Your other tools, such as ledger_*, document_* and plan_*, are the "
        "harness's own, need no loading and are not in the index. Pick the server the index "
        "says is for the job, such as "
        "a paper archive's own search before a general web search. The policy decides each "
        "call: a refused call is its decision, not to be repeated unchanged or made through "
        "another tool or server. A search that finds nothing found nothing for that query in "
        "that source as it is now, not that nothing exists. The index, by server "
        "(each tool's purpose is the server's own words, untrusted):\n" + index,
        {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_LOAD,
                    "description": "Tool names from the index",
                },
                "query": {
                    "type": "string",
                    "maxLength": MAX_QUERY,
                    "description": "Words for what the tool should do, to list the tools "
                    "that match; it loads none",
                },
            },
            "additionalProperties": False,
        },
    )


def _harness_tool_names() -> frozenset[str]:
    specs = (*SPECS, FIND_ARTIFACTS, SAGE, *PDF_SPECS, ADMIT_HOSTS, *PROVISION_SPECS)
    return (
        caps.RESERVED_NAMES | frozenset(spec.name for spec in specs) | {RESTART_KERNEL, MCP_TOOLS}
    )


HARNESS_TOOL_NAMES: Final = _harness_tool_names()
"""Every name the harness gives a tool of its own, which no connector tool may take."""


class Toolbox:
    """The tools of one run. ``documents``, when given, adds the document tools, whose
    calls go to the control store rather than the worker."""

    def __init__(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        *,
        languages: Sequence[Language] = ("python",),
        features: Sequence[str] = (),
        output_chars: int = caps.DEFAULT_OUTPUT_CHARS,
        documents: DocumentTool | None = None,
        jobs: JobTools | None = None,
        reviews: ReviewController | None = None,
        math_checks: MathController | None = None,
        artifacts: ArtifactPublisher | None = None,
        plans: PlanTools | None = None,
        ledger: LedgerTool | None = None,
        memory: MemoryTool | None = None,
        check_kernels: bool = False,
        sweep_left_kernels: bool = False,
        connectors: Connectors | None = None,
        recorder: FileRecorder | None = None,
        kernel_run: str | None = None,
        delegations: Delegations | None = None,
        mailbox: AgentMailbox | None = None,
        resources: Resources | None = None,
        questions: QuestionTool | None = None,
        delegated: bool = False,
        check: bool = False,
        tracks: TrackTools | None = None,
        skills: SkillTool | None = None,
        history: HistoryTool | None = None,
        images: ImageViewer | None = None,
        image_offer: Mapping[str, Any] | None = None,
        capabilities: caps.CapabilitySet | None = None,
    ) -> None:
        """For a ``delegated`` run, a call to a tool it is not offered, such as a review,
        plan, delegate or question tool, is refused with ``not_permitted_for_role`` (PD-16,
        PD-41). Its connector tools are its own run's, as a main run's are (DA-3). A
        ``check`` run is offered only the reads of the documents, the ledger and the
        memory, beside its compute and file tools: it changes no entry, document or memory
        (PD-34, LG-13, as the coordinator tightened them on 2026-10-15)."""
        if delegated and questions is not None:
            raise ValueError("a delegated run cannot ask the user")
        if capabilities is None:
            # Compatibility for direct callers. Production binds scoped domains in
            # the harness, rather than passing a second set of service options.
            bundles: list[caps.Capability] = []

            def add[T](service: T | None, bind: Callable[[T], caps.Capability]) -> None:
                if service is not None:
                    bundles.append(bind(service))

            add(documents, caps.documents)
            add(reviews, caps.reviews)
            add(plans, caps.plans)
            add(ledger, caps.ledger)
            add(memory, caps.memory)
            add(tracks, caps.tracks)
            add(delegations, caps.delegations)
            add(resources, caps.resources)
            add(mailbox, caps.mailbox)
            add(skills, caps.skills)
            add(history, caps.history)
            add(images, caps.images)
            add(questions, caps.questions)
            add(math_checks, caps.math)
            add(recorder, caps.files)
            add(artifacts, caps.artifacts)
            bundles.append(caps.jobs(jobs if BACKGROUND in features else None))
            capabilities = caps.CapabilitySet(tuple(bundles))
            # Legacy direct callers could explicitly provide review/plan controllers
            # even for a delegated run; only its record filtering is adapted here.
            if check:
                capabilities = caps.record_reads(capabilities)
        self.client = client
        self.context = context
        self.output_chars = output_chars
        self.languages = tuple(languages)
        self.reviews = capabilities.service(caps.REVIEWS)
        self.math_checks = capabilities.service(caps.MATH)
        self.documents = capabilities.service(caps.DOCUMENTS)
        self.jobs = capabilities.service(caps.JOBS)
        self.artifacts = capabilities.service(caps.ARTIFACTS)
        self.plans = capabilities.service(caps.PLANS)
        self.recorder = capabilities.service(caps.FILES)
        self.delegations = capabilities.service(caps.DELEGATIONS)
        self.mailbox = capabilities.service(caps.MAILBOX)
        self.resources = capabilities.service(caps.RESOURCES)
        self.questions = capabilities.service(caps.QUESTIONS)
        self.skills = capabilities.service(caps.SKILLS)
        self.delegated = delegated
        self.check_kernels = check_kernels
        self.sweep_left_kernels = sweep_left_kernels
        self.kernel_run = kernel_run
        self.tool_mix: ToolMix | None = ToolMix()
        """The run's tool mix, counted as its calls return (Kernels 13)."""
        self.lost_kernels: LostKernels | None = None
        """The records of the run's kernels' cells, which a cell that reports lost state
        lists (Kernels 7); bound by the harness when the run keeps them."""
        if delegated and self.questions is not None:
            raise ValueError("a delegated run cannot ask the user")

        def kernels() -> TurnHook:
            keep = self.delegations.live if self.delegations is not None else None
            return KernelCheck(
                self.client,
                self.context,
                run=self.kernel_run,
                sweep=self.sweep_left_kernels,
                keep=keep,
            )

        capabilities = replace(
            capabilities,
            bundles=(
                *(b for b in capabilities.bundles if b.name != caps.KERNELS.name),
                caps.KERNELS.bind(kernels),
            ),
        )
        self.capabilities = capabilities.with_hook_slots()
        self.provisioning = ProvisionTools(client, context, output_chars=output_chars)
        worker: dict[str, tuple[Effect, Handler]] = {
            "shell": ("effect", _plain(self._shell)),
            "python": ("effect", _plain(self._python)),
            "read_file": ("read", _plain(self._read)),
            "write_file": ("effect", _plain(self._write)),
            "edit_file": ("effect", _plain(self._edit)),
            "list_files": ("read", _plain(self._list)),
            "glob": ("read", _plain(self._glob)),
            "grep": ("read", _plain(self._grep)),
            "publish": ("effect", self._publish),
            "find_artifacts": ("read", _plain(self._find_artifacts)),
            "sage": ("effect", _plain(self._sage)),
            RESTART_KERNEL: ("effect", _plain(self._restart)),
            **{
                spec.name: (
                    "read" if spec.name in PDF_READS else "effect",
                    _plain(partial(self._pdf, spec.name.removeprefix("pdf_"))),
                )
                for spec in PDF_SPECS
            },
            "admit_hosts": ("effect", _plain(self._admit)),
            "provision": ("effect", _plain(self.provisioning.provision)),
            "provision_status": ("read", _plain(self.provisioning.status)),
        }
        # The kernel tools come first, before shell: the order a model reads its tools in
        # steers which one it reaches for, and computation belongs in a kernel.
        kernel_specs = {spec.name: spec for spec in (*SPECS, SAGE) if spec.name in KERNEL_TOOLS}
        specs = [kernel_specs[name] for name in KERNEL_TOOLS if name in self.languages]
        specs.extend(spec for spec in SPECS if spec.name not in KERNEL_TOOLS)
        if self.artifacts is not None:
            specs.append(FIND_ARTIFACTS)
        if self.languages:
            specs.append(
                ToolSpec(
                    RESTART_KERNEL,
                    RESTART_KERNEL_DESCRIPTION,
                    {
                        "type": "object",
                        "properties": {
                            "language": {"type": "string", "enum": list(self.languages)}
                        },
                        "additionalProperties": False,
                    },
                )
            )
        if "pdf" in features:
            specs.extend(PDF_SPECS)
        if "admission" in features:
            specs.append(ADMIT_HOSTS)
        if PROVISIONING in features:
            specs.extend(PROVISION_SPECS)
        entries = [ToolEntry(spec, *worker[spec.name]) for spec in specs]
        entries.extend(self.capabilities.entries)
        if self.jobs is not None:
            self.jobs.bindings = lambda: self._bindings({})
            self.jobs.binding_inventory = self._binding_inventory
        self.image_offer = None if image_offer is None else dict(image_offer)
        self.connectors = connectors
        """The run's connector tools: indexed in ``mcp_tools``, and offered once loaded."""
        self.deferred: dict[str, ToolEntry] = {}
        self.unloadable: dict[str, str] = {}
        """Connector tools that are not loaded, with why: a schema that defines the
        purpose field itself (Purpose 1)."""
        self._offered: dict[str, tuple[ToolSpec, ToolSpec]] = {}
        if connectors is not None:
            self._defer(connectors, {entry.spec.name for entry in entries})
            # A server whose listing failed is unavailable, not missing: mcp_tools says
            # so, and lists it once more (lab 41, B4).
            if self.deferred or connectors.failed or self.unloadable:
                entries.append(self._mcp_tools_entry(connectors))
        self.entries = {entry.spec.name: entry for entry in entries}
        if len(self.entries) != len(entries):
            names = [entry.spec.name for entry in entries]
            taken = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(f"two offered tools have one name: {', '.join(taken)}")
        self.revision = 0
        """Counts the loads of connector tools, which change what a model call carries."""

    def _defer(self, connectors: Connectors, names: set[str]) -> None:
        """Index the connector tools not yet indexed. A connector tool is never offered
        under a name the harness gives a tool of its own, whether this run has that tool
        or not: the system prompt names some of them."""
        taken = names | HARNESS_TOOL_NAMES
        for entry in _group(
            [spec for spec in connectors.specs if spec.name not in self.deferred],
            lambda name, call: connectors.call(name, call.args),
            {},
            bound=True,
        ):
            if entry.spec.name in taken:
                _log.warning("connector tool %s is not offered: its name is taken", entry.spec.name)
                del connectors.tools[entry.spec.name]
            elif collides(entry.spec):
                _log.warning("connector tool %s is not offered: %s", entry.spec.name, COLLISION)
                self.unloadable[entry.spec.name] = COLLISION
                del connectors.tools[entry.spec.name]
            else:
                self.deferred[entry.spec.name] = entry

    def _mcp_tools_entry(self, connectors: Connectors) -> ToolEntry:
        return ToolEntry(mcp_tools_spec(connectors.index()), "read", _plain(self._mcp_tools))

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """The tools every model call carries: the run's own, and the connector tools
        loaded so far, each with the required ``purpose_note`` (Purpose 1)."""
        found: list[ToolSpec] = []
        for name, entry in self.entries.items():
            kept = self._offered.get(name)
            if kept is None or kept[0] is not entry.spec:
                kept = (entry.spec, offered(entry.spec))
                self._offered[name] = kept
            found.append(kept[1])
        return tuple(found)

    def _load(self, names: Sequence[str]) -> tuple[list[str], list[str], list[str], list[str]]:
        """Load connector tools into the run: those loaded now, before, unknown (a
        harness tool's name among them), and past the run's budget of schemas."""
        assert self.connectors is not None  # noqa: S101 - only with connectors
        added, before, unknown, over = self.connectors.take(names)
        for name in added:
            self.entries[name] = self.deferred[name]
        if added:
            self.revision += 1
        return added, before, unknown, over

    async def _mcp_tools(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        """Load connector tools by name (lab 39, B5), and list a query's matches, which
        it does not load (lab 40, B4)."""
        assert self.connectors is not None  # noqa: S101 - offered only with connectors
        names, query = args.get("names", []), args.get("query")
        if (
            not isinstance(names, list)
            or len(cast(list[object], names)) > MAX_LOAD
            or not all(isinstance(name, str) for name in cast(list[object], names))
            or (query is not None and (not isinstance(query, str) or len(query) > MAX_QUERY))
            or (not names and not query and not self.connectors.failed)
        ):
            return _failure(
                ErrorKind.INVALID_REQUEST,
                f"give names, a list of at most {MAX_LOAD} tool names from the index, or a "
                f"query of at most {MAX_QUERY} characters",
            )
        connectors = self.connectors
        answer: dict[str, Any] = {}
        notes: list[str] = []
        if connectors.failed and not connectors.retried:
            # A server whose listing failed at the run's start is listed once more.
            relisted = await connectors.relist()
            if relisted:
                self._defer(connectors, set(self.entries) - set(self.deferred))
                self.entries[MCP_TOOLS] = self._mcp_tools_entry(connectors)
                self.revision += 1
                answer["relisted"] = relisted
                notes.append(
                    "listed again: their tools are in mcp_tools' index from your next turn, "
                    "and can be loaded by name now"
                )
        if connectors.failed:
            answer["unavailable"] = dict(connectors.failed)
            notes.append(
                "these servers could not be listed: their tools are unavailable in this run, "
                "not missing; say so rather than conclude that no such tool exists"
            )
        asked = list(dict.fromkeys(cast(list[str], names)))
        added, before, unknown, over = self._load([n for n in asked if n not in self.unloadable])
        answer["loaded"] = added
        if before:
            answer["already_loaded"] = before
        harness = [name for name in unknown if name in HARNESS_TOOL_NAMES]
        refused = [name for name in unknown if name in connectors.narrowed]
        if unknown := [name for name in unknown if name not in harness and name not in refused]:
            answer["unknown"] = unknown
        if refused:
            # A delegated task's brief left them out (DA-6).
            answer["not_permitted"] = refused
            notes.append(
                "not_permitted_for_role: your brief's connectors do not include these tools; "
                "tell your parent if the work needs them"
            )
        if harness:
            answer["harness_tools"] = harness
            notes.append(
                "these are the harness's own tools, not connector tools: they need no "
                "loading, so call them by name when you have them"
            )
        if self.unloadable:
            answer["not_loadable"] = dict(self.unloadable)
            notes.append(
                "these connector tools cannot be loaded in this run, for the reason given: "
                "they are unavailable, not missing"
            )
        if over:
            answer["not_loaded"] = over
            notes.append(
                f"loading these would pass the run's {MAX_LOADED_CHARS:,} characters of "
                "loaded tool schemas: work with the tools loaded"
            )
        elif added or before:
            notes.append("call a loaded tool by its name; your tools now include it")
        if query:
            found = connectors.search(query)
            answer["matches"] = [
                {
                    "name": name,
                    "server": connectors.tools[name].group,
                    "purpose": connectors.tools[name].purpose,
                    "loaded": name in connectors.loaded,
                }
                for name in found
            ]
            notes.append(
                "a query loads nothing: load the tools you need with names"
                if found
                else "no connector tool matches the query well. Connector tools reach outside "
                "services, such as paper archives and web search; the harness's own tools, "
                "such as ledger_*, document_* and plan_*, are not connector tools and need "
                "no loading"
            )
        if notes:
            answer["note"] = "; ".join(notes)
        record = {
            **answer,
            "sha256": {name: self.connectors.tools[name].sha256 for name in added},
        }
        return ToolOutcome(dump(answer), {"mcp_tools": record})

    async def call(
        self,
        call: ToolCall,
        operation: str,
        *,
        model: str | None = None,
        gates: Sequence[TurnHook] = (),
    ) -> ToolOutcome:
        """Run one tool call; definite failures become errors for the model.

        Its ``purpose_note`` is checked first, before anything else reads the call: a
        call without one, or with one outside its limits, is refused with
        ``invalid_request``, and nothing runs. The rest of the harness, its gates and
        handlers, raw ones included, gets the call without it; the record keeps it as
        ``purpose`` (Purpose 2 and 3).

        ``model`` is the model that asked for the call, as the gateway named it, which
        document revisions record. Each of ``gates`` may refuse the call by its effect
        class; with plan tools, the plan's gate is always among them. After the call, the
        run's next safe point, the result carries notices of changes others made to the
        documents the run has seen (LD-15).
        """
        try:
            parts = split(call)
        except PurposeError as error:
            return _failure(ErrorKind.INVALID_REQUEST, error.message)
        call, purpose = parts.call, parts.purpose
        outcome = await self._dispatch(call, operation, model, gates)
        if self.tool_mix is not None:
            self.tool_mix.count(call.name, call.arguments, outcome.record)
        outcome.record["purpose"] = purpose
        return outcome

    async def _dispatch(
        self,
        call: ToolCall,
        operation: str,
        model: str | None,
        gates: Sequence[TurnHook],
    ) -> ToolOutcome:
        entry = self.entries.get(call.name)
        if entry is None and self.delegated and call.name not in self.deferred:
            return _failure(
                ErrorKind.POLICY_DENIED,
                f"not_permitted_for_role: a delegated run is not offered {call.name!r}",
            )
        if entry is None and call.name in self.deferred:
            # A connector tool called before it was loaded: loaded now, and called, so
            # its parameters are in the model's tools from the next turn.
            if self._load([call.name])[3]:
                return _failure(
                    ErrorKind.INVALID_REQUEST,
                    f"{call.name} is not loaded: loading it would pass the run's "
                    f"{MAX_LOADED_CHARS:,} characters of loaded tool schemas",
                )
            entry = self.entries[call.name]
        if entry is None:
            return _failure(ErrorKind.INVALID_REQUEST, f"there is no tool named {call.name!r}")
        if self.plans is not None and not any(isinstance(gate, Plans) for gate in gates):
            gates = (*gates, Plans(self.plans))
        try:
            outcome = await self._call(entry, call, operation, model, gates)
        except UnknownEffectError:
            raise
        except Exception as exc:  # one bad call must not end the task
            _log.exception("the %s tool failed", call.name)
            outcome = _failure(
                ErrorKind.INTERNAL,
                f"the {call.name} tool failed unexpectedly ({type(exc).__name__})",
            )
        # The review's list of what the run did reads it (CR-2 as amended for lab 40).
        outcome.record.setdefault("effect", entry.effect)
        if self.recorder is not None and outcome.written is not None:
            try:
                recorded = await self.recorder.record(
                    outcome.written,
                    tool_call=call.id or operation,
                    model=model,
                    operation=operation,
                )
            except Exception as error:  # recording never costs the call its result
                _log.exception("recording the files of the %s call failed", call.name)
                outcome.record["recorded_files"] = {"failed": type(error).__name__}
            else:
                report = recorded.summary()
                if report is not None:
                    outcome.record["recorded_files"] = {**report, "files": recorded.files[:100]}
                    if not entry.whole:
                        outcome.text = with_recorded(outcome.text, report)
        if entry.whole:
            outcome.whole = True
            return outcome
        if self.documents is not None:
            try:
                notices = await self.documents.notices()
            except Exception:  # a notice that cannot be made must not cost the result
                _log.exception("the document notices failed")
                notices = []
            if notices:
                outcome.text = with_notices(outcome.text, notices)
                outcome.record["document_notices"] = notices
        return outcome

    def turn_hooks(self) -> tuple[TurnHook, ...]:
        """New hooks for this run's notice sources, in the order they reach the model."""

        def current(name: str, default: object) -> object | None:
            if name == caps.KERNELS.name:
                return default if self.check_kernels else None
            # Direct callers have always been able to replace public controllers
            # before constructing hooks. Their entries remain the original offer.
            return cast(object | None, getattr(self, name, default))

        return self.capabilities.turn_hooks(current, output_chars=self.output_chars)

    async def _call(
        self,
        entry: ToolEntry,
        call: ToolCall,
        operation: str,
        model: str | None,
        gates: Sequence[TurnHook],
    ) -> ToolOutcome:
        for gate in gates:
            refused = await gate.gate_call(entry.spec.name, entry.effect)
            if refused is not None:
                return _failure(ErrorKind(refused.kind), refused.message)
        args: dict[str, Any] = {}
        if not entry.raw:
            try:
                arguments: object = json.loads(call.arguments or "{}")
            except (ValueError, RecursionError) as exc:
                return _failure(ErrorKind.INVALID_REQUEST, f"arguments are not JSON: {exc}")
            if not isinstance(arguments, dict):
                return _failure(ErrorKind.INVALID_REQUEST, "arguments must be a JSON object")
            args = cast(dict[str, Any], arguments)
        try:
            outcome = await entry.handler(Request(call, args, operation, model))
        except DocumentError as error:
            return _failure(ErrorKind(error.kind), error.message)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ()))
            return _failure(ErrorKind.INVALID_REQUEST, f"{where}: {first.get('msg')}")
        except WorkerAPIError as exc:
            if exc.uncertain:
                raise UnknownEffectError(
                    "worker_unknown_effect",
                    f"the {call.name} operation has an unknown outcome; "
                    "reconcile its original operation before continuing",
                ) from exc
            return _failure(exc.kind, exc.message)
        if entry.bound:
            outcome.text = self._cut(outcome.text, outcome.record)
        return outcome

    # Executions.

    def _bindings(self, args: dict[str, Any]) -> dict[str, str]:
        """The variables the run's staged skill packs set in its executions (SK-16); a
        model that sends its own is refused."""
        if "bindings" in args:
            raise WorkerAPIError(
                ErrorKind.INVALID_REQUEST, "bindings are the staged skills', never a call's"
            )
        stager = self.skills.stager if self.skills is not None else None
        return dict(stager.bindings) if stager is not None else {}

    def _binding_inventory(self) -> str | None:
        stager = self.skills.stager if self.skills is not None else None
        return stager.inventory_digest if stager is not None else None

    async def _shell(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ShellRequest.model_validate(
            {
                "operation_id": operation,
                "context": self.context,
                **_known(args, "script", "cwd", "timeout_seconds"),
                "bindings": self._bindings(args),
                "binding_inventory": self._binding_inventory(),
            }
        )
        interpreter = shell_computation(request.script or "")

        async def nudge(_: Execution) -> dict[str, Any]:
            note = self._kernel_note(interpreter)
            return {} if note is None else {"note": note}

        return await self._run(request, added=nudge)

    def _kernel_note(self, interpreter: Interpreter | None) -> str | None:
        """The platform's note on the first shell call of the run that ran an interpreter
        on code (Kernels 13): later ones are counted, silently. A run offered no kernel
        gets none."""
        mix = self.tool_mix
        if interpreter is None or mix is None or mix.nudged:
            return None
        tool = interpreter if interpreter in self.languages else None
        if tool is None and "python" in self.languages:
            tool = "python"
        if tool is None:
            return None
        mix.nudged = True
        jobs = (
            ", and start_job runs a long computation in the background"
            if self.jobs is not None
            else ""
        )
        name = "Sage" if interpreter == "sage" else "Python"
        return (
            f"Platform note: this ran {name} through the shell: a {tool} cell keeps "
            f"variables, imports and loaded data between calls{jobs}."
        )

    async def _run(
        self,
        request: ShellRequest | CellRequest,
        *,
        added: Callable[[Execution], Awaitable[dict[str, Any]]] | None = None,
    ) -> ToolOutcome:
        """Run ``request`` and describe its execution for the model, with what
        ``added`` gives beside it: the platform's notes on this execution, which never
        cost the call its result."""
        replayed = False

        def submitted(_: Execution, created: bool) -> None:
            nonlocal replayed
            replayed = not created

        execution = await self.client.run(request, on_submitted=submitted)
        record: dict[str, Any] = {
            "execution": execution.model_dump(mode="json"),
            "replayed": replayed,
        }
        extra: dict[str, Any] = {}
        if added is not None:
            try:
                extra = await added(execution)
            except Exception:  # a note that cannot be made must not cost the result
                _log.exception("the platform's note on %s failed", execution.execution_id)
        written = Written.of(execution)
        planned = {name: saved_path(execution, name) for name, _, _ in _texts(execution)}
        text, sampled = self._fit(execution, replayed, planned, {}, extra)
        if not sampled:
            return ToolOutcome(text, record, written=written)
        saved, unsaved = await self._save(execution, sampled)
        if unsaved:
            text, _ = self._fit(execution, replayed, saved, unsaved, extra)
        record["saved_output"] = saved
        paths = (*written.paths, *(path for path in saved.values() if path not in written.paths))
        texts = {saved[name]: text for name, text, _ in _texts(execution) if name in saved}
        return ToolOutcome(text, record, written=replace(written, paths=paths), saved=texts)

    async def _save(
        self, execution: Execution, names: Sequence[str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Save each of the ``names`` texts whole to its file in the session workspace
        (tools spec Shell 5): the paths saved, and why the others were not."""
        texts = {name: text for name, text, _ in _texts(execution)}
        saved: dict[str, str] = {}
        unsaved: dict[str, str] = {}
        for name in names:
            path = saved_path(execution, name)
            content = texts[name].encode("utf-8", "replace")
            try:
                await self.client.write_file(
                    WriteFileRequest(
                        context=self.context,
                        path=path,
                        content_b64=base64.b64encode(content).decode(),
                    )
                )
            except WorkerAPIError as error:
                unsaved[name] = f"{error.kind.value}: {bound(error.message, 200)}"
                continue
            except Exception:  # saving never costs the call its result
                _log.exception("saving the output of %s failed", execution.execution_id)
                unsaved[name] = "internal: the harness could not write it"
                continue
            saved[name] = path
        return saved, unsaved

    async def _pdf(self, kind: str, args: dict[str, Any], operation: str) -> ToolOutcome:
        """A PDF operation in the sandbox; the helper's JSON is formatted for the model."""
        request = PdfRequest.model_validate(
            {
                "operation_id": operation,
                "context": self.context,
                "operation": kind,
                **_known(args, "path", "pages", "query", "page", "region", "dpi"),
                **_known(args, "include_artifacts"),
            }
        )
        replayed = False

        def submitted(_: Execution, created: bool) -> None:
            nonlocal replayed
            replayed = not created

        execution = await self.client.run(request, on_submitted=submitted)
        record: dict[str, Any] = {
            "execution": execution.model_dump(mode="json"),
            "replayed": replayed,
        }
        written = Written.of(execution)
        result = execution.result
        stdout = result.stdout if result is not None else None
        helper_error = result is not None and result.exit_code == PDF_HELPER_ERROR
        if stdout is None or (execution.state is not ExecutionState.SUCCEEDED and not helper_error):
            return ToolOutcome(self._summary(execution, replayed=replayed), record, written=written)
        if stdout.truncated:
            outcome = _failure(
                ErrorKind.INVALID_REQUEST,
                "the result was larger than the output limit; ask for fewer pages",
            )
            outcome.written = written
            return outcome
        try:
            document: object = json.loads(stdout.text)
        except (ValueError, RecursionError):
            document = None
        if not isinstance(document, dict):
            return ToolOutcome(self._summary(execution, replayed=replayed), record, written=written)
        parsed = cast(dict[str, Any], document)
        error = parsed.get("error")
        if isinstance(error, dict):
            failure = cast(dict[str, Any], error)
            try:
                kind_of = ErrorKind(str(failure.get("kind")))
            except ValueError:
                kind_of = ErrorKind.INTERNAL
            outcome = _failure(kind_of, str(failure.get("message", "the PDF helper failed")))
            outcome.written = written
            return outcome
        record["pdf"] = parsed
        text = format_pdf(kind, parsed, boxes=args.get("boxes") is True)
        return ToolOutcome(self._cut(text, record), record, written=written)

    async def _python(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        return await self._cell("python", args, operation)

    async def _sage(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        return await self._cell("sage", args, operation)

    async def _cell(self, language: Language, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = CellRequest.model_validate(
            {
                "operation_id": operation,
                "context": self.context,
                "language": language,
                "run": self.kernel_run,
                **_known(args, "code", "timeout_seconds"),
                "bindings": self._bindings(args),
                "binding_inventory": self._binding_inventory(),
            }
        )
        outcome = await self._run(request, added=partial(self._lost_state, language))
        outcome.record["kernel_replay_binding_free"] = (
            not request.bindings and request.binding_inventory is None
        )
        return outcome

    async def _lost_state(self, language: Language, execution: Execution) -> dict[str, Any]:
        """For a cell that reports its kernel's state lost, what the lost generation had
        computed, from the records (Kernels 7, as amended for A10): its cells by archive
        message ID and purpose, and the files they wrote that still exist."""
        result = execution.result
        if result is None or not result.state_lost or self.lost_kernels is None:
            return {}
        lost = await self.lost_kernels.lost(language, result.kernel or execution.kernel)
        if lost is None or not lost.cells:
            return {}
        saved = await self._existing(lost.written)
        return {"lost_state": _lost_state(lost, saved)}

    async def _existing(self, paths: Sequence[str]) -> list[str]:
        """The newest ``MAX_SAVED_FILES`` of ``paths`` that are regular files now, in
        their order, from one listing of each directory; one that cannot be listed is
        left out."""
        tried = list(paths)[-2 * MAX_SAVED_FILES :]
        found: set[str] = set()
        for directory in sorted({posixpath.dirname(path) or "." for path in tried}):
            try:
                listing = await self.client.list_files(
                    ListFilesRequest(context=self.context, path=directory)
                )
            except (WorkerAPIError, ValueError):
                continue
            files = {entry.name for entry in listing.entries if entry.type == "file"}
            found.update(
                path
                for path in tried
                if (posixpath.dirname(path) or ".") == directory
                and posixpath.basename(path) in files
            )
        return [path for path in tried if path in found][-MAX_SAVED_FILES:]

    async def _restart(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        language = args.get("language", self.languages[0] if self.languages else "python")
        if language not in self.languages:
            return _failure(ErrorKind.UNSUPPORTED, f"no {language!r} kernel in this environment")
        kernel = await self.client.restart_kernel(self.context, language, self.kernel_run)
        return ToolOutcome(
            dump(
                {
                    "restarted": True,
                    "kernel_generation": kernel.generation,
                    "state_lost": True,
                    "kernel": kernel.model_dump(mode="json"),
                }
            ),
            {"kernel": kernel.model_dump(mode="json")},
        )

    def _summary(self, execution: Execution, *, replayed: bool = False) -> str:
        """The execution for the model, in at most ``output_chars`` characters of JSON.

        Each text is bounded; while they add up to more, every bound is halved.
        """
        return self._fit(execution, replayed, None, {})[0]

    def _fit(
        self,
        execution: Execution,
        replayed: bool,
        saved: Mapping[str, str] | None,
        unsaved: Mapping[str, str],
        added: Mapping[str, Any] | None = None,
    ) -> tuple[str, list[str]]:
        """``_summary``'s text, with the texts it shows only in part: with ``saved``,
        it says they are samples and names their files, or why one was not saved; and
        ``added``, the platform's notes, each bounded as a note is."""
        limit = self.output_chars // 2
        while True:
            summary = _describe(execution, limit, replayed, self.kernel_run, saved, unsaved)
            for key, value in (added or {}).items():
                summary[key] = bound(value, limit) if isinstance(value, str) else value
            text = dump(summary)
            if len(text) <= self.output_chars or limit <= 64:
                return text, _sampled(execution, limit)
            limit //= 2

    def _bound(self, text: str) -> str:
        return bound(text, self.output_chars // 2)

    def _cut(self, text: str, record: dict[str, Any]) -> str:
        """``text`` bounded, and when that cut it, ``record['cut']`` says so: its length
        and the character ranges the model was shown, its head and its tail, so that the
        read register never takes a cut result for a whole read (CM-12)."""
        limit = self.output_chars // 2
        if len(text) > limit:
            half = limit // 2
            record["cut"] = {
                "length": len(text),
                "shown": [[0, half], [len(text) - half, len(text)]],
            }
        return bound(text, limit)

    # Files.

    async def _read(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ReadFileRequest.model_validate(
            {"context": self.context, **_known(args, "path", "offset", "length")}
        )
        content = await self.client.read_file(request)
        try:
            data = base64.b64decode(content.content_b64, validate=True)
        except binascii.Error:
            return _failure(ErrorKind.INTERNAL, "the worker sent file content that is not base64")
        try:
            # A range that ends inside a character stops before it; the next read starts there.
            text = codecs.getincrementaldecoder("utf-8")().decode(data, final=content.eof)
        except UnicodeDecodeError:
            text = None
        summary: dict[str, Any] = {
            "path": content.path,
            "size": content.size,
            "sha256": content.sha256,
        }
        if content.offset >= content.size and not data:
            summary["eof"] = (
                f"offset {content.offset} is at or past the end of this {content.size}-byte file"
            )
        part = self.output_chars // 2  # the most one call shows whole, in bytes or characters
        whole = False
        if text is None:
            summary["binary"] = f"{len(data)} bytes, not shown"
            end = content.offset + len(data)
        else:
            summary["content"] = self._bound(text)
            whole = summary["content"] == text
            end = content.offset + len(text.encode())
        if text is not None and not whole:
            summary["more"] = (
                f"shown in part: read it in ranges from offset {content.offset}, with length "
                f"{part} each"
            )
        elif end < content.size:
            summary["more"] = f"read again with offset {end} and length {part}"
        # What the model saw whole, as text: the bytes from ``offset`` (``shown``), and
        # whether that is the whole file, from its start to its end.
        shown = end - content.offset if whole else 0
        read = {
            "path": content.path,
            "sha256": content.sha256,
            "size": content.size,
            "offset": content.offset,
            "shown": shown,
            "complete": bool(content.offset == 0 and shown and end == content.size),
        }
        return ToolOutcome(dump(summary), {"read": read})

    async def _write(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        content = args.get("content")
        if not isinstance(content, str):
            return _failure(ErrorKind.INVALID_REQUEST, "content must be a string")
        try:
            encoded = content.encode()
        except UnicodeEncodeError:
            return _failure(
                ErrorKind.INVALID_REQUEST, "content is not valid text: it has an unpaired surrogate"
            )
        request = WriteFileRequest.model_validate(
            {
                "context": self.context,
                **_known(args, "path", "create_only", "expected_sha256"),
                "content_b64": base64.b64encode(encoded).decode(),
            }
        )
        written = await self.client.write_file(request)
        return ToolOutcome(
            dump({"written": request.path, **written.model_dump()}),
            {"write": written.model_dump()},
            written=Written((request.path,)),
        )

    async def _edit(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        for name in ("old_string", "new_string"):
            value = args.get(name)
            if not isinstance(value, str):
                return _failure(ErrorKind.INVALID_REQUEST, f"{name} must be a string")
            try:
                value.encode()
            except UnicodeEncodeError:
                return _failure(
                    ErrorKind.INVALID_REQUEST,
                    f"{name} is not valid text: it has an unpaired surrogate",
                )
        request = EditFileRequest.model_validate(
            {
                "context": self.context,
                **_known(
                    args, "path", "old_string", "new_string", "replace_all", "expected_sha256"
                ),
            }
        )
        edited = await self.client.edit_file(request)
        return ToolOutcome(
            dump({"edited": request.path, **edited.model_dump()}),
            {"edit": edited.model_dump()},
            written=Written((request.path,)),
        )

    async def _glob(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = GlobRequest.model_validate(
            {"context": self.context, **_known(args, "pattern", "path", "limit")}
        )
        listing = await self.client.glob_files(request)
        entries = [
            {
                "path": bound(entry.path, 512),
                "type": entry.type,
                "size": entry.size,
                "modified": entry.modified_at.isoformat(timespec="seconds"),
            }
            for entry in listing.entries
        ]
        shown = len(entries)
        while True:
            answer: dict[str, Any] = {
                "path": listing.path,
                "pattern": listing.pattern,
                "entries": entries[:shown],
                "truncated": listing.truncated or shown < len(entries),
            }
            note = _search_note(listing.stopped, shown < len(entries), listing.skipped)
            if note:
                answer["note"] = note
            text = dump(answer)
            if len(text) <= self.output_chars or shown == 0:
                break
            shown //= 2
        record = {
            "entries": len(listing.entries),
            "shown": shown,
            "truncated": answer["truncated"],
            "stopped": listing.stopped,
            "scanned": listing.scanned,
        }
        return ToolOutcome(text, {"glob": record})

    async def _grep(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = GrepRequest.model_validate(
            {
                "context": self.context,
                **_known(
                    args,
                    "pattern",
                    "path",
                    "glob",
                    "mode",
                    "case_insensitive",
                    "context_lines",
                    "max_results",
                ),
            }
        )
        found = await self.client.grep_files(request)
        items: list[Any]
        if found.mode == "files":
            key, items = "files", [bound(path, 512) for path in found.files]
        elif found.mode == "count":
            key = "counts"
            items = [{"path": bound(c.path, 512), "count": c.count} for c in found.counts]
        else:
            key, items = "lines", list(found.lines)
        shown = len(items)
        while True:
            answer: dict[str, Any] = {
                "path": found.path,
                "pattern": found.pattern,
                "mode": found.mode,
                key: items[:shown],
                "truncated": found.truncated or shown < len(items),
            }
            skipped = {
                name: count
                for name, count in (
                    ("binary", found.skipped_binary),
                    ("too_large", found.skipped_large),
                    ("unreadable", found.skipped_unreadable),
                )
                if count
            }
            if skipped:
                answer["skipped_files"] = skipped
            note = _search_note(found.stopped, shown < len(items), found.skipped)
            if not items and not found.truncated:
                note = (note + "; " if note else "") + "no match in the files searched"
            if note:
                answer["note"] = note
            text = dump(answer)
            if len(text) <= self.output_chars or shown == 0:
                break
            shown //= 2
        record = {
            "mode": found.mode,
            "results": len(items),
            "shown": shown,
            "truncated": answer["truncated"],
            "stopped": found.stopped,
            "scanned_files": found.scanned_files,
            "scanned_bytes": found.scanned_bytes,
        }
        return ToolOutcome(text, {"grep": record})

    async def _list(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ListFilesRequest.model_validate({"context": self.context, **_known(args, "path")})
        listing = await self.client.list_files(request)
        entries = [
            {"name": bound(entry.name, 256), "type": entry.type, "size": entry.size}
            for entry in listing.entries
        ]
        shown = min(len(entries), SHOWN_ENTRIES)
        while True:
            text = dump(
                {
                    "path": listing.path,
                    "entries": entries[:shown],
                    "truncated": listing.truncated or shown < len(entries),
                }
            )
            if len(text) <= self.output_chars or shown == 0:
                return ToolOutcome(text)
            shown //= 2

    async def _admit(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        hosts = args.get("hosts")
        if (
            not isinstance(hosts, list)
            or not 1 <= len(cast(list[object], hosts)) <= MAX_ADMISSION_HOSTS
            or not all(isinstance(host, str) for host in cast(list[object], hosts))
        ):
            return _failure(
                ErrorKind.INVALID_REQUEST,
                f"hosts must be a list of 1 to {MAX_ADMISSION_HOSTS} host names",
            )
        wanted = [_host_port(host) for host in cast(list[str], hosts)]
        result = await self.client.admit_hosts(
            self.context.environment_id, wanted, context=self.context
        )
        decisions = [decision.model_dump(mode="json") for decision in result.decisions]
        lines = [
            f"{d.host}:{d.port} reachable"
            if d.admitted
            else f"{d.host}:{d.port} refused: {bound(d.reason, 300)}"
            for d in result.decisions
        ]
        return ToolOutcome("\n".join(lines), {"admissions": decisions})

    async def _find_artifacts(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        """The project's artifacts, so a later run can pin one as evidence (lab 38, B6)."""
        assert self.artifacts is not None  # noqa: S101 - offered only with an inventory
        texts = _known(args, "path", "contains", "media_type")
        results_only, before = args.get("results_only", False), args.get("before")
        if (
            not all(isinstance(value, str) for value in texts.values())
            or not isinstance(results_only, bool)
            or (before is not None and (type(before) is not int or before < 1))
        ):
            return _failure(
                ErrorKind.INVALID_REQUEST,
                "path, contains and media_type are text, results_only true or false, "
                "and before a cursor from next_before",
            )
        found, after, results = await self.artifacts.find(
            **texts, results_only=results_only, before=before
        )
        entries = [
            {
                "path": entry.path,
                "artifact_id": entry.reference.artifact_id,
                "revision": entry.reference.revision,
                "sha256": entry.reference.sha256,
                "size": entry.reference.size,
                "media_type": entry.reference.media_type,
                "result": entry.reference.artifact_id in results,
                "recorded_at": entry.created_at.isoformat(),
                "by": entry.publication.kind,
                **({"deleted": True} if entry.deleted else {}),
                **({"content_kept": False} if not entry.content_retained else {}),
            }
            for entry in found
        ]
        shown = len(entries)
        while True:
            text = dump(
                {
                    "artifacts": entries[:shown],
                    "next_before": after,
                    **(
                        {"omitted": f"{len(entries) - shown} more here: narrow with path"}
                        if shown < len(entries)
                        else {}
                    ),
                }
            )
            if len(text) <= self.output_chars or shown == 0:
                return ToolOutcome(text, {"artifacts": len(entries)})
            shown //= 2

    async def _publish(self, call: Request) -> ToolOutcome:
        request = PublishRequest.model_validate(
            {"context": self.context, **_known(call.args, "path", "media_type")}
        )
        if self.artifacts is not None:
            result = await self.artifacts.publish(
                request, call.operation, tool_call=call.call.id, model=call.model
            )
            worker = result.worker.model_dump(mode="json") if result.worker is not None else None
            receipt = result.receipt.model_dump(mode="json")
            return ToolOutcome(
                self._bound(dump({"published": worker, "inventory": receipt})),
                {"artifact": worker, "artifact_inventory": receipt},
            )
        artifact = await self.client.publish(request)
        record = artifact.model_dump(mode="json")
        return ToolOutcome(dump({"published": record}), {"artifact": record})


def _describe(
    execution: Execution,
    limit: int,
    replayed: bool,
    run: str | None = None,
    saved: Mapping[str, str] | None = None,
    unsaved: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The parts of an execution the model reads, each text within ``limit`` characters.

    Of the session's running kernels, the run reads only those its cells use: the
    session's for a main run, its own for a delegated one (``run``). With ``saved``,
    the texts shown only in part are called a sample, with the file each is saved in,
    or, from ``unsaved``, why it is not (tools spec Shell 5).
    """
    summary: dict[str, Any] = {"state": execution.state.value}
    if replayed:
        summary["replayed"] = (
            "this call was made before with the same operation ID; this is its earlier result"
        )
    result = execution.result
    if result is not None:
        if result.exit_code is not None or result.signal is not None:
            summary["exit_code"] = result.exit_code
            if result.signal is not None:
                summary["signal"] = result.signal
            if (
                result.exit_code not in (None, 0)
                and not (result.stdout and result.stdout.total_bytes)
                and not (result.stderr and result.stderr.total_bytes)
            ):
                summary["diagnostic"] = (
                    "The command exited without captured stdout or stderr. If it redirected "
                    "output, inspect that log; otherwise rerun the failing step with diagnostics "
                    "visible."
                )
        for stream in (result.stdout, result.stderr):
            if stream is not None and stream.total_bytes:
                summary[stream.name] = bound(stream.text, limit)
                if stream.truncated:
                    summary[f"{stream.name}_total_bytes"] = stream.total_bytes
        outputs: list[str] = [_output(output.data, limit) for output in result.outputs]
        if len(outputs) > SHOWN_OUTPUTS:
            half = SHOWN_OUTPUTS // 2
            omitted = f"[… {len(outputs) - 2 * half} outputs omitted …]"
            outputs = [*outputs[:half], omitted, *outputs[-half:]]
        if outputs:
            summary["outputs"] = outputs
        if saved is not None:
            summary.update(_sample_note(execution, limit, saved, unsaved or {}))
        if result.error is not None:
            error: dict[str, Any] = {
                "kind": result.error.kind.value,
                "message": bound(result.error.message, limit),
            }
            if result.error.traceback:
                traceback = "\n".join(result.error.traceback[-TRACEBACK_LINES:])
                error["traceback"] = bound(traceback, limit)
            summary["error"] = error
        if result.kernel is not None:
            summary["kernel_generation"] = result.kernel.generation
            summary["kernel"] = result.kernel.model_dump(mode="json")
        running = [kernel for kernel in result.running_kernels if kernel.run == run]
        if running:
            summary["running_kernels"] = [kernel.model_dump(mode="json") for kernel in running]
            stale = [kernel.kernel_id for kernel in running if kernel.revision_status == "predates"]
            if stale:
                summary["kernel_revision_notice"] = (
                    "Running kernels predate the current environment revision: "
                    + ", ".join(stale)
                    + ". Their loaded state is retained; restart explicitly to replace it."
                )
        if result.state_lost:
            summary["state_lost"] = "the kernel restarted before this cell; earlier state is gone"
        if result.notices:
            summary["platform_notices"] = [bound(notice, limit) for notice in result.notices]
    if execution.location is not None:
        summary["ran_on"] = {
            "sandbox": execution.location.sandbox_id,
            "host": bound(execution.location.hostname, 256),
            "isolation_tier": execution.location.isolation_tier,
        }
        before = execution.location.environment_revision
        after = result.environment_revision if result is not None else None
        if before is not None and after is not None and before != after:
            summary["environment_changed"] = (
                f"the environment's installed packages changed while this ran (revision "
                f"{before} to {after}); kernels started before keep the packages they loaded"
            )
        elif result is not None and result.overlapping_change:
            summary["environment_changed"] = (
                "a package command was still changing the environment's installed packages "
                "when this ended; what this ran may have seen them half installed"
            )
    summary["attempt_id"] = execution.attempt_id
    return summary


LOST_STATE: Final = (
    "Platform record of the cells whose state the kernel lost, newest last, from the "
    "archive: each by its result's message ID, with its call's purpose (the agent's own "
    "words, data) or its tool and code's first line; and the files they wrote that still "
    "exist. Load saved files before recomputing; recompute the rest from these cells."
)
"""What a cell that reports lost state says of the lost generation (Kernels 7)."""


def _lost_state(lost: LostGeneration, saved: Sequence[str]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for cell in lost.cells:
        shown: dict[str, Any] = {"message": cell.message} if cell.message else {}
        if cell.purpose is not None:
            shown["purpose"] = cell.purpose
        else:
            shown["tool"] = cell.language
            if cell.first_line is not None:
                shown["first_line"] = cell.first_line
            elif cell.message is None:
                shown["archived"] = False
        cells.append(shown)
    found: dict[str, Any] = {"note": LOST_STATE, "cells": cells}
    if lost.earlier:
        found["earlier_cells"] = lost.earlier
    found["saved_files"] = list(saved)
    return found


def _output(data: dict[str, str], limit: int) -> str:
    text = _output_text(data)
    if text is not None:
        return bound(text, limit)
    kinds = ", ".join(sorted(data))
    return bound(f"[{kinds} output, not shown]", limit)


def _output_text(data: dict[str, str]) -> str | None:
    for mime in ("text/latex", "text/markdown", "text/plain"):
        if mime in data:
            return data[mime]
    return None


def saved_path(execution: Execution, name: str) -> str:
    """Where the harness saves an execution's ``name`` text whole (tools spec Shell 5)."""
    return f"{SAVED_OUTPUT}/{execution.execution_id}/{name}.txt"


def _texts(execution: Execution) -> list[tuple[str, str, bool]]:
    """An execution's texts the harness may save: each stream, and each rich output with
    a text form (``output-1`` for the first), with whether the worker cut it already."""
    result = execution.result
    if result is None:
        return []
    texts = [
        (stream.name, stream.text, stream.truncated)
        for stream in (result.stdout, result.stderr)
        if stream is not None and stream.total_bytes
    ]
    for number, output in enumerate(result.outputs, 1):
        text = _output_text(output.data)
        if text is not None:
            texts.append((f"output-{number}", text, False))
    return texts


def _sampled(execution: Execution, limit: int) -> list[str]:
    """The texts the model sees only in part when each is bounded to ``limit``."""
    return [name for name, text, cut in _texts(execution) if cut or len(text) > limit]


def _sample_note(
    execution: Execution, limit: int, saved: Mapping[str, str], unsaved: Mapping[str, str]
) -> dict[str, Any]:
    """What tells the model that a text it sees in part is a sample (lane K's triage,
    P4): which texts, where each is saved whole, and what was not kept or saved."""
    names = _sampled(execution, limit)
    if not names:
        return {}
    files = {name: saved[name] for name in names if name in saved and name not in unsaved}
    note = (
        f"Shown here only in part, as head and tail with the middle left out: "
        f"{', '.join(names)}. What is shown is a sample: take no counts, identifiers, rows "
        f"or claims about all or none of it from what you see."
    )
    if files:
        note += " Each is saved whole in its file under saved: read it in parts or compute from it."
    result = execution.result
    for stream in () if result is None else (result.stdout, result.stderr):
        if stream is not None and stream.truncated and stream.name in files:
            note += (
                f" The worker kept only the first and last parts of {stream.name} "
                f"({stream.total_bytes} bytes in all), so its file is a sample too: to keep "
                f"all of an output this large, write it to a file in the script."
            )
    for name in names:
        if name in unsaved:
            note += f" {name} could not be saved ({unsaved[name]})."
    summary: dict[str, Any] = {"sample": note}
    if files:
        summary["saved"] = files
    return summary


def bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters omitted …]\n{text[-half:]}"


PDF_HELPER_ERROR: Final = 3
"""The PDF helper's exit status for an error it describes in JSON."""


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, Any]]:
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def format_pdf(kind: str, result: dict[str, Any], *, boxes: bool = False) -> str:
    """A PDF helper result as text for the model, with lines citable as ``p<page>:<line>``;
    with ``boxes``, each page line after its citation shows its box, as ``pdf_render``
    takes a region."""
    source = _mapping(result.get("source"))
    # The full SHA-256: the platform matches a quote to the PDF it was read from (CR-4).
    # The path escaped: a name with a line break plants no line of its own (CR-4 review).
    path = str(source.get("path")).encode("unicode_escape").decode("ascii")
    head = f"{path} (sha256 {source.get('sha256', '')})"
    lines: list[str] = []
    if kind == "info":
        pages = _items(result.get("pages"))
        lines.append(f"{head}: {result.get('page_count')} pages")
        labels = ", ".join(f"{page.get('index')}: {page.get('label')}" for page in pages)
        lines.append(f"pages (physical: printed label): {labels}")
        empty = [str(page.get("index")) for page in pages if not page.get("has_text")]
        if empty:
            lines.append("pages without extractable text (render them): " + ", ".join(empty))
        for entry in _items(result.get("outline")):
            level = entry.get("level")
            indent = "  " * max(0, (level if isinstance(level, int) else 1) - 1)
            lines.append(f"{indent}- {entry.get('title')} -> p{entry.get('page_index')}")
    elif kind == "pages":
        lines.append(head)
        for page in _items(result.get("pages")):
            index = page.get("index")
            lines.append(f"--- page {index} (printed {page.get('label')}) ---")
            for line in _items(page.get("lines")):
                box = f"{_box(line.get('bbox'))} " if boxes else ""
                lines.append(f"[p{index}:{line.get('n')}] {box}{_marked(line)}{line.get('text')}")
            left = page.get("artifacts_left_out")
            if isinstance(left, list) and left:
                numbers = ", ".join(str(n) for n in cast(list[object], left))
                lines.append(
                    f"(lines {numbers}: running headers, footers or page numbers, left out; "
                    "the other lines keep their numbers, and include_artifacts shows them)"
                )
            if page.get("note"):
                lines.append(f"({page.get('note')})")
            if page.get("truncated"):
                lines.append("(the page has more lines than were returned)")
    elif kind == "search":
        hits = _items(result.get("hits"))
        lines.append(f"{len(hits)} hits for {result.get('query')!r} in {head}")
        for hit in hits:
            lines.append(
                f"[p{hit.get('page_index')}:{hit.get('n')}] (printed {hit.get('page_label')}) "
                f"{_marked(hit)}{hit.get('text')}"
            )
        if result.get("truncated"):
            lines.append("(more hits were not returned; search for something narrower)")
    else:
        image = _mapping(result.get("image"))
        region = result.get("region")
        where = f"region {region} of " if region else ""
        lines.append(
            f"rendered {where}page {result.get('page_index')} "
            f"(printed {result.get('page_label')}) of {head} at {result.get('dpi')} dpi "
            f"to {image.get('path')}: {image.get('width')}x{image.get('height')} px, "
            f"sha256 {image.get('sha256')}. Publish it to keep it as evidence."
        )
    return "\n".join(lines)


def _box(value: object) -> str:
    """A line's box as ``pdf_render`` takes a region: ``[x0, y0, x1, y1]`` in points."""
    numbers = cast(list[object], value) if isinstance(value, list) else []
    if len(numbers) != 4 or not all(isinstance(n, int | float) for n in numbers):
        return "[no box]"
    return "[" + ", ".join(f"{cast(float, n):g}" for n in numbers) + "]"


def _marked(line: Mapping[str, object]) -> str:
    """What a PDF line that is a running header, footer or page number starts with."""
    return "(header/footer) " if line.get("artifact") is True else ""


def _host_port(text: str) -> HostPort:
    """``host``, ``host:port``, or ``[address]:port`` for an IPv6 literal."""
    host, port = text.strip(), None
    if host.startswith("["):
        address, _, rest = host[1:].partition("]")
        host = address
        port = rest[1:] if rest.startswith(":") else None
    elif host.count(":") == 1:
        host, _, port = host.partition(":")
    if port is None:
        return HostPort(host=host)
    if not port.isdigit():
        return HostPort(host=text.strip())  # refused by the worker as no host it knows
    return HostPort(host=host, port=int(port))


_STOPS: Final[dict[str, str]] = {
    "results": "more matched than the limit: narrow the path or pattern, or raise the limit",
    "entries": "the search stopped at its limit of entries walked: narrow the path or glob",
    "depth": "directories past the search's depth limit were not searched",
    "bytes": "the search stopped at its limit of bytes read: narrow the path or glob",
    "time": "the search stopped at its time limit: narrow the path or glob",
    "reply": "the results passed the reply's size limit: narrow the path or pattern",
}


def _search_note(stopped: SearchStop | None, cut: bool, skipped: Sequence[str]) -> str:
    """What a glob's or grep's result leaves out, and why, for the model."""
    notes: list[str] = []
    if stopped is not None:
        notes.append(_STOPS[stopped])
    if cut:
        notes.append("shown in part to fit the result: narrow the search")
    if skipped:
        notes.append(f"not searched: {', '.join(skipped)} (name one as path to search it)")
    return "; ".join(notes)


def _known(args: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: args[name] for name in names if name in args}


def _failure(kind: ErrorKind, message: str) -> ToolOutcome:
    return ToolOutcome(
        dump({"error": {"kind": kind.value, "message": bound(message, 4000)}}),
        {"error": kind.value},
    )
