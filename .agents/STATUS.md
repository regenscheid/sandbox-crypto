# Status

Updated: 2026-10-10.

**Phase:** 1 (babbage), milestone M0. **Focus:** infrastructure track P: the policy library, its OpenShell compiler and the policy middleware.

## Recent progress

- Policy library (`packages/policy`): merged in PRs #3 and #5, after five adversarial review rounds.
- OpenShell compiler (D69), PR #6: admission endpoints plus the policy middleware for research sandboxes, and the MCP proxy's policy, with rendered examples in `examples/openshell/`. OpenShell carries the traffic; there is no separate egress proxy.
- Policy middleware service (`packages/policy-middleware`, D70), PR #7: the OpenShell supervisor middleware that decides each request with the library. Verified against a real OpenShell v0.1.3 gateway with the VM driver on babbage ([lab report](docs/agent-reports/babbage-openshell-lab-2026-10-10.md)); the lab was torn down afterwards.
- The lab showed that a sandbox resolves only host names its policy names. The compiler now names every public host the policy mentions; on-demand admission will cover the rest (D69, amended with the user).
- OpenShell v0.1.3 installed on babbage (`~/.local/opt/openshell-0.1.3/bin`), checked against the published checksums, GitHub's digests and the release workflow's build attestations.
- Plan drift fixed in PR #4; conventions, uv workspace and CI (lint, types, tests, license check, generated-code check); plan snapshot tags pushed.

## Next

- The package cache service (D71): five ecosystems, in progress; then a babbage lab with real clients through OpenShell.
- `allow_credentialed`: let a read-only rule time out to an alert even for credentialed requests (agreed with the user).
- Orchestrator: the middleware's policy store and bindings, escalation grants, and on-demand admission of hosts.
- Validate the MCP proxy's compiled policy on a real gateway.
- W0 behavior specifications.

## Blockers and open questions

- Confirm D68 (policy evaluation details), the details of D69 (its direction, OpenShell plus a policy middleware, is confirmed), D70 (the middleware service) and PSF-2.0 and MIT-0 in tier 1.
- OpenShell's VM driver README calls the driver experimental at v0.1.3, while the support matrix lists it as supported. babbage's planned tier 3 isolation depends on it (D43). It worked throughout the lab.
- babbage enforces Ubuntu's AppArmor restriction on unprivileged user namespaces. Shells started from Claude Desktop run under its `claude-desktop` profile and are exempt, so qualification checks must run from a plain terminal or `systemd-run --user`.
- crypto-analysis-skills: confirm the copyright holder before merging its MIT LICENSE pull request.
