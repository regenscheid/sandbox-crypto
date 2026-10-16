# SPDX-License-Identifier: MIT
"""The worker API: execution requests and results, files and capabilities.

docs/specs/tools.md defines the behavior. Requests name an operation ID chosen
by the client: submitting the same ID again returns the same execution.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, model_validator

from .context import ExecutionContext
from .model import Contract, Counter, Identifier, Sha256

PROTOCOL_VERSION: Final = 6
"""The worker API's version. Releases are lockstep (docs/COMPATIBILITY.md): it changes with
any change to what the worker or its clients send, and each side refuses another version."""
PROTOCOL_HEADER: Final = "X-Worker-Protocol"
"""The header carrying ``PROTOCOL_VERSION`` on every worker request and response."""
MAX_TIMEOUT_SECONDS: Final = 24 * 3600
DEFAULT_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_OUTPUT_LIMIT: Final = 1024 * 1024
MAX_OUTPUT_LIMIT: Final = 64 * 1024 * 1024
MAX_PATH_BYTES: Final = 4096
MAX_READ_BYTES: Final = 8 * 1024 * 1024
MAX_EVENT_BYTES: Final = 32 * 1024 * 1024
"""The longest line of an event stream the worker sends; clients must accept lines this long.
A result too large for it keeps less of its output than ``output_limit_bytes`` allows."""
EVENT_HEARTBEAT_SECONDS: Final = 15.0
"""An idle event stream carries an empty line at least this often, so clients can time out."""


def validate_relative_path(value: str) -> str:
    """Check a workspace path: relative, without empty, ``.`` or ``..`` segments.

    ``.`` alone names the session workspace. A path starting with ``shared/``
    (or ``shared`` alone) names the project's shared area. Raises ValueError.
    """
    if value == ".":
        return value
    if not value or "\x00" in value or len(value.encode()) > MAX_PATH_BYTES:
        raise ValueError("invalid path")
    if value.startswith("/"):
        raise ValueError(f"path {value!r} must be relative to the workspace")
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"path {value!r} has an empty, '.' or '..' segment")
    return value


RelativePath = Annotated[str, AfterValidator(validate_relative_path)]
MediaType = Annotated[
    str,
    StringConstraints(
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9!#$\&^_.+\-]*/[A-Za-z0-9][A-Za-z0-9!#$\&^_.+\-]*"
        r"(?:; ?[A-Za-z0-9!#$\&^_.+\-]+=[A-Za-z0-9!#$\&^_.+\-]+)*$",
    ),
]
"""``type/subtype`` with optional ``; name=value`` parameters: no spaces, quotes or line breaks."""
Timeout = Annotated[float, Field(gt=0, le=MAX_TIMEOUT_SECONDS, allow_inf_nan=False)]
OutputLimit = Annotated[int, Field(ge=1024, le=MAX_OUTPUT_LIMIT)]
Language = Literal["python", "sage"]
"""Kernel languages, all through the Jupyter protocol (D41): Python, and SageMath's own
kernel, which preparses Sage syntax. R joins the same way."""
LANGUAGES: Final[tuple[Language, ...]] = ("python", "sage")
MAX_RUN_KERNELS: Final = 63
"""The most delegated runs of one session that have kernels of their own at once."""
MAX_SESSION_KERNELS: Final = (1 + MAX_RUN_KERNELS) * len(LANGUAGES)
"""The most kernels a session's listing holds: its own, one per language, and its runs'
(plans-and-delegation spec, PD-37)."""
KERNEL_OBSERVATION_BUSY: Final = "kernel observation capacity is busy"
"""The worker's refusal (``unavailable``) of a passive kernel observation while another one
reads the same environment: only one runs at a time, and none waits. It is expected when
two callers poll at once, such as the workspace's kernel panel and a run's check before a
turn, so a caller that can look again later treats it as no failure."""


def _no_nul(text: str, what: str) -> None:
    if "\x00" in text:
        raise ValueError(f"{what} must not contain NUL characters")


MAX_BINDINGS: Final = 64
"""Skill bindings one execution carries at most (docs/specs/skills.md, SK-16)."""
_VARIABLE: Final = r"^[A-Z][A-Z0-9_]{1,63}$"
Bindings = Annotated[
    dict[
        Annotated[str, StringConstraints(pattern=_VARIABLE)],
        Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATH_BYTES)],
    ],
    Field(max_length=MAX_BINDINGS),
]
"""Environment variables an execution gets beside the environment's own: the skill packs'
bindings, each naming a path in a staged pack (SK-16). The harness sets only those the
project allows (SK-31)."""


def _check_bindings(bindings: dict[str, str]) -> None:
    for value in bindings.values():
        _no_nul(value, "a binding")


def _check_command(script: str | None, argv: tuple[str, ...] | None) -> None:
    """Exactly one of a non-blank ``script`` and an ``argv`` naming a program; no NULs."""
    if (script is None) == (argv is None):
        raise ValueError("give exactly one of script and argv")
    if script is not None:
        if not script.strip():
            raise ValueError("script must not be empty")
        _no_nul(script, "script")
    if argv is not None:
        if not argv or not argv[0]:
            raise ValueError("argv must name a program")
        for argument in argv:
            _no_nul(argument, "argv")


class ShellRequest(Contract):
    """A shell call: a ``script`` for ``bash -c``, or an ``argv`` run without a shell."""

    kind: Literal["shell"] = "shell"
    operation_id: Identifier
    context: ExecutionContext
    script: str | None = None
    argv: tuple[str, ...] | None = None
    cwd: RelativePath = "."
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS
    output_limit_bytes: OutputLimit = DEFAULT_OUTPUT_LIMIT
    bindings: Bindings = Field(default_factory=dict[str, str])

    @model_validator(mode="after")
    def _one_command(self) -> Self:
        _check_command(self.script, self.argv)
        _check_bindings(self.bindings)
        return self


class JobRequest(Contract):
    """A shell execution that outlives its initiating call and turn (D83)."""

    kind: Literal["job"] = "job"
    operation_id: Identifier
    context: ExecutionContext
    script: str | None = None
    argv: tuple[str, ...] | None = None
    purpose: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    expected_progress: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    wake_session: bool = False
    cwd: RelativePath = "."
    timeout_seconds: Annotated[float, Field(gt=0, le=7 * 24 * 3600, allow_inf_nan=False)] | None = (
        None
    )
    """The job's time limit; None, the default, takes the policy's default when submitted."""
    output_limit_bytes: OutputLimit = DEFAULT_OUTPUT_LIMIT
    bindings: Bindings = Field(default_factory=dict[str, str])

    @model_validator(mode="after")
    def _one_command(self) -> Self:
        if not self.purpose.strip() or not self.expected_progress.strip():
            raise ValueError("purpose and expected_progress must not be blank")
        _no_nul(self.purpose, "purpose")
        _no_nul(self.expected_progress, "expected_progress")
        _check_command(self.script, self.argv)
        _check_bindings(self.bindings)
        return self


class CellRequest(Contract):
    """A cell for the session's kernel of ``language``, or a run's own (``run``)."""

    kind: Literal["cell"] = "cell"
    operation_id: Identifier
    context: ExecutionContext
    language: Language = "python"
    code: str
    run: Identifier | None = None
    """The run whose own kernel runs the cell: a delegated run's, which no other run
    shares (plans-and-delegation spec, PD-37). ``None`` is the session's kernel, which
    its main runs share."""
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS
    output_limit_bytes: OutputLimit = DEFAULT_OUTPUT_LIMIT
    bindings: Bindings = Field(default_factory=dict[str, str])
    """The environment of a kernel this cell starts; a kernel already running keeps its
    own, and the result's notices say so when they differ."""

    @model_validator(mode="after")
    def _checked(self) -> Self:
        _check_bindings(self.bindings)
        return self


Feature = Literal["pdf", "admission", "provisioning", "jobs"]
"""Capabilities beyond kernels that an environment can offer. ``pdf`` needs pdf_oxide (D59);
``admission`` means the worker admits hosts ahead of use (D69, ``AdmissionRequest``);
``provisioning`` that it installs capabilities and packages on request (``ProvisionRequest``,
Conda provisioning)."""
FEATURES: Final[tuple[Feature, ...]] = ("pdf", "jobs")
"""The features an executor may report; the worker adds ``admission`` itself."""
PdfOperation = Literal["info", "pages", "search", "render"]
PageList = Annotated[
    str, StringConstraints(pattern=r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$", max_length=200)
]
"""Pages by physical number, from 1, such as ``3-5,9``."""


class PdfRequest(Contract):
    """A PDF operation (docs/specs/pdf-sources.md), parsed inside the sandbox.

    The result's stdout holds the PDF helper's JSON: the operation's result, or, with
    exit code 3, ``{"error": {"kind", "message"}}``. A render writes a PNG into the
    session workspace, so it appears in ``written_files`` and can be published.
    """

    kind: Literal["pdf"] = "pdf"
    operation_id: Identifier
    context: ExecutionContext
    operation: PdfOperation
    path: RelativePath
    pages: PageList | None = None
    query: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    page: Annotated[int, Field(ge=1)] | None = None
    region: tuple[float, float, float, float] | None = None
    """``[x0, y0, x1, y1]`` in PDF points, origin at the page's bottom left."""
    dpi: Annotated[int, Field(ge=18, le=300)] = 150
    include_artifacts: bool = False
    out: RelativePath | None = None
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS
    output_limit_bytes: OutputLimit = DEFAULT_OUTPUT_LIMIT

    @model_validator(mode="after")
    def _arguments(self) -> Self:
        needs: dict[str, tuple[str, object]] = {
            "pages": ("pages", self.pages),
            "search": ("query", self.query),
            "render": ("page", self.page),
        }
        if self.operation in needs:
            name, value = needs[self.operation]
            if value is None:
                raise ValueError(f"{self.operation} needs {name}")
        if self.query is not None:
            _no_nul(self.query, "query")
        if self.region is not None:
            x0, y0, x1, y1 = self.region
            if not all(math.isfinite(value) for value in self.region) or x0 >= x1 or y0 >= y1:
                raise ValueError("region must be finite [x0, y0, x1, y1] with x0 < x1 and y0 < y1")
        return self


ExecutionRequest = Annotated[
    ShellRequest | JobRequest | CellRequest | PdfRequest, Field(discriminator="kind")
]


class ExecutionState(StrEnum):
    """Attempt states (docs/specs/tools.md, "Lifecycle")."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    LOST = "lost"


TERMINAL_STATES: Final = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.TIMED_OUT,
        ExecutionState.CANCELLED,
        ExecutionState.LOST,
    }
)


class ErrorKind(StrEnum):
    """Failure kinds every tool reports (docs/specs/tools.md, "Errors")."""

    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    KERNEL_DIED = "kernel_died"
    EXCEPTION = "exception"
    INTERNAL = "internal"


class ErrorInfo(Contract):
    """A failure, with the exception's name, value and traceback for cell errors."""

    kind: ErrorKind
    message: str
    name: str | None = None
    value: str | None = None
    traceback: tuple[str, ...] = ()


class StreamOutput(Contract):
    """What remains of a stream: its head and tail when it exceeded the limit."""

    name: Literal["stdout", "stderr"]
    text: str
    total_bytes: Annotated[int, Field(ge=0)]
    truncated: bool = False


class RichOutput(Contract):
    """A display or a cell's result, as a MIME bundle (binary types base64-encoded)."""

    type: Literal["display", "result"]
    data: dict[str, str]
    execution_count: int | None = None


class KernelInfo(Contract):
    """A session's kernel; ``generation`` grows by one on every restart."""

    kernel_id: Identifier
    language: Language
    generation: Annotated[int, Field(ge=1)]
    run: Identifier | None = None
    """The run whose own kernel it is; ``None`` for the session's."""
    start_revision: Counter | None = None
    """Revision observed at this kernel's birth, never inferred from a later cell."""
    current_revision: Counter | None = None
    revision_status: Literal["current", "predates", "unknown"] = "unknown"

    @model_validator(mode="after")
    def _revision_status(self) -> Self:
        expected = "unknown"
        if self.start_revision is not None and self.current_revision is not None:
            if self.start_revision > self.current_revision:
                raise ValueError("kernel start revision is ahead of current revision")
            expected = "current" if self.start_revision == self.current_revision else "predates"
        if self.revision_status != expected:
            raise ValueError("kernel revision status does not match its revisions")
        return self


class KernelStatusRequest(Contract):
    """Inspect this session's running kernels without starting or restarting one."""

    context: ExecutionContext


class KernelListing(Contract):
    """A bounded, session-scoped observation of running kernels: the session's, and those
    of its delegated runs, one a language each."""

    environment_id: Identifier
    session_id: Identifier
    current_revision: Counter | None = None
    kernels: tuple[KernelInfo, ...] = Field(default=(), max_length=MAX_SESSION_KERNELS)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len({(kernel.run, kernel.language) for kernel in self.kernels}) != len(self.kernels):
            raise ValueError("kernel listing repeats a run's language")
        if any(kernel.current_revision != self.current_revision for kernel in self.kernels):
            raise ValueError("kernel listing has inconsistent current revisions")
        return self


class KernelObservation(KernelListing):
    """A successful existing-channel observation, timestamped by the worker."""

    observed_at: AwareDatetime


IsolationTier = Annotated[int, Field(ge=0, le=3)]
"""0 (none) to 3 (a microVM per sandbox), as docs/THREAT-MODEL.md defines them (D43)."""
ProbeText = Annotated[str, StringConstraints(max_length=256)]
KernelSharing = Literal["separate", "shared", "unknown"]
SentinelState = Literal["hidden", "visible", "unknown"]


class ProbeFacts(Contract):
    """What a probe read about the machine and the process it ran in (D43).

    The executor reads these inside the sandbox, and the worker reads the same on
    its own host to compare. ``None`` means the probe could not tell.
    """

    kernel_release: ProbeText | None = None
    boot_id: ProbeText | None = None
    """``/proc/sys/kernel/random/boot_id``: the same value means the same running kernel."""
    hostname: ProbeText | None = None
    machine_id: ProbeText | None = None
    seccomp: Literal["disabled", "strict", "filter"] | None = None
    no_new_privs: bool | None = None
    capabilities: ProbeText | None = None
    """The effective capability set (``CapEff``), in hex."""
    uid: Annotated[int, Field(ge=0)] | None = None
    hypervisor: bool | None = None
    """Whether the CPU reports a hypervisor; ``None`` where it lists no flags (arm64)."""
    sys_vendor: ProbeText | None = None
    product_name: ProbeText | None = None


class IsolationEvidence(Contract):
    """What the worker's probe found when it started the executor (docs/WORKER.md).

    Evidence only ever lowers the declared tier. A probe from inside one sandbox
    cannot show that it has a VM of its own, so a declared tier 3 stands when
    nothing contradicts it.
    """

    kernel: KernelSharing
    """Whether the sandbox runs on the worker host's kernel, compared by boot ID."""
    sentinel: SentinelState
    """Whether a random file the worker wrote in its state directory shows at the same path."""
    findings: tuple[str, ...] = ()
    """What limits the tier, in plain words; empty when nothing does."""
    sandbox: ProbeFacts | None = None
    """What the executor read in the sandbox; ``None`` when the probe failed."""
    error: str | None = None
    """Why the probe failed; the tier is then 0."""


class Location(Contract):
    """Where an execution ran: evidence that it ran in the sandbox."""

    pool_id: Identifier
    backend: Literal["local", "openshell"]
    sandbox_id: str | None = None
    hostname: str
    isolation_tier: IsolationTier | None = None
    """The isolation tier in effect (D43): the declared tier, lowered by what the probe
    found. ``None`` when unmeasured."""
    declared_isolation_tier: IsolationTier | None = None
    """The tier the backend's settings declare."""
    isolation_evidence: IsolationEvidence | None = None
    environment_revision: str | None = None
    """The environment's revision when the attempt started, after any wait for a package
    command (CP-12): ``0`` for the packages it was first seen with, one more for each change
    to its installed packages (W3). Until it starts, the revision when it was dispatched."""


class Result(Contract):
    """A terminal attempt's outcome."""

    exit_code: int | None = None
    signal: int | None = None
    status: Literal["ok", "error", "aborted"] | None = None
    execution_count: int | None = None
    stdout: StreamOutput | None = None
    stderr: StreamOutput | None = None
    outputs: tuple[RichOutput, ...] = ()
    error: ErrorInfo | None = None
    kernel: KernelInfo | None = None
    running_kernels: tuple[KernelInfo, ...] = Field(default=(), max_length=MAX_SESSION_KERNELS)
    """This session's running kernels as observed at completion; no live broadcast."""
    state_lost: bool = False
    """The kernel restarted before this cell, so earlier state is gone."""
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    sandbox_started_at: datetime | None = None
    sandbox_ended_at: datetime | None = None
    """When the attempt started and ended, by the sandbox's own clock."""
    written_files: tuple[str, ...] = ()
    """Files in the session workspace or the shared area (``shared/…``) created or changed
    while the attempt ran. An attempt running at the same time in the session may have
    written some of them."""
    written_files_truncated: bool = False
    deleted_files: tuple[str, ...] = Field(default=(), max_length=1000)
    """Files of the session workspace or the shared area that were there when the attempt
    started and are not when it ended (research artifacts, RA-33), as ``written_files``
    names them. Reported only when the worker compared both areas whole; an attempt
    running at the same time, or the shared area's sync, may have deleted some of them."""
    deleted_files_truncated: bool = False
    """The list is incomplete: the comparison was cut short, or more than 1,000 went."""
    environment_revision: str | None = None
    """The environment's revision when the attempt ended. It differs from the location's
    when the installed packages changed while it ran, which it or an attempt running
    beside it did."""
    overlapping_change: bool = False
    """The environment's packages changed while the attempt ran, or a package command was
    still changing them when it ended (Conda provisioning, CP-12). Code it ran may have
    seen the packages before, after or in between; the revisions say who changed what."""
    notices: tuple[str, ...] = ()
    """What the platform did for the environment since the session's previous result, such
    as admitting a host that a connection was refused for (D69)."""


class JobDetails(Contract):
    """Durable job metadata, including its whole-output files in the session workspace."""

    purpose: str
    expected_progress: str
    wake_session: bool = False
    timeout_seconds: Annotated[float, Field(gt=0, le=604800, allow_inf_nan=False)]
    stdout_path: RelativePath
    stderr_path: RelativePath


class Execution(Contract):
    """An execution and its current attempt."""

    execution_id: Identifier
    attempt_id: Identifier
    operation_id: Identifier
    kind: Literal["shell", "cell", "pdf", "job"]
    context: ExecutionContext
    state: ExecutionState
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    location: Location | None = None
    kernel: KernelInfo | None = None
    """The kernel observed at this cell's start; retained even if it is replaced mid-cell."""
    kernel_state_lost: bool = False
    """The start observation found that this cell's previous kernel state was lost."""
    result: Result | None = None
    last_seq: Annotated[int, Field(ge=0)] = 0
    """The sequence number of the latest event."""
    job: JobDetails | None = None
    origin: Identifier | None = None
    """The provisioning operation this execution is a step of (its dry run, install or a
    probe), when the worker ran it for one."""


class JobListing(Contract):
    """A bounded session job listing, oldest first, with a submission cursor."""

    jobs: tuple[Execution, ...] = ()
    truncated: bool = False
    next_after: Identifier | None = None


class JobStatus(Contract):
    """Current job state, bounded live head and tail, and observed process usage."""

    execution: Execution
    stdout: StreamOutput | None = None
    stderr: StreamOutput | None = None
    cpu_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    memory_bytes: Annotated[int, Field(ge=0)] | None = None


class KernelRestartRequest(Contract):
    """Restart the session's kernel of ``language``, or a run's own (``run``)."""

    context: ExecutionContext
    language: Language = "python"
    run: Identifier | None = None
    """The delegated run whose own kernel restarts (PD-37); ``None`` is the session's."""


class SessionCloseRequest(Contract):
    """Stop a session's kernels and jobs in one environment. Files and foreground calls stay.

    With ``run``, stop only that delegated run's own kernels, when the run ends (PD-37);
    the session's kernels and its jobs stay."""

    context: ExecutionContext
    run: Identifier | None = None


class SessionClosed(Contract):
    kernels: tuple[KernelInfo, ...] = ()
    """The kernels that were running and are now stopped."""


class ReadFileRequest(Contract):
    """Read up to ``length`` bytes from ``offset``; larger files are read in ranges."""

    context: ExecutionContext
    path: RelativePath
    offset: Annotated[int, Field(ge=0)] = 0
    length: Annotated[int, Field(ge=0, le=MAX_READ_BYTES)] = MAX_READ_BYTES


class ListFilesRequest(Contract):
    context: ExecutionContext
    path: RelativePath = "."


class FileEntry(Contract):
    name: str
    type: Literal["file", "directory", "symlink", "other"]
    size: Annotated[int, Field(ge=0)]
    modified_at: datetime


class FileListing(Contract):
    path: str
    entries: tuple[FileEntry, ...]
    truncated: bool = False


class FileContent(Contract):
    """A range of a file; ``sha256``, ``size`` and ``modified_at`` describe the whole file."""

    path: str
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    modified_at: datetime | None = None
    offset: Annotated[int, Field(ge=0)]
    content_b64: str
    eof: bool


class WriteFileRequest(Contract):
    """Create or replace a file. Writes to the shared area need a precondition."""

    context: ExecutionContext
    path: RelativePath
    content_b64: str
    expected_sha256: Sha256 | None = None
    create_only: bool = False


class WriteFileResult(Contract):
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]


MAX_STAGED_FILES: Final = 4096
"""Files one staged pack holds at most, as an imported pack does."""
MAX_STAGED_BYTES: Final = 32 * 1024 * 1024
"""Bytes one staged pack holds at most, as an imported pack does."""
StagedPath = Annotated[str, AfterValidator(validate_relative_path)]
SourceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")]


class StagedFile(Contract):
    """A file of a staged skill pack, as the harness expects it (SK-16)."""

    path: StagedPath
    sha256: Sha256
    size: Annotated[int, Field(ge=0, le=MAX_STAGED_BYTES)]
    executable: bool = False


class StageCheckRequest(Contract):
    """Whether a pack's snapshot is staged in the environment as ``files`` say, file by
    file, with nothing more; with ``seal``, once it is, its files and folders are made
    read-only (docs/specs/skills.md, SK-16)."""

    context: ExecutionContext
    source: SourceName
    snapshot: Sha256
    files: tuple[StagedFile, ...] = Field(max_length=MAX_STAGED_FILES)
    seal: bool = False

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if sum(file.size for file in self.files) > MAX_STAGED_BYTES:
            raise ValueError(f"a staged pack holds at most {MAX_STAGED_BYTES} bytes")
        if len({file.path for file in self.files}) != len(self.files):
            raise ValueError("a staged pack names each file once")
        return self


class StageCheck(Contract):
    """Where the snapshot is staged, and whether it is as expected: the paths that differ,
    are missing or are more than expected, at most ``MAX_STAGED_FILES``."""

    location: str
    ready: bool
    sealed: bool = False
    differing: tuple[str, ...] = Field(default=(), max_length=MAX_STAGED_FILES)


class StagedContent(Contract):
    path: StagedPath
    content_b64: str
    executable: bool = False


class StagePutRequest(Contract):
    """Write some of a pack's files where its snapshot is staged; with ``reset``, after
    removing what is there. Content travels in batches within the request size; a check
    with ``seal`` ends the staging."""

    context: ExecutionContext
    source: SourceName
    snapshot: Sha256
    reset: bool = False
    files: tuple[StagedContent, ...] = Field(max_length=MAX_STAGED_FILES)


class StagePutResult(Contract):
    written: Annotated[int, Field(ge=0, le=MAX_STAGED_FILES)]


class PublishRequest(Contract):
    context: ExecutionContext
    path: RelativePath
    media_type: MediaType | None = None


class Capabilities(Contract):
    protocol: int
    languages: tuple[Language, ...]
    location: Location
    features: tuple[Feature, ...] = ()


class WorkerErrorResponse(Contract):
    """The body of every error response from the worker API."""

    error: ErrorInfo
