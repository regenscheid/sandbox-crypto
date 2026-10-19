# Auxiliary paid-call accounting

Observed `research_model_calls.usage` is authoritative for reviewer, watcher, fold,
skill metadata and embedding charges before the result returns to its consumer.
Main and delegated responses continue to use their atomic assistant usage journal.

Auxiliary legacy records keep their existing fields and local budget behavior. Each
new observed call carries its opaque intent identity to that writer. The writer
atomically records an exact legacy table/row link in `research_observations`, scoped
to the original attempt and work. It never matches charges by amount, time or model.
Canonical known usage suppresses only its explicitly linked legacy copy, regardless
of the reporting time window. Unlinked legacy records remain counted. Fold usage
JSON carries the same optional identity, including through saved/event fallbacks.

A crash before materialization retains the canonical charge. A failure to record the
link rolls back the legacy write. Repeating an identity cannot create another charged
copy. All three public accounting queries use the same precedence: known costs,
model-information costs and unpriced token-bearing calls. Unknown paid outcomes
remain explicitly unresolved; this increment does not invent their usage.
