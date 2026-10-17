# Durable Stop and interrupted child cleanup

Explicit Stop intent is durable before signalling cancellation and applies through
the saved delegation ancestry. Child admission checks this intent atomically.
Stopping one branch leaves siblings and other sessions alone. Recovery after a
crash preserves the request even if the run saved no final record. Saved outcomes
remain immutable; an unresponsive child still ends `interrupted`, rather than
claiming its effects stopped.

Parent failure, a lost lease and service interruption now save child partial records
as `interrupted`, rather than conflating cleanup with explicit cancellation. Both
root Stop and `cancel_delegation` retain `cancelled` through two-level trees.

Validation: 137 delegation, lifecycle, restart and run-service tests passed in
34.13 seconds, including reopen-before-cleanup, subtree scoping, refused child
admission, saved outcomes, real service reaping and lease loss. Ruff and pyright pass.
No live services were used. This prerequisite does not implement recursive Resume;
independent review and integration remain pending. Evidence outside Git:
`research-foundations-stop-reasons-final.log`, SHA-256
`305a29cd5c6428e95a0b7252fce3bb6cc7984a5df724629ca74b53a4ec02970d`.
