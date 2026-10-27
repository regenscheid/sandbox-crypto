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

PROTOCOL_VERSION: Final = 16
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
        Annotated[str, StringConstraints(pattern=r"^[^\x00-\x20\x7f;]{1,4096}$")],
    ],
    Field(max_length=MAX_BINDINGS),
]
"""Environment variables an execution gets beside the environment's own: the skill packs'
bindings, each naming a path in a staged pack, without whitespace, control characters or
``;`` (SK-16). The harness sets only those the project allows (SK-31), and an executor
refuses one the environment sets itself."""


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


IsolationTier = Annotated[int, Field(ge=0, le=3)]
"""0 (none) to 3 (a microVM per sandbox), as docs/THREAT-MODEL.md defines them (D43)."""
MAX_JOB_INPUTS: Final = 64
"""Session workspace files or directories one job sandbox's request copies in (JS-5)."""
JobProfileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
"""A job profile the worker's settings define, such as ``large`` (D93, JS-8)."""


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
    binding_inventory: Sha256 | None = None

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
    binding_inventory: Sha256 | None = None
    profile: JobProfileName | None = None
    """A job profile granted to the project: the job runs in a new sandbox of its own, of
    that size, made from the session environment's image and policy (D93). ``None``, the
    default, runs it in the session environment, as before protocol 13."""
    inputs: tuple[RelativePath, ...] = Field(default=(), max_length=MAX_JOB_INPUTS)
    """Session workspace files or directories copied to the same paths in the job's
    sandbox before it runs; only with ``profile``."""
    min_isolation_tier: IsolationTier | None = None
    """The harness's own minimum tier, never the agent's: a job sandbox below it, or below
    the session environment's, fails before its command; only with ``profile``."""

    @model_validator(mode="after")
    def _one_command(self) -> Self:
        if self.profile is None and (self.inputs or self.min_isolation_tier is not None):
            raise ValueError("inputs and min_isolation_tier need a job profile")
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("inputs name each path once")
        if any(path == "." or path == ".jobs" or path.startswith(".jobs/") for path in self.inputs):
            raise ValueError("inputs name files of the session workspace, not it or .jobs")
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
    binding_inventory: Sha256 | None = None

    @model_validator(mode="after")
    def _checked(self) -> Self:
        _check_bindings(self.bindings)
        return self


Feature = Literal["pdf", "admission", "provisioning", "jobs", "job_sandboxes", "child_environments"]
"""Capabilities beyond kernels that an environment can offer. ``pdf`` needs pdf_oxide (D59);
``admission`` means the worker admits hosts ahead of use (D69, ``AdmissionRequest``);
``provisioning`` that it installs capabilities and packages on request (``ProvisionRequest``,
Conda provisioning); ``job_sandboxes`` that a job may name one of the capabilities'
``job_profiles`` (D93); ``child_environments`` that a delegation may run in an
environment of its own, made from this one (docs/specs/child-environments.md, CE-1)."""
FEATURES: Final[tuple[Feature, ...]] = ("pdf", "jobs")
"""The features an executor may report; the worker adds ``admission`` itself."""
PdfOperation = Literal["info", "pages", "search", "render", "draw"]
"""``draw`` makes a recorded figure a PNG for ``view_image`` (docs/specs/figures.md, FG-35):
a PDF page or region, an SVG, or a PNG or JPEG scaled down; it writes no file."""
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


JobSandboxPhase = Literal["creating", "preparing", "running", "collecting", "released", "retained"]
"""Where a job's own sandbox is (JS-4): placed, then packages and inputs, the command, the
copy back; then deleted, or stopped and kept until a deadline after a failure (JS-25)."""
CollectionStatus = Literal["pending", "complete", "incomplete"]
MAX_MANIFEST_FILES: Final = 1000
"""Files a job's collection copies back at most (JS-17), and inputs its manifest lists."""
DEFAULT_COLLECT_MAX_BYTES: Final = 4 * 1024**3
"""Bytes a job's collection copies back by default (owner, 2026-10-22)."""
DEFAULT_STAGE_MAX_BYTES: Final = 2 * 1024**3
"""Bytes of inputs a job sandbox copies in by default (JS-13)."""
JobFailureReason = Literal[
    "capacity_unavailable",
    "isolation_insufficient",
    "package_replay_failed",
    "environment_mismatch",
    "staging_failed",
    "sandbox_lost",
    "sandbox_unavailable",
    "worker_restarted",
]
"""Why a job sandbox ended its job short of its command's own outcome."""


class JobSandbox(Contract):
    """A job's own sandbox, as the job's record shows it (JS-2, JS-27)."""

    environment_id: Identifier
    """The job environment: no agent tool can name it (JS-1)."""
    profile: JobProfileName
    phase: JobSandboxPhase
    cpu_millicores: Annotated[int, Field(ge=1)]
    memory_mib: Annotated[int, Field(ge=1)]
    """What the profile reserves (its Kubernetes limits)."""
    retained_until: datetime | None = None
    collection: CollectionStatus = "pending"
    manifest_sha256: Sha256 | None = None
    """The digest of its immutable result manifest, once the job has ended."""


class JobSandboxRelease(Contract):
    """The user's request to release a retained job sandbox now (JS-24): the job whose
    sandbox it is is the route's; ``context`` is the session that owns the job."""

    context: ExecutionContext
    operation_id: Identifier
    """The receipt: the same request again answers the first result."""


class JobSandboxReleased(Contract):
    """A job sandbox the worker deleted by its identity (JS-25), at the user's request."""

    execution_id: Identifier
    phase: Literal["released"] = "released"


class JobDetails(Contract):
    """Durable job metadata, including its whole-output files in the session workspace."""

    purpose: str
    expected_progress: str
    wake_session: bool = False
    timeout_seconds: Annotated[float, Field(gt=0, le=604800, allow_inf_nan=False)]
    stdout_path: RelativePath
    stderr_path: RelativePath
    profile: JobProfileName | None = None
    """The job profile its sandbox has; ``None`` for a job in the session environment."""
    sandbox: JobSandbox | None = None


class ManifestFile(Contract):
    """A file a job sandbox's manifest names: an input or a collected file."""

    path: RelativePath
    """Its session workspace path: for a collected file, under ``.jobs/<ID>/files/``."""
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256


class UncollectedFile(Contract):
    path: Annotated[str, StringConstraints(max_length=MAX_PATH_BYTES)]
    """As the job sandbox named it."""
    reason: Annotated[str, StringConstraints(max_length=300)]


class ReplayedPackages(Contract):
    """The session's package changes the job sandbox installed before its command (D93)."""

    conda: tuple[Annotated[str, StringConstraints(max_length=256)], ...] = Field(
        default=(), max_length=2000
    )
    pip: tuple[Annotated[str, StringConstraints(max_length=256)], ...] = Field(
        default=(), max_length=2000
    )
    operations: tuple[Identifier, ...] = Field(default=(), max_length=256)
    """The provisioning operations that installed them, in order."""
    session_revision: Annotated[int, Field(ge=0)] | None = None
    session_lockfile: Sha256 | None = None
    job_lockfile: Sha256 | None = None
    """The job environment's lockfile after the replay, when it had one."""


class JobResultManifest(Contract):
    """What a job sandbox ran and returned (JS-16): stored once, with the job's end, and
    never rewritten. ``.jobs/<ID>/manifest.json`` in the session is an untrusted copy."""

    job_id: Identifier
    attempt_id: Identifier
    profile: JobProfileName
    driver_config_sha256: Sha256
    image: Annotated[str, StringConstraints(max_length=1024)]
    environment_id: Identifier
    sandbox_id: Annotated[str, StringConstraints(max_length=256)] | None = None
    isolation_tier: IsolationTier | None = None
    isolation_evidence: IsolationEvidence | None = None
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime
    outcome: ExecutionState
    reason: JobFailureReason | None = None
    exit_code: int | None = None
    signal: int | None = None
    packages: ReplayedPackages = Field(default_factory=ReplayedPackages)
    inputs: tuple[ManifestFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)
    collected: tuple[ManifestFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES + 2)
    not_collected: tuple[UncollectedFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)
    collection: CollectionStatus


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
    manifest: JobResultManifest | None = None
    """A job sandbox's result manifest, once it has ended; its digest is the execution's
    ``job.sandbox.manifest_sha256``."""


class KernelRestartRequest(Contract):
    """Restart the session's kernel of ``language``, or a run's own (``run``)."""

    context: ExecutionContext
    language: Language = "python"
    run: Identifier | None = None
    """The delegated run whose own kernel restarts (PD-37); ``None`` is the session's."""
    only_if_absent: bool = False
    """For recovery, refuse to replace a kernel that became live after observation."""


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


MAX_FILE_WRITE_BYTES: Final = 100 * 1024 * 1024
MAX_FILE_WRITE_BODY: Final = 4 * ((MAX_FILE_WRITE_BYTES + 2) // 3) + 64 * 1024
"""A 100 MiB write plus its bounded JSON context and path metadata."""


class WriteFileRequest(Contract):
    """Create or replace a file. Writes to the shared area need a precondition."""

    context: ExecutionContext
    path: RelativePath
    content_b64: str = Field(max_length=4 * ((MAX_FILE_WRITE_BYTES + 2) // 3))
    expected_sha256: Sha256 | None = None
    create_only: bool = False

    @model_validator(mode="after")
    def bounded_content(self) -> Self:
        """The final base64 quartet must not allow bytes past the 100 MiB bound."""
        padding = (
            2 if self.content_b64.endswith("==") else 1 if self.content_b64.endswith("=") else 0
        )
        size = len(self.content_b64) // 4 * 3 - padding
        if size > MAX_FILE_WRITE_BYTES:
            raise ValueError("file write exceeds 100 MiB")
        return self


class WriteFileResult(Contract):
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]


MAX_EDIT_BYTES: Final = MAX_READ_BYTES
"""The largest file ``edit_file`` changes, before and after the edit: the read limit."""
MAX_EDIT_STRING: Final = 1024 * 1024
"""Characters of ``old_string`` and of ``new_string``, each."""


class EditFileRequest(Contract):
    """Replace ``old_string`` with ``new_string`` in a UTF-8 text file, atomically.

    ``old_string`` must occur exactly once unless ``replace_all``. The file must exist;
    in the shared area the edit needs ``expected_sha256``, the hash ``read_file`` showed.
    """

    context: ExecutionContext
    path: RelativePath
    old_string: Annotated[str, Field(min_length=1, max_length=MAX_EDIT_STRING)]
    new_string: Annotated[str, Field(max_length=MAX_EDIT_STRING)]
    replace_all: bool = False
    expected_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def changes_something(self) -> Self:
        if self.old_string == self.new_string:
            raise ValueError("old_string and new_string are the same")
        return self


class EditFileResult(Contract):
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    replacements: Annotated[int, Field(ge=1)]


MAX_SEARCH_PATTERN: Final = 1000
"""Characters of a glob or a grep pattern."""
MAX_GLOB_RESULTS: Final = 200
MAX_GREP_RESULTS: Final = 500
DEFAULT_GREP_RESULTS: Final = 100
MAX_GREP_CONTEXT: Final = 10
MAX_GREP_LINE_CHARS: Final = 400
"""Characters of one line ``grep`` shows in ``content`` mode; a longer one is cut."""
MAX_SKIPPED_NAMES: Final = 16


def validate_glob(value: str) -> str:
    """Check a path pattern: relative, without empty, ``.`` or ``..`` segments.

    ``*`` and ``?`` match within one segment, ``[…]`` one character of a set, and a
    segment ``**`` any number of directories. Raises ValueError.
    """
    if not value or "\x00" in value or len(value) > MAX_SEARCH_PATTERN:
        raise ValueError("invalid pattern")
    if value.startswith("/"):
        raise ValueError(f"pattern {value!r} must be relative to the path")
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"pattern {value!r} has an empty, '.' or '..' segment")
    return value


GlobPattern = Annotated[str, AfterValidator(validate_glob)]
SearchPattern = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SEARCH_PATTERN)]
SearchStop = Literal["results", "entries", "depth", "bytes", "time", "reply"]
"""Why a search stopped early: its result limit, the entries or directory levels it walks,
the bytes it reads, its time, or the size of its reply."""


class GlobRequest(Contract):
    """The paths below ``path`` that match ``pattern``, newest first."""

    context: ExecutionContext
    pattern: GlobPattern
    path: RelativePath = "."
    limit: Annotated[int, Field(ge=1, le=MAX_GLOB_RESULTS)] = MAX_GLOB_RESULTS


class GlobEntry(Contract):
    """A match, by its path relative to the session workspace (``shared/…`` in the
    shared area); a symlink is reported as a link and not followed."""

    path: str
    type: Literal["file", "directory", "symlink", "other"]
    size: Annotated[int, Field(ge=0)]
    modified_at: datetime


class GlobListing(Contract):
    path: str
    pattern: str
    entries: tuple[GlobEntry, ...] = Field(max_length=MAX_GLOB_RESULTS)
    truncated: bool = False
    """More entries matched than are listed, or the walk stopped early (``stopped``)."""
    stopped: SearchStop | None = None
    scanned: Annotated[int, Field(ge=0)]
    skipped: tuple[str, ...] = Field(default=(), max_length=MAX_SKIPPED_NAMES)
    """Directories the walk did not enter: the research prefix and the executor's state."""


GrepMode = Literal["files", "count", "content"]


class GrepRequest(Contract):
    """Search the text files below ``path``, or the file it names, for a Python regular
    expression. Binary files, symlinks and the directories ``GlobListing.skipped`` names
    are left out; ``glob`` narrows the files, by name or, with a ``/``, by path."""

    context: ExecutionContext
    pattern: SearchPattern
    path: RelativePath = "."
    glob: GlobPattern | None = None
    mode: GrepMode = "files"
    case_insensitive: bool = False
    context_lines: Annotated[int, Field(ge=0, le=MAX_GREP_CONTEXT)] = 0
    max_results: Annotated[int, Field(ge=1, le=MAX_GREP_RESULTS)] = DEFAULT_GREP_RESULTS


class GrepCount(Contract):
    path: str
    count: Annotated[int, Field(ge=1)]


class GrepResult(Contract):
    """``files`` lists the files that match, ``count`` each one's matching lines, and
    ``content`` the lines, as ``path:line: text``, with context lines as
    ``path-line- text`` and ``--`` between groups."""

    path: str
    pattern: str
    mode: GrepMode
    files: tuple[str, ...] = Field(default=(), max_length=MAX_GREP_RESULTS)
    counts: tuple[GrepCount, ...] = Field(default=(), max_length=MAX_GREP_RESULTS)
    lines: tuple[str, ...] = Field(
        default=(), max_length=MAX_GREP_RESULTS * (2 * MAX_GREP_CONTEXT + 2)
    )
    truncated: bool = False
    stopped: SearchStop | None = None
    scanned_files: Annotated[int, Field(ge=0)]
    scanned_bytes: Annotated[int, Field(ge=0)]
    skipped_binary: Annotated[int, Field(ge=0)] = 0
    skipped_large: Annotated[int, Field(ge=0)] = 0
    skipped_unreadable: Annotated[int, Field(ge=0)] = 0
    skipped: tuple[str, ...] = Field(default=(), max_length=MAX_SKIPPED_NAMES)


MAX_STAGED_FILES: Final = 4096
"""Files one staged pack holds at most, as an imported pack does."""
MAX_STAGED_BYTES: Final = 32 * 1024 * 1024
"""Bytes one staged pack holds at most, as an imported pack does."""
StagedPath = Annotated[str, AfterValidator(validate_relative_path)]
CommandName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
SourceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")]


class StagedFile(Contract):
    """A file of a staged skill pack, as the harness expects it (SK-16)."""

    path: StagedPath
    sha256: Sha256
    size: Annotated[int, Field(ge=0, le=MAX_STAGED_BYTES)]
    executable: bool = False


class BindingRestriction(Contract):
    """One matching denial's installed-package extraction provenance."""

    name: str = Field(min_length=1, max_length=128)
    package: str = Field(min_length=1, max_length=512)
    source: str = Field(min_length=1, max_length=4096)
    sha256: Sha256
    rule: str = Field(min_length=1, max_length=128)


class BindingInventory(Contract):
    """Current static restriction projection; missing inventory grants no bindings."""

    state: Literal["ready", "unavailable"] = "unavailable"
    digest: Sha256 | None = None
    fingerprint: str = Field(default="", max_length=256)
    generator: int = Field(default=1, ge=1)
    epoch: int = Field(default=0, ge=0)
    reserved: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=8192
    )
    namespaces: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=8192
    )
    coverage: str = Field(default="inventory unavailable", max_length=2000)
    provenance: tuple[BindingRestriction, ...] = Field(default=(), max_length=8192)

    @model_validator(mode="after")
    def _ready(self) -> Self:
        if self.state == "ready" and (self.digest is None or not self.fingerprint):
            raise ValueError("ready inventory requires digest and package fingerprint")
        return self


class StageCheckRequest(Contract):
    """Whether a pack's snapshot is staged in the environment as ``files`` say, file by
    file, with nothing more; with ``seal``, once it is, its files and folders are made
    read-only (docs/specs/skills.md, SK-16)."""

    context: ExecutionContext
    source: SourceName
    snapshot: Sha256
    files: tuple[StagedFile, ...] = Field(max_length=MAX_STAGED_FILES)
    seal: bool = False
    commands: tuple[CommandName, ...] = Field(default=(), max_length=256)
    """Names to look for on the environment's ``PATH``: a pack's namespace that is a
    command's name is that command's (SK-31)."""
    binding_names: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=8192
    )
    """Candidate variables and namespaces for the current inventory projection."""

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
    commands: tuple[str, ...] = Field(default=(), max_length=256)
    """Those of the request's ``commands`` the environment's ``PATH`` has."""
    inventory: BindingInventory = Field(default_factory=BindingInventory)


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


class JobProfileOffer(Contract):
    """A job profile the environment's project may run a job sandbox with (JS-5)."""

    name: JobProfileName
    description: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    cpu_millicores: Annotated[int, Field(ge=1)]
    memory_mib: Annotated[int, Field(ge=1)]
    wall_time_max_seconds: Annotated[float, Field(gt=0, le=604800, allow_inf_nan=False)]


class Capabilities(Contract):
    protocol: int
    languages: tuple[Language, ...]
    location: Location
    features: tuple[Feature, ...] = ()
    kernel_reason: Annotated[str, StringConstraints(max_length=300)] | None = None
    """Why the environment offers no Python kernel, when it does not and its executor said why."""
    job_profiles: tuple[JobProfileOffer, ...] = Field(default=(), max_length=32)
    """With the ``job_sandboxes`` feature: the profiles granted to the project (D93)."""


class WorkerErrorResponse(Contract):
    """The body of every error response from the worker API."""

    error: ErrorInfo
