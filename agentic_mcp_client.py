# =============================================================================
# MCP CLIENT (2026-08-08) — the agentic plane can CALL external MCP servers
# (including our own gateway on :8087). Minimal stdlib JSON-RPC over HTTP.
# Config: `mcp_servers` in the config table = JSON list of
#   {"name": "...", "url": "http://host:port/mcp", "api_key": "..."}
# Chat: /mcp · /mcp <server> · /mcp <server> <tool> [json args] · /mcp add name|url|key
# =============================================================================
def _mcp_servers():
    raw = _cfg_get("mcp_servers") or ""
    try:
        servers = json.loads(raw) if raw else []
        return servers if isinstance(servers, list) else []
    except Exception:
        return []


def _mcp_save_servers(servers):
    _cfg_set("mcp_servers", json.dumps(servers, indent=1))


def _mcp_jsonrpc(url, payload, api_key=None, timeout=30):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    # strip SSE framing if the server responds event-stream
    if body.lstrip().startswith("event:") or "\ndata:" in body:
        lines = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        body = "\n".join(lines)
    return json.loads(body)


def _mcp_handshake(server):
    url = (server.get("url") or "").rstrip("/")
    key = server.get("api_key") or ""
    init = _mcp_jsonrpc(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "appvault-agentic", "version": "1.0"}}}, key)
    try:
        _mcp_jsonrpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, key, timeout=5)
    except Exception:
        pass
    return init


def _mcp_tools(server):
    url = (server.get("url") or "").rstrip("/")
    key = server.get("api_key") or ""
    try:
        _mcp_handshake(server)
        res = _mcp_jsonrpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, key)
        return (res.get("result") or {}).get("tools", [])
    except Exception as e:
        return {"error": str(e)[:200]}


def _mcp_call(server, tool, args):
    url = (server.get("url") or "").rstrip("/")
    key = server.get("api_key") or ""
    try:
        _mcp_handshake(server)
        res = _mcp_jsonrpc(url, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}}}, key, timeout=120)
        result = res.get("result") or {}
        content = result.get("content") or []
        out = "\n".join(str(c.get("text") or c.get("content") or "")
                        for c in content if isinstance(c, dict))
        if not out:
            out = json.dumps(result)[:600]
        if result.get("isError"):
            return f"⚠️ MCP error from {tool}: {str(out)[:400]}"
        return str(out)[:2000]
    except Exception as e:
        return f"⚠️ MCP call failed: {str(e)[:200]}"


def _slash_mcp(args):
    parts = [p.strip() for p in args.split("|")]
    servers = _mcp_servers()
    if parts[0] == "add" and len(parts) >= 3:
        name, url = parts[1], parts[2]
        key = parts[3] if len(parts) > 3 else ""
        if not url.startswith("http"):
            return "⚠️ URL must start with http(s)://"
        servers = [s for s in servers if s.get("name") != name]
        servers.append({"name": name, "url": url, "api_key": key})
        _mcp_save_servers(servers)
        return f"✅ MCP server '{name}' added ({url}). Try: /mcp {name}"
    if parts[0] == "del" and len(parts) >= 2:
        _mcp_save_servers([s for s in servers if s.get("name") != parts[1]])
        return f"🗑 MCP server '{parts[1]}' removed"
    if not servers:
        return ("No MCP servers configured. Add one:\n"
                "`/mcp add my-server|http://host:port/mcp|optional-api-key`\n"
                "Try our own gateway: `/mcp add appvault|http://localhost:8087/mcp`")
    if len(parts) == 1:
        lines = [f"- **{s['name']}** · {s.get('url')}" for s in servers]
        return ("**MCP servers:**\n" + "\n".join(lines) +
                "\n\n`/mcp <server>` lists its tools · `/mcp <server> <tool> {json}` calls one")
    # find server by name (fuzzy)
    srv = next((s for s in servers if s.get("name", "").lower() == parts[0].lower()), None)
    if not srv:
        return f"⚠️ No server named '{parts[0]}' — /mcp to list"
    if len(parts) == 2:
        tools = _mcp_tools(srv)
        if isinstance(tools, dict) and tools.get("error"):
            return f"⚠️ {tools['error']}"
        if not tools:
            return f"Server '{parts[0]}' exposes no tools."
        lines = [f"- **{t.get('name')}** — {t.get('description', '')[:90]}" for t in tools[:25]]
        return f"**Tools on '{parts[0]}' ({len(tools)}):**\n" + "\n".join(lines)
    # call: /mcp <server> <tool> [json args]
    tool = parts[1]
    try:
        args = json.loads(parts[2]) if len(parts) > 2 and parts[2] else {}
    except Exception:
        return "⚠️ Arguments must be valid JSON (e.g. {\"message\": \"hi\"})"
    _audit("chat", "mcp.call", f"{srv['name']}:{tool}")
    return _mcp_call(srv, tool, args)
