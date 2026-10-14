# Original orchestrator prompts and planning design

The original [prompt catalog](../../../docs/ORCHESTRATOR-PROMPTS.md) audits
MAIN, background watcher, reviewer, repair and tool inputs. It specifies planning,
execution, delegated tracks, steering, interruption, recovery and compaction
without copying upstream prompts. D86 records the user's requested planning phase
and visible progress; detailed draft choices retain their stated status.

The implemented correction is deliberately small: MAIN describes worker execution
and control bookkeeping accurately, background-job guidance distinguishes worker
restart from actual daemon/VM loss, and a historical review-trigger docstring is
corrected. Tool grants, schemas, dispatch, policy and closing behavior are unchanged.
Two scripted actual-loop tests inspect rendered system input and offered tools,
including control-side document reads and untrusted tool content.

Signed design author `e3ba76663c882be978c6fc48e2c2fde3feda1735` and signed
correction author `fd7ecea38a951d85a0a8d34cd9033dfac92a2846` are integrated at
signed root source `186261a530176dbdde5ec5223ea616881f893da5`, directly based on
actual PR #99 main `afb049a`. All five correction paths match the author exactly.
Subsequent root changes are documentation/evidence only.

| Validation | Result |
| --- | --- |
| Author affected tests | 30 passed |
| Independent actual-input tests | 2 passed; AST checks confirm literal/docstring-only runtime changes |
| Root full Python on `186261a` | 10,034 passed, 43 opt-in skips, two existing serializer warnings; Python 3.13.15 |
| Locked dependencies, Ruff/format, strict Pyright, licenses, protocols | Passed |
| Web/math/builds | Exact unchanged PR #99 paths; reuse 614 web, 24 math and both production builds |
| Independent source/design review | Clear; source facts distinguished from draft capabilities |

Exact local evidence (archived outside git, sha256 `1a24b54c9d47`) retains
the review and fresh root gate logs with byte lengths and SHA-256. The reused web
evidence is retained in Lab27 (archived outside git, sha256 `27545cfd2f97`);
it is not a new browser acceptance for prompts. No live external model, service,
browser, converter or VM was started for this slice.

Durable plans/progress, their MAIN tools and phase prompts, live GUI updates,
delegation, approval routing, context compaction and the complete prompt catalog
are separate implementation work. This change enables none of those tools and
adds no mandatory approval gate. PR #100 merged at `c14d1ea412591aa8793b91da0a071a7d9416286a` after all four fresh exact-head checks passed on independently reviewed signed `02a73c9` (run `38327914825`).
