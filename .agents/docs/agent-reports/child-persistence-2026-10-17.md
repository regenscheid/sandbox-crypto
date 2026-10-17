# Child persistence failures stay visible

A delegated attempt previously ignored the status of its final save. A valid model
answer could therefore become a successful structured handback even though saving
its record or delivery acknowledgements failed. It now reports `persistence_error`
as a failed attempt, preserves its partial report, and withholds the structured
successful result. Already committed evidence remains unchanged; this grants no
permission to replay effects.

Validation: 124 delegation, lifecycle, depth, restart, failure and harness tests
passed in 23.66 seconds. New end-to-end cases inject a failed child record save and
a failed child acknowledgement separately, showing the parent's handback is not
successful in either case and the latter retains the already saved child record.
Ruff, pyright and repository hygiene checks pass.
Evidence outside Git: `research-foundations-child-persistence.log`, SHA-256
`f1e08ea0166fd748051315a1f19b25033d4563987ff0984219bcf144548258ef`.
No live service was used. Independent review and integration remain pending.
