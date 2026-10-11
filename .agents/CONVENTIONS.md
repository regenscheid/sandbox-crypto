# Development conventions

Applies to all code and documentation in this repository. Change this file through a pull request like any other convention change, and log the change in [PLAN-CHANGES.md](PLAN-CHANGES.md).

## Where things live

| Location | Contents |
| --- | --- |
| `docs/` | Architecture and design documentation, the [decision register](../docs/DECISIONS.md), and policy/connector specifications. Describes how the system **is** (or is decided to be). Kept current in the same pull request as the code it describes |
| `.agents/STATUS.md` | Current phase, focus, recent progress, blockers and open questions |
| `.agents/BACKLOG.md` | Working checklist, organized by work package |
| `.agents/PLAN-CHANGES.md` | Dated log of every change to the plan, decisions or conventions, with links |
| `.agents/CONVENTIONS.md` | This file |
| `.agents/critical-review.md`, `.agents/proposed-changes.md` | The 2026-10-10 plan review (historical) |
| `.agents/docs/` | Agent source investigations (historical evidence) |
| `.agents/upstream/` | Descriptions of bugs found in upstream projects, one file each, ready to file. Filing is the user's call |
| `packages/<component>/` | One uv workspace member per deployable or shared component |
| `examples/` | Illustrative configuration (policy, deployment profiles); tests load these to keep them valid |
| `scripts/` | Repository tooling, such as the license check |
| `.github/workflows/` | CI |

## Repository layout

The repository is a [uv](https://docs.astral.sh/uv/) workspace. Each member lives in `packages/<name>/` with its own `pyproject.toml`, `src/` and `tests/`. Code uses the `sandbox_science` namespace package: a member named `policy` provides `sandbox_science.policy`. There is no `sandbox_science/__init__.py`.

Planned members (created when work starts on them):

| Member | Import | Deployable unit |
| --- | --- | --- |
| `policy` | `sandbox_science.policy` | Library: policy model, validation, evaluation (shared) |
| `contracts` | `sandbox_science.contracts` | Library: shared contracts (`ExecutionContext`, `ArtifactRef`, `Event`, …) |
| `worker` | `sandbox_science.worker` | Worker service (trusted, per pool) |
| `orchestrator` | `sandbox_science.orchestrator` | Control application and harness |
| `mcp-proxy` | `sandbox_science.mcp_proxy` | MCP proxy (runs in its own OpenShell sandbox) |
| `policy-middleware` | `sandbox_science.policy_middleware` | OpenShell supervisor middleware that decides each request with the policy library (D69) |
| `package-cache` | `sandbox_science.package_cache` | Pull-through package cache for research sandboxes (D71) |
| `connector-auth` | `sandbox_science.connector_auth` | Connector auth service (OAuth for MCP) |
| `web` | — | Browser front end (separate toolchain, D60) |

Libraries must not import deployable members. Deployable members may import libraries, never each other.

## Language and tooling

- **Python ≥ 3.12** for all server-side code (D60). Develop and test on 3.13.
- **uv** manages environments and the lockfile (`uv.lock`, committed). Do not use bare `pip install`.
- **ruff** for linting and formatting (configuration in the root `pyproject.toml`).
- **pyright** in strict mode for `packages/*/src`.
- **pytest** for tests.
- **pydantic v2** for every external schema (policy files, API messages, events). Models use `extra="forbid"` and are frozen unless mutation is essential.

Run before every push:

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv run python scripts/check_licenses.py
uv run python scripts/generate_protos.py --check
```

CI runs the same commands.

## Licensing

- Every source file starts with `# SPDX-License-Identifier: MIT`.
- New dependencies must follow the tiers in [LICENSING.md](../docs/LICENSING.md). Tier 1 platform code may depend only on MIT, BSD, ISC, Apache-2.0, PSF-2.0, PostgreSQL, public-domain or unmodified MPL-2.0 packages.
- Never import or link GPL, LGPL or AGPL code from platform code. GPL research tools are reached only through process or protocol boundaries.
- Add each new runtime dependency to the LICENSING.md component table in the same pull request.

## Security rules for code

- Fail closed. Unknown inputs, ambiguous paths, unmatched servers and unsupported features are denied or reported, never silently allowed.
- No secrets in the repository, tests or fixtures. Tests use obviously synthetic values (`test-secret-…`).
- No code path may run research code in the orchestrator or any trusted service (D11, hard floor).
- Agent-supplied text is data. It never changes policy, grants permission or selects a code path that widens access.
- Every security-relevant rule has a test, including at least one negative case.

## Code style

- Type everything; no `Any` in public signatures unless unavoidable and commented.
- Prefer small pure functions and immutable data. Side effects live at the edges (I/O, network, subprocess).
- Errors: raise specific exception types defined in the member; never swallow exceptions silently.
- Comments explain *why*, not *what*. Docstrings on public classes and functions.
- Keep modules focused; split a module when it holds unrelated concepts.

## Testing

- Unit tests live in `packages/<member>/tests/`, named `test_<module>.py`.
- Tests that load `examples/` files keep the examples valid.
- Security behavior gets explicit negative tests (denials, rejected inputs).
- Integration tests that need OpenShell, KVM or a cluster are marked `@pytest.mark.integration` and skipped unless `SS_INTEGRATION=1`. They run on babbage, not in CI.

## Git and pull requests

- Branch from `main`: `impl/<work-package>-<topic>` for code (for example `impl/p-policy-engine`), `docs/<topic>` for documentation-only changes, `fix/<topic>` for fixes.
- Commit messages: `<type>(<scope>): <summary>` where type is `feat`, `fix`, `docs`, `test`, `refactor`, `chore` or `ci`. Body explains why. End with the attribution trailers in use for the session.
- One coherent change per pull request. The pull request description lists the work-package items it completes and any plan changes.
- Merge with merge commits. Never rewrite published history.
- Pull requests are merged by the user, or by an agent only when the user explicitly says so.

## Keeping documentation and tracking current

Every pull request that changes behavior or the plan also:

1. Updates the affected `docs/` pages so they describe the system as built or decided.
2. Records new or changed decisions in [docs/DECISIONS.md](../docs/DECISIONS.md) (with status) and adds a dated entry to [PLAN-CHANGES.md](PLAN-CHANGES.md).
3. Ticks or adds items in [BACKLOG.md](BACKLOG.md).
4. Refreshes [STATUS.md](STATUS.md) (focus, recent progress, blockers).

A decision is changed by editing its row and marking the status (for example `Confirmed (amended YYYY-MM-DD)`), never by silently rewriting history; the PLAN-CHANGES entry gives the reason.

## Definition of done

A backlog item is done when its code is merged with tests, the CI checks pass, affected `docs/` are updated, and the tracking files reflect it. Items that need hardware this repository's CI cannot provide (OpenShell on babbage, the Kubernetes test cluster) are done only when their integration evidence is recorded in the item.
