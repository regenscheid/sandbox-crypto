# Requested reasoning effort

The launcher selects effort by caller role, independently of model alias:

```toml
[model.reasoning_effort]
main = "max"
delegated = "max"
reviewer = "medium"
watcher = "low"
fold = "medium"
overview = "medium"
skills_metadata = "low"
```

When `reviewer` is omitted, the launcher and native run service request `medium` for
the consistency reviewer. Main and delegated roles retain provider defaults when
omitted. An explicit deployment value still wins.

These are requested values, not a declaration that a gateway or model supports every
example value. Names are bounded lowercase wire values; the configured value is sent
verbatim. A provider refusal remains an error: the tool never substitutes a lower level.
Omitted roles omit the effort field and retain provider defaults, even when MAIN has an
explicit setting. Embeddings do not have a reasoning-effort role.

The native harness takes repeated `--reasoning-effort ROLE=VALUE` arguments. The
launcher sends the same settings to the confined runtime. Responses uses
`reasoning.effort`; chat uses `reasoning_effort`. Messages rejects explicit effort
until an adapter-specific mapping has been implemented and qualified. The separate
workspace math-repair caller uses the reviewer's configured API and requested effort.

New work pins alias, adapter and requested effort for every enabled caller. Saved run
records and input blueprints retain those profiles; paid intents atomically append a
profile notice before dispatch, so an unknown response does not erase the request.
Gateway-returned reasoning settings remain provider observations, distinct from these
requested pins. A normal Resume preserves saved profiles; legacy work preserves omitted
effort. An explicit authenticated [Resume effort decision](specs/resume-effort-override.md)
may change effort for the new attempt without rewriting its original blueprint.

The workspace's **Project settings → Agent models and reasoning effort** editor lets
the user choose the main/orchestrator, delegated-agent and consistency-reviewer alias
and effort independently. Model choices are limited to the deployment's trusted agent
catalog (the aliases configured for those three roles). The browser cannot choose an
adapter, endpoint or credential. A save applies to the next root research tree. That
tree's children and reviews inherit its immutable profile snapshot, and ordinary
Resume keeps it; an edit never changes a running tree. The separate math-repair caller
continues to use its deployment reviewer setting.

Fold narratives and overview narratives use independent profiles. Native model-switch
refitting preserves the former alias while selecting the narrative effort. The confined
runtime still refuses former aliases outside its configured/scoped model capabilities;
this existing unsupported path does not gain authority through an effort setting. Configuration changes alone do not
change a saved continuation's profile. An adapter/alias mismatch fails rather than
silently changing the pinned caller. Existing budgets, response bounds and turn limits
still apply.
