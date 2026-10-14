# Explicit Conda upgrades

This partial CP-20 slice adds `action: "upgrade"` to the existing provisioning API
for explicitly named Conda specs. It uses the same environment queue, package
policy decisions, file-set permission request, AGPL refusal, original package
admission, wall time, revision records, installer observations/artifacts and
provisioning events as an install. API/client callers and the existing MAIN
`provision` tool can explicitly select the action; its offered enum is `install`
or `upgrade`, with omitted action preserving install. Unknown or copied action
values refuse before dispatch rather than being discarded. The delegated child
tool table still has no provisioning entry.
There is no new route, grant or automatic selection of packages to upgrade.

An upgrade request must contain Conda specs and no capability, pip or system
packages. Remove, restore, upgrade-all and unknown actions are rejected, including
copied unknown action values before request encoding/replay. Pip upgrade,
uninstall and revision restoration remain open. In particular, existing download
approvals are not repurposed as deletion authority.

Legacy requests still mean install. Their canonical JSON and request digest stay
byte-for-byte unchanged: the `install` action is omitted when serialized, including
when explicitly supplied. An explicit upgrade action is serialized and belongs to
the original operation digest. Reusing an operation ID with another action conflicts;
an exact terminal replay returns the original record without another installer.
Unknown/null actions at the provisioning request API never silently default to install.

After the original installer-version observation, an upgrade requires every named
target in the current retained Conda lockfile. Missing/unknown history or a revision
change while reading it refuses before the update dry run. The actual file-read
task remains owned through repeated caller cancellation. This is a retained
observation, not authentication of arbitrary research code or an OS write barrier.
Existing package turn/guest admission fences still govern the actual command.

Resolution runs `micromamba update --dry-run --json` on only the supplied specs.
Its linked files pass the existing bounded parser and policy/AGPL checks. If needed,
the existing one-file-set permission flow waits for a real operator answer. The
actual `micromamba update` uses `MAMBA_PINNED_PACKAGES` with the exact decided
channel/subdir/name/version/build pins. There is no fallback to `install` or
`--all`. This delegates update solving under explicit constraints to micromamba;
it does not promise that every dependency's version increases, or introduce a
policy-driven solver that automatically picks an older allowed candidate.

An unchanged solve uses the existing `already_satisfied` outcome with retained
version/import checks; that is not a functional capability witness. Changed updates
record their before/after lockfiles, package differences and command lineage.
Installer step labels remain `version-conda`, `resolve-conda`, `install-conda`;
these identify phases. The request action and revision's command distinguish the
update. Sanitized log artifacts remain qualified partial diagnostics.

A denied upgrade downloads no new files. A failed/cancelled update follows the
existing CP-13 classification: known unchanged stays failed, changed/uncertain
state holds `needs_recovery`; automatic rollback is not implemented. Later work is
refused by that hold. No installer is repeated to fill missing logs or outcomes.
Disk limits remain the separate CP-14 backend prerequisite.

The existing read-only detail decoder accepts the explicit upgrade field and the
panel displays its action. Legacy detail remains compatible. No package action,
permission control or additional fetch owner is added to the viewer.

Validation is isolated: historical JSON bytes, explicit replay/conflict, malformed
and copied action negatives, command/pin identity, installed/current target checks,
actual file-thread cancellation drainage, policy escalation denial and AGPL refusal,
changed-update cancellation/recovery, MAIN offered-schema/handler agreement and
read-only decoder compatibility. Fake
package managers and disposable prefixes exercise the production API and policy
path. Fresh combined gates pass11,464 Python/43 skips/two warnings/407.67s,828 web/42 post-build and all17 required checks/builds.

[OpenShell Lab36](../.agents/docs/agent-reports/babbage-openshell-lab-36-2026-10-14.md) passed the actual scalar-first SymPy1.13.3→1.14.0 install/update, direct production MAIN handler, fresh guest version/function checks at both revisions, six artifacts, four events and known replay without new work. Runtime5c7/helper645 passed EXIT0/57.266s; complete owned teardown was independently cleared at2026-10-14T04:49:51.949614Z. This is metadata/command/functional observation, not installed-file restoration or model-loop proof. Fresh publication CI and delivery remain separate.
