import sys
from sandbox_science.policy import PoolCeiling, load_policy
from sandbox_science.policy.openshell import McpProxyTarget, SandboxFilesystem, compile_mcp_proxy
target = McpProxyTarget(
    binaries=("/opt/sandbox-science/bin/python*",),
    filesystem=SandboxFilesystem(read_only=("/opt/sandbox-science",)),
)
compiled = compile_mcp_proxy(load_policy(sys.argv[1]), target, ceiling=PoolCeiling.unrestricted())
for finding in compiled.findings:
    print("finding:", finding.severity.value, finding.subject, finding.message)
open(sys.argv[2], "w").write(compiled.policy.to_yaml())
print("compiled")
