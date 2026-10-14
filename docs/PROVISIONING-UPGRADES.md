# Explicit Conda upgrades

The provisioning API takes `action: "upgrade"` for explicitly named Conda specs
(part of CP-20). An upgrade uses the same environment queue, package
policy decisions, file-set permission request, AGPL refusal, package
admission, wall time, revision records, installer observations and artifacts, and
provisioning events as an install. API clients and the main agent's
`provision` tool can select the action: `install` or `upgrade`, with an omitted
action meaning install. Unknown or copied action values refuse before dispatch
rather than being discarded. There is no route or grant of its own, and nothing
picks packages to upgrade automatically.

An upgrade request must contain Conda specs and no capability, pip or system
packages. Remove, restore, upgrade-all and unknown actions are rejected, including
copied unknown action values before request encoding or replay. Pip upgrades,
uninstalls and revision restoration are not built. Download approvals are never
used as authority to delete.

Legacy requests still mean install. Their canonical JSON and request digest stay
byte-for-byte unchanged: the `install` action is omitted when serialized, including
when explicitly supplied. An explicit upgrade action is serialized and belongs to
the original operation digest. Reusing an operation ID with another action conflicts;
an exact terminal replay returns the original record without another installer.
Unknown/null actions at the provisioning request API never silently default to install.

After the installer-version observation, an upgrade requires every named
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

The workspace's read-only detail decoder accepts the upgrade field, and the
panel displays the action; older details still decode. The viewer has no package
action or permission control.

Checked with a real SymPy upgrade on OpenShell, with version and function checks in a fresh guest at both revisions, in [Lab36](../.agents/docs/agent-reports/babbage-openshell-lab-36-2026-10-14.md).
