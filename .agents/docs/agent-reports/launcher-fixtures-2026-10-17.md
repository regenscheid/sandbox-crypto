# Confined launcher fixture qualification

The provider and lab-config security tests failed because the initial test cache
was under /home/andy, which is itself a Git checkout. Refusing a synthetic CLI or
lab config there is correct. No security guard or assertion was relaxed. This gate
therefore used /var/tmp/sandbox-science-test-cache/launcher-fixtures-fixed, outside
that ancestor checkout, as an explicit exception to the usual home-cache placement.

Updated only MCP/sandbox lifecycle fixtures: assert exact control host and VM
startup, credential upload and runtime invocation; exact VM/control/proxy/worker
cleanup order; no host harness loop; MCP configuration in the trusted host and
absent from VM settings. Another project's and manually made sandboxes remain
untouched. The fatal-part fixture now kills the control host; its old host-harness
process no longer exists. It still verifies research sandboxes remain running.

Focused four modules test_access_provider, test_config, test_mcp, test_sandboxes:
114passed in20.54s, four workers, live/integration environment unset. Strict
pyright on changed tests:0errors; scoped ruff and diff check passed. Root performed
an independent read of the fixture changes with no remaining medium+ findings.
No production changes or personal service access.

Raw log: /home/andy/.cache/pt/launcher-fixtures-fixed.log
SHA256: d677ebbef1c8db6d8ed69256d563541ee7c64200d18bb5f1039d4557bedac699
Initial outside-checkout gate110pass/4old-fixturefail:
/home/andy/.cache/pt/launcher-fixtures.log
SHA256: 69b1de1a9dbe3b6a197a13c75319efdd4666e5e05cde87588430362c9dec319e
