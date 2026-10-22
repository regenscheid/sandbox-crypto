# Tool and hook capability assembly

A run's ordinary capabilities bind their tool entries, required ordered hooks and
owned cleanup in one place. The harness constructs scoped domain controllers;
`capabilities.py` binds those controllers and selects the main, delegated task or
delegated check offer. `Toolbox` retains dispatch, effect enforcement, argument
handling, worker operations and deferred connector loading.

Each definition supplies its reserved names and its position in the tool and hook
orders. These orders differ and remain explicit. A hook factory creates fresh
adapter state for each `turn_hooks()` call; controllers that already are hooks
retain their identity. The catalog reserves names even when a role or worker
feature does not offer the capability. Worker specifications remain with their
handlers and contribute their names to the same reservation calculation.

Main agents may receive reviews, plans, questions and connectors. Delegated tasks
receive only capabilities constructed from their admitted scope; delegated checks
additionally receive only read effects from document, ledger and memory tools.
Trusted role factories enforce this selection. The existing `Toolbox` keyword
constructor remains an adapter for direct callers, including its role denials.
No agent text selects a factory or broadens a scope.

A bundle owning a closeable registers its cleanup with the harness exit stack as
soon as the resource is made. Setup failure therefore closes resources already
created. The stack retains reverse registration order, after hook leases close.
Viewers, review controllers and artifact publishers retain their existing close
operations and registration order. External stores remain caller owned.

The refactor changes no schema, prompt, confined protocol, effect classification,
result truncation, hook ordering or disposal order. Skills retain their existing
prompt and pin APIs. Run lifecycle hooks (inputs, work continuity, messages,
events, context and folding) remain in the harness: they are not tool capabilities.

Public characterization covers offered tool/schema order, effect classes, hook
order and freshness, role denials, check filtering, reserved-name collisions and
partial setup disposal. Existing loop, delegation, connector, plan, kernel and
harness tests cover execution through these offers. No live model or worker
service is needed for assembly characterization.
