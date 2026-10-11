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

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from .artifacts import Sha256
from .context import ExecutionContext
from .model import Contract, Identifier

PROTOCOL_VERSION: Final = 1
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


def _no_nul(text: str, what: str) -> None:
    if "\x00" in text:
        raise ValueError(f"{what} must not contain NUL characters")


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

    @model_validator(mode="after")
    def _one_command(self) -> Self:
        if (self.script is None) == (self.argv is None):
            raise ValueError("give exactly one of script and argv")
        if self.script is not None:
            if not self.script.strip():
                raise ValueError("script must not be empty")
            _no_nul(self.script, "script")
        if self.argv is not None:
            if not self.argv or not self.argv[0]:
                raise ValueError("argv must name a program")
            for argument in self.argv:
                _no_nul(argument, "argv")
        return self


class CellRequest(Contract):
    """A cell for the session's kernel of ``language``."""

    kind: Literal["cell"] = "cell"
    operation_id: Identifier
    context: ExecutionContext
    language: Language = "python"
    code: str
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS
    output_limit_bytes: OutputLimit = DEFAULT_OUTPUT_LIMIT


Feature = Literal["pdf", "admission"]
"""Capabilities beyond kernels that an environment can offer. ``pdf`` needs pdf_oxide (D59);
``admission`` means the worker admits hosts ahead of use (D69, ``AdmissionRequest``)."""
FEATURES: Final[tuple[Feature, ...]] = ("pdf",)
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


ExecutionRequest = Annotated[ShellRequest | CellRequest | PdfRequest, Field(discriminator="kind")]


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
    """The environment's revision when the attempt started: ``0`` for the packages it was
    first seen with, one more for each change to its installed packages (W3)."""


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
    environment_revision: str | None = None
    """The environment's revision when the attempt ended. It differs from the location's
    when the installed packages changed while it ran, which it or an attempt running
    beside it did."""
    notices: tuple[str, ...] = ()
    """What the platform did for the environment since the session's previous result, such
    as admitting a host that a connection was refused for (D69)."""


class Execution(Contract):
    """An execution and its current attempt."""

    execution_id: Identifier
    attempt_id: Identifier
    operation_id: Identifier
    kind: Literal["shell", "cell", "pdf"]
    context: ExecutionContext
    state: ExecutionState
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    location: Location | None = None
    result: Result | None = None
    last_seq: Annotated[int, Field(ge=0)] = 0
    """The sequence number of the latest event."""


class KernelRestartRequest(Contract):
    context: ExecutionContext
    language: Language = "python"


class SessionCloseRequest(Contract):
    """Stop a session's kernels in one environment. Its files stay, and so do shell calls."""

    context: ExecutionContext


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
