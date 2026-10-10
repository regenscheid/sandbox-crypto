# Status

Updated: 2026-10-10.

**Phase:** 1 (babbage), milestone M0. **Focus:** infrastructure track P, starting with the project policy library.

## Recent progress

- Policy library (`packages/policy`) merged in PR #3: schema, safe YAML loading, request classification, hard floor, pool ceiling, evaluation and monitors, with tests and CI.
- Two adversarial review rounds found 20 issues in the merged library, including three in the shipped example policy. The fixes are in the follow-up PR; merge it before anything uses the library.
- Plan drift fixed in PR #4.
- OpenShell compiler (D69): admission endpoints plus the policy middleware for research sandboxes, and the MCP proxy's policy, with rendered examples in `examples/openshell/`. The separate egress proxy is dropped: OpenShell carries the traffic and the middleware decides each request.
- Development conventions, uv workspace and CI (lint, types, tests, license check).
- Plan snapshot tags pushed.

## Next

- Build the policy middleware service.
- Install OpenShell v0.1.3 on babbage, load the compiled example policies and verify the middleware path; so far the output is checked only against the documented schema.
- W0 behavior specifications.
- Install OpenShell on babbage and run its kernel qualification.

## Blockers and open questions

- Confirm D68 (policy evaluation details), D69 (OpenShell fence design) and PSF-2.0 in tier 1.
- OpenShell's VM driver README calls the driver experimental at v0.1.3, while the support matrix lists it as supported. babbage's planned tier 3 isolation depends on it (D43).
- babbage enforces Ubuntu's AppArmor restriction on unprivileged user namespaces. Shells started from Claude Desktop run under its `claude-desktop` profile and are exempt, so qualification checks must run from a plain terminal or `systemd-run --user`.
- crypto-analysis-skills: confirm the copyright holder before merging its MIT LICENSE pull request.
