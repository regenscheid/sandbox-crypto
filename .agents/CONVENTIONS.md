# Development conventions

Applies to all code and documentation in this repository. Change this file through a pull request like any other convention change, and log the change in [PLAN-CHANGES.md](PLAN-CHANGES.md).

## Where things live

| Location | Contents |
| --- | --- |
| `docs/` | Architecture and design documentation, the [decision register](../docs/DECISIONS.md), and policy/connector specifications. Describes how the system **is** (or is decided to be). Kept current in the same pull request as the code it describes |
| `.agents/STATUS.md` | Current phase and focus, what works today, the refactoring plan's phase, blockers and open questions. Replaced, not appended to; under 10 KB |
| `.agents/BACKLOG.md` | Working checklist, organized by work package |
| `.agents/PLAN-CHANGES.md` | Dated list of changes to the plan, decisions or conventions, one or two lines each, with links. Progress goes in PR descriptions and lab reports |
| `.agents/CONVENTIONS.md` | This file |
| `.agents/critical-review.md`, `.agents/proposed-changes.md` | The 2026-10-10 plan review (historical) |
| `.agents/docs/` | Agent source investigations, lab reports and acceptance reports (historical evidence) |
| `.agents/upstream/` | Descriptions of bugs found in upstream projects, one file each, ready to file. Filing is the user's call |
| `.agents/refactor-plan/` | The revision and refactoring plan of 2026-10-14 and the six reviews behind it |
| `.agents/handoff.md` | Where things stand, how work is done now and what is next, for the next agent. Replaced, not appended to; under 8 KB |
| `.agents/labs/` | Scripts for labs on babbage against a real OpenShell gateway, and how to run and report them |
| `packages/<component>/` | One uv workspace member per deployable or shared component |
| `web/<app>/` | Browser apps, each an npm project of its own: `web/workspace/` is the research workspace GUI (D80, [workspace](../docs/WORKSPACE.md)). Not uv members, since uv, pyright and pytest search `packages/` |
| `skills/builtin/` | The built-in skills, one `skills/builtin/<name>/SKILL.md` each: Markdown with standard front matter, shipped with the harness, imported and pinned at a run's start as the source `builtin` ([skills](../docs/SKILLS.md#the-built-in-pack)). Edit one in the pull request that changes the tool or rule it describes; `test_builtin_skills.py` checks the tools it names |
| `examples/` | Illustrative configuration (policy, deployment profiles); tests load these to keep them valid |
| `images/` | Recipes for the base profile images (`minimal`, `crypto-base`) and the `orchestrator` image: Containerfile, Conda lockfiles, build, lock and test scripts ([images](../docs/IMAGES.md)) |
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
| `policy-middleware` | `sandbox_science.policy_middleware` | OpenShell supervisor middleware that decides each request with the policy library (D69) |
| `package-cache` | `sandbox_science.package_cache` | Pull-through package cache for research sandboxes (D71) |
| `event-store` | `sandbox_science.event_store` | Library: append-only event store with a hash chain (D74, shared) |
| `connector-auth` | `sandbox_science.connector_auth` | Connector auth service (OAuth for MCP) |
| `workspace` | `sandbox_science.workspace` | The research workspace's web server: the GUI's files and its API (D80) |
| `launcher` | `sandbox_science.launcher` | `sandbox-science up`: starts, supervises and stops a local Sandbox Science from one config file, running the other members as commands, never importing them |
| `testing` | `sandbox_science.testing` | Development only: support for this repository's tests (`FakeClock`, `eventually()`, the worker API client `WorkerApi`, the leftover-process check in `processes`, and a fake MCP server with TLS for the harness's tests in `mcp`). A dev dependency; no production member imports it |

The browser front end itself is not a member: it lives in `web/workspace/`, with Node's toolchain (D60, D80).

Libraries must not import deployable members. Deployable members may import libraries, never each other. One temporary W4 exception, until the control application has an API of its own (W5): `workspace` accesses harness records through the orchestrator's scoped project and control stores. This includes reads and explicit authenticated USER changes to documents, annotations and findings, with the existing same-origin write guard and capability checks. It does not grant the workspace worker or agent authority.

## Language and tooling

- **Python ≥ 3.12** for all server-side code (D60). Develop and test on 3.13.
- **uv** manages environments and the lockfile (`uv.lock`, committed). Do not use bare `pip install`.
- **ruff** for linting and formatting (configuration in the root `pyproject.toml`).
- **pyright** in strict mode for `packages/*/src`.
- **pytest** for tests.
- **pydantic v2** for every external schema (policy files, API messages, events). Models use `extra="forbid"` and are frozen unless mutation is essential.
- **Browser apps** (D80) are Svelte 5 with TypeScript in strict mode, built by Vite, tested with vitest, checked with svelte-check and tsc, and formatted with prettier (`npm run format`; settings in the app's `package.json`). npm packages are untrusted downloads: exact versions, a committed `package-lock.json`, installs only with `--ignore-scripts` (the app's `.npmrc` says so too), `node_modules` inside the app's directory, and no `npx` for anything that is not a declared dependency.

Run before every push:

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -n auto
uv run python scripts/tiers.py check
uv run python scripts/check_licenses.py
uv run python scripts/check_licenses.py --image trusted-app
uv run python scripts/check_licenses.py --image trusted-worker
uv run python scripts/generate_protos.py --check
uv run python scripts/check_repo_hygiene.py
```

and, in `web/workspace/`:

```bash
python3 ../../scripts/check_licenses.py --npm package-lock.json
npm ci --ignore-scripts
python3 ../../scripts/check_licenses.py --npm package-lock.json --installed
npm run format:check
npm run check
npm test
npm run build
npm run build:math
```

The two `--image` checks compare each trusted image's `licenses.txt` with the licenses of its locked Python wheels (they fetch a wheel the synced environment lacks, such as `boto3`'s, so they need the network or `uv`'s cache). CI runs the same checks, and the math checker's tests (`uv run pytest -q packages/orchestrator/tests/test_mathcheck.py`) after `npm run build:math`. On a pull request it runs them on Python 3.13, with the fast tier, the general slow tier and two process-staging shards beside the web app's checks. Each staging module has its own runner and one xdist worker, so its executor processes do not compete with another staging module; the general jobs retain work-level balancing. `scripts/ci_pytest.sh` keeps each job's output in a file, printed when pytest ends or its diagnostic deadline stops it: 18 minutes for the general tiers and 10 minutes for staging. A push to main also runs the non-staging suite serially on Python 3.12 and 3.13, with each staging module in its own single-worker job on both versions. A nightly run does the same, then repeats the general slow tier ten times (five jobs of two repetitions) and each isolated staging module ten times (two jobs of five) to measure how often their tests flake; each repeat job's summary gives the flake rate it saw (`scripts/flake_report.py`). The nightly runs the workflow file on `main`, so a CI fix reaches it only when merged there. A weekly `audit` workflow runs `pip-audit` on `uv.lock`'s production set and `npm audit --audit-level=high` on the web app's lockfile; it is not a pull request check, and Dependabot proposes weekly, grouped updates for GitHub Actions and the web app's npm packages only (`.github/dependabot.yml`).

## Licensing

- Every source file starts with `# SPDX-License-Identifier: MIT`, or the same in the file type's own comment syntax (`//`, `/* */`, `<!-- -->`). JSON files cannot carry it. A file ported from an Apache-2.0 source says `Apache-2.0` instead and keeps that source's notices ([LICENSING.md](../docs/LICENSING.md)).
- New dependencies must follow the tiers in [LICENSING.md](../docs/LICENSING.md). Tier 1 platform code may depend only on MIT, MIT-0, BSD, ISC, Apache-2.0, PSF-2.0, PostgreSQL, public-domain or unmodified MPL-2.0 packages.
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
- Contracts follow the [compatibility policy](../docs/COMPATIBILITY.md). Until M3 the orchestrator, the workspace and the worker are released in lockstep: any change to what crosses the worker API raises `PROTOCOL_VERSION`, and each side refuses another version. A new request field gets a default that keeps the old behavior, since an operation's identity is the digest of its non-default fields (`contracts/canonical.py`); a change to a default's meaning or a field's JSON form keeps accepting the old digest for one release.
- Contracts hold shapes, per-field bounds and the cross-field rules that define a value (such as exactly one of two fields), not re-derivations of what the server computed. They build on the primitives in `contracts/model.py` (`StrictContract`, `Counter`, `Revision`, `Sha256`, pydantic's `AwareDatetime`, and `check_page` for offset pages), and name their limits as constants the worker imports instead of repeating literals.

## Testing

- Unit tests live in `packages/<member>/tests/`, named `test_<module>.py`.
- Tests that load `examples/` files keep the examples valid.
- Security behavior gets explicit negative tests (denials, rejected inputs).
- Integration tests that need OpenShell, KVM, a cluster or the public registries are marked `@pytest.mark.integration`. The root `conftest.py` skips them unless `SS_INTEGRATION=1`. They run on babbage, not in CI.
- Tests that call the deployment's real model gateway, for the labs, are marked `@pytest.mark.live`. The root `conftest.py` skips them unless `SS_LIVE_GATEWAY` is set, so CI never runs one. A live test reports what it measures and gates nothing unless its fixture says so ([specs](../docs/specs/README.md)).
- Async tests and fixtures run under pytest-asyncio's auto mode, so they need no `asyncio` mark.
- Every test has a 300 s limit (pytest-timeout), so a hang fails instead of stalling CI. `--timeout=0` turns it off for long manual runs. In CI a fast-tier test has 120 s; a test past its limit has every thread's stack printed, then its worker is ended and xdist names the test.
- A component with a deadline, a lease or a polling interval reads time through the `Clock` protocol in `contracts` and takes its timeouts and intervals as constructor parameters, defaulting to `SYSTEM_CLOCK` and the production values. Code that predates this rule is converted as the refactoring plan reaches it. Tests pass `sandbox_science.testing.FakeClock` and short timeouts instead of patching module constants, and wait for a condition with `eventually()` instead of sleeping. A timeout test uses a peer that never answers, so the timeout is the only possible outcome.
- Tests run in parallel (pytest-xdist), so a test owns its files (`tmp_path`) and does not depend on other tests or their order. `pytest-repeat` (`--count 50`) with `-n 8` checks a test for flakiness; add `-o tmp_path_retention_policy=failed`, since many repetitions fill `/tmp` otherwise.
- **Tiers.** A test module that starts real processes for its tests (kernels, executors, sandbox daemons, package managers) is marked slow, with `pytestmark = pytest.mark.slow` beside any other marks. The fast tier is everything else, and is meant to stay near two minutes on four CPUs. Marks are strict (`--strict-markers`), so a misspelt one fails. `scripts/tiers.py` holds each tier's marker expression; `scripts/tiers.py check` fails when a test is in no tier or in two, or when a `test_*.py` file yields no test, such as one outside `testpaths`.
- **No processes left behind.** After each test module, the root `conftest.py` looks for processes still running from the run's temporary directory: executors and sandbox daemons whose `--root` is there, and kernels and commands working in a directory there, and commands of the test's own session that name a path there as an argument (such as `harness serve --state-dir`). It counts only this user's processes started after the test process, never the test process's ancestors, and only live directories, since other agents' test runs share the machine. It gives them ten seconds, then kills them and fails the module's last test. A test that kills an executor on purpose gives its commands a way to end with it.
- pyright reports unnecessary `# type: ignore` and `# pyright: ignore` comments as errors, and ruff reports unused `noqa` comments. Tests may run subprocesses, build SQL and use synthetic secrets and temporary paths without inline suppressions.

## Git and pull requests

- Branch from `main`: `impl/<work-package>-<topic>` for code (for example `impl/p-policy-engine`), `docs/<topic>` for documentation-only changes, `fix/<topic>` for fixes.
- Commit messages: `<type>(<scope>): <summary>` where type is `feat`, `fix`, `docs`, `test`, `refactor`, `chore` or `ci`. Body explains why. No attribution lines in commits or pull request descriptions. Commits are signed through the owner's SSH configuration (plain `git commit`).
- One coherent change per pull request. The pull request description lists the work-package items it completes and any plan changes.
- Merge with merge commits. Never rewrite published history. The one exception so far: on 2026-10-14 the owner had raw lab evidence removed from all history (all commit SHAs before then changed; the pre-rewrite repository is kept privately on babbage).
- **Private names stay private.** Product code (`packages/*/src`) keeps pyright strict: a file comment may say only `# pyright: strict`, a line ignores only named rules (`# pyright: ignore[rule]`; `# type: ignore` is off), and a line that must use another module's private name says why in words after the comment. `scripts/check_repo_hygiene.py` checks these and that `pyproject.toml` keeps `strict = ["packages/*/src"]` and `enableTypeIgnoreComments = false`.
- **Lab evidence stays out of git.** Lab reports are Markdown in `.agents/docs/agent-reports/`, short, with results and findings. Raw evidence (JSON records, logs, screenshots) is kept outside the repository, on babbage, and each report names its files by SHA-256. `scripts/check_repo_hygiene.py` fails CI on evidence files under `.agents/docs` and on any tracked file over 1 MiB. Evidence never records home-directory paths, `/tmp` contents or copies of files already in git.
- Pull requests are merged by the user, or by an agent when the user says so. The owner has asked agents to merge pull requests once CI is green and review is clear.

## Keeping documentation and tracking current

Every pull request that changes behavior or the plan also:

1. Updates the affected `docs/` pages so they describe the system as built or decided: how things are, not the history of how they got there.
2. Records new or changed decisions in [docs/DECISIONS.md](../docs/DECISIONS.md) (with status), and adds one or two dated lines to [PLAN-CHANGES.md](PLAN-CHANGES.md) for a change to the plan, a decision or a convention.
3. Ticks or adds items in [BACKLOG.md](BACKLOG.md).
4. Puts its summary, test results and evidence hashes in the pull request description.

[STATUS.md](STATUS.md) and [handoff.md](handoff.md) are replaced, not appended to, by the agent coordinating the work, and stay under 10 KB and 8 KB. Other pull requests leave them, and PLAN-CHANGES, alone unless their task says otherwise. Keep a space between a number and the word beside it ("passed 11,464").

A decision is changed by editing its row and marking the status (for example `Confirmed (amended YYYY-MM-DD)`), never by silently rewriting history; the PLAN-CHANGES line gives the reason.

## Working rules

From the [refactoring plan](refactor-plan/PLAN.md#working-rules-from-now-on) of 2026-10-14. They apply to every change.

1. **No production module without a production caller.** Prerequisites wait in a branch until the feature that wires them is ready.
2. **Small pull requests:** at most about 800 changed lines of product code, excluding tests and generated files; pure deletions are exempt. Split bigger changes into steps that each keep behavior.
3. **Calibrate to the [threat model](../docs/THREAT-MODEL.md).** A defense against something it does not cover needs a decision record first. Reviews flag over-hardening as findings.
4. **Characterization tests before a refactor,** through public APIs. No new tests that read private attributes.
5. **Evidence stays out of git** (see *Git and pull requests*).
6. **STATUS and the handoff replace content instead of appending it,** with size caps. History goes in pull request descriptions and lab reports.
7. **No new subsystem in `orchestrator/loop.py`, `worker/manager.py` or the workspace's `create_app`.** Features plug in through seams.
8. **Specs before code:** nothing is built on a spec whose open questions affect it.

## How work is done

The owner's standing instructions:
- **Advance the plan autonomously,** installing software as needed, with several agents in parallel. Merge pull requests once CI is green and review is clear.
- **Decision requests in plain language:** the practical effect, an example and a recommendation. The owner is a cryptography researcher; explain platform terms.
- **Bug reports for upstream projects** go in `.agents/upstream/`, one file each; filing them is the owner's call.
- Never enter real credentials; test tokens are visibly synthetic (`test-secret-…`). The owner's real keys (the model gateway's and Cloudflare Access's) are files in `~/.config/sandbox-science/secrets/`. Agents never open, read, list, copy or print them; a program gets the path of one only when the task says so.
- No sudo and no change to system or security settings. No persistent configuration (such as systemd units in `~/.config`) without permission; transient `systemd-run --user` units are fine.
- Downloaded files are untrusted: put them in their own directory, check checksums, and run Python on them with `-I`. npm installs use `--ignore-scripts`.
- Docker is shared with the owner's other projects: only create, tag or remove `sandbox-science/*` images, and never prune.
- Tear labs down completely. Never run destructive commands on real data, even as a test.

The process:
- **One task per agent,** in a worktree of its own, from a written brief: what to do and what is out of scope, the files and findings to read, these conventions and the working rules, and "open a PR, make CI green, report in your final message".
- **Review by risk** (the owner, 2026-10-14, to save tokens). Security paths, storage migrations, concurrency and new features get a full adversarial review by a separate agent that does not modify the repository. It reports findings by severity, each with a file and line, a scenario and a fix; the author fixes them and the reviewer checks the fixes. Repeat until nothing critical, high or medium remains. Code moves, docs, test-only changes and fixes that implement a reviewer's suggestion get a light check.
- **Labs on babbage** check what only OpenShell shows. Run one before merging when a change's behavior depends on the runtime ([labs](labs/README.md)).
- **Merging:** `gh pr merge N --merge`, then fast-forward the main checkout to `origin/main` and run `uv sync --locked`, then remove clean merged worktrees. New decision numbers take the next free D-number on main at merge time.

## Definition of done

A backlog item is done when its code is merged with tests, the CI checks pass, affected `docs/` are updated, and the tracking files reflect it. Items that need hardware this repository's CI cannot provide (OpenShell on babbage, the Kubernetes test cluster) are done only when their integration evidence is recorded in the item.
