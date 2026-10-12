"""Lab 15 caller: DeepWiki through the proxy, from the host. Usage: caller.py URL TOKEN"""
import asyncio, json, sys
import aiohttp
URL, TOKEN = sys.argv[1], sys.argv[2]
async def main() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "Sandbox-Science-Session": "s1"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as http:
        session = None
        for i, (method, params) in enumerate([
            ("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "lab15", "version": "0"}}),
            ("notifications/initialized", None),
            ("tools/call", {"name": "read_wiki_structure", "arguments": {"repoName": "modelcontextprotocol/python-sdk"}}),
        ]):
            body = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                body["params"] = params
            if method != "notifications/initialized":
                body["id"] = i
            sent = dict(headers)
            if session:
                sent.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"})
            async with http.post(f"{URL}/mcp/deepwiki", json=body, headers=sent) as r:
                text = await r.text()
                session = r.headers.get("Mcp-Session-Id") or session
                print(f"{method:28s} {r.status} {text[:160]!r}")
asyncio.run(main())
