# Plan changes

Dated log of changes to the plan, decisions and conventions ([conventions](CONVENTIONS.md)). Newest first.

## 2026-10-10

- **Policy evaluation details (D68, proposed).** Writing the evaluator in `packages/policy` turned D62's prose into precise rules ([policy](../docs/POLICY.md)). Three gaps needed a decision: the example's `large-egress` alert could never fire under most-specific-wins, so threshold alerts move to a new `monitors` section; `*search*` allowed crypto-research-mcp's `firecrawl_search_feedback`, which submits feedback, so tool patterns now resolve exact names first and otherwise take the most restrictive list; and `on_timeout: allow` "for alert-class rules" had no meaning, so the policy-wide value must be `deny` and only read-only rules may time out to an alert. New safety rules: wildcards and defaults never grant non-public destinations, ambiguous requests are denied, and the hard floor also covers loopback, unspecified and Kubernetes API addresses.
- **Example policy.** `large-egress` moved to `monitors`. The crypto-research-mcp tool lists were rewritten against the server's 78 tools, classified from their own descriptions; a test fixture pins each tool's outcome. Per-call billed searches and cache-filling document reads stay allowed by choice; feedback, crawl, agent, scrape, parse, interact, monitor-changing and refresh tools ask first; portal configuration is denied.
- **Tier 1 licenses add PSF-2.0** ([licensing](../docs/LICENSING.md)). pydantic requires typing-extensions (PSF-2.0, permissive and GPL-compatible). CI now checks the licenses of all installed distributions.
- **Conventions adopted** ([CONVENTIONS.md](CONVENTIONS.md)), carried over from the paused `impl/p-policy-engine` branch. Pyright strict applies to `packages/*/src` as the conventions state; tests use standard mode.
- **Plan snapshots tagged.** The signed tags `plan-03-pre-review` (`cb89015`) and `plan-04-reviewed` (`9e4ae25`) are on GitHub, as the [analysis archive](docs/README.md) already described.
