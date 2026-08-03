#!/usr/bin/env python3
"""
AppVault MCP Gateway — one MCP server per VPS exposing installed apps as LLM tools.

Phase 1 (read-only):
- Dynamic tool registration from catalog `mcp` manifests (each app can ship
  `mcp.tools[]` with name/description/inputSchema/handler + handler config).
- Handlers:
    http        -> call the app's REST API via its mapped host port
    docker_exec -> run a read-only command inside the app's container
    docker      -> container status / logs / inspect
    sql         -> read-only query against the app's DB container
                   (wrapped in BEGIN TRANSACTION READ ONLY ... ROLLBACK)
- Write-scoped tools ("write": true) are DENIED by default; set MCP_ALLOW_WRITES=1
  to opt in. Phase 2 replaces this with the approval gate + credential vault UI.
- API-key auth on the streamable-http transport (Authorization: Bearer or X-Api-Key),
  matching the agent's existing X-Api-Key convention.

Wiring: agent.py STARTUP block starts start_gateway() in a daemon thread (:8087).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import inspect
from collections import deque
from typing import Literal, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from mcp.server.fastmcp import FastMCP

_OUT_LIMIT = 4000
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------- handlers

def _http_call(td, kwargs, deps):
    app_id = td["_app"]
    host_port = deps["get_host_port"](f"app-{app_id}")
    if not host_port:
        return {"ok": False, "error": f"{app_id} not running (no mapped host port)"}
    path = td.get("path", "/")
    used = {k for k in kwargs if "{" + k + "}" in path}
    for k in used:
        path = path.replace("{" + k + "}", str(kwargs[k]))
    rest = {k: v for k, v in kwargs.items() if k not in used}
    url = f"http://127.0.0.1:{host_port}{path}"
    headers = {}
    cred = deps["vault"](app_id)
    if cred.get("header") and cred.get("value"):
        headers[cred["header"]] = str(cred["value"])
    try:
        if td.get("method", "GET").upper() == "POST" and rest:
            req = Request(url, data=json.dumps(rest).encode(), headers=headers, method="POST")
        else:
            if rest:
                url += ("&" if "?" in url else "?") + urlencode(rest)
            req = Request(url, headers=headers)
        with urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return {"ok": True, "data": json.loads(body)}
        except Exception:
            return {"ok": True, "data": body[:_OUT_LIMIT]}
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:_OUT_LIMIT]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:_OUT_LIMIT]}


def _interp(template, kwargs):
    """Replace {param} placeholders in a manifest template with caller values."""
    out = template
    for k, v in kwargs.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _docker_exec(td, kwargs, deps):
    cname = f"app-{td['_app']}"
    cmd = [_interp(c, kwargs) if isinstance(c, str) else c for c in td.get("cmd", [])]
    ok, out = deps["docker_fn"]("exec", cname, *cmd)
    return {"ok": ok, "output": (out or "")[:_OUT_LIMIT]}


def _docker_op(td, kwargs, deps):
    cname = f"app-{td['_app']}"
    op = td.get("op", "status")
    if op == "status":
        ok, out = deps["docker_fn"]("ps", "-a", "--filter", f"name={cname}", "--format", "{{.Status}}")
    elif op == "logs":
        ok, out = deps["docker_fn"]("logs", "--tail", str(td.get("lines", 100)), cname)
    elif op == "inspect":
        ok, out = deps["docker_fn"]("inspect", "--format", td.get("format", "{{json .State}}"), cname)
    else:
        return {"ok": False, "error": f"unknown docker op '{op}'"}
    return {"ok": ok, "output": (out or "")[:_OUT_LIMIT]}


def _sql_query(td, kwargs, deps):
    db = td.get("db", {})
    cname = db.get("container") or f"app-{td['_app']}"
    engine = db.get("engine", "postgres")
    query = _interp(td.get("query", kwargs.get("query", "")), kwargs)
    if not query:
        return {"ok": False, "error": "no query provided"}
    # Read-only enforcement: run inside a read-only txn and roll back, so even
    # DML smuggled into the query cannot persist.
    wrapped = f"BEGIN TRANSACTION READ ONLY; {query.rstrip().rstrip(';')}; ROLLBACK;"
    if engine == "postgres":
        cmd = ["psql", "-U", db.get("user", "postgres"), "-d", db.get("db", "postgres"),
               "-tA", "-c", wrapped]
    elif engine == "mariadb":
        cmd = ["mariadb", "-u", db.get("user", "root"), f"-p{db.get('password', '')}",
               "-N", "-e", wrapped]
    else:
        return {"ok": False, "error": f"unknown db engine '{engine}'"}
    ok, out = deps["docker_fn"]("exec", cname, *cmd)
    return {"ok": ok, "output": (out or "")[:_OUT_LIMIT]}


def _dispatch(td, kwargs, deps):
    if td.get("write"):
        verdict = deps["gate"].verdict(td["_app"])
        if verdict == "deny":
            return {"ok": False,
                    "error": f"'{td['name']}' is write-scoped; approval policy denies"}
        if verdict == "prompt":
            aid = deps["gate"].register(td, kwargs, deps)
            return {"ok": False, "approval_required": True, "approval_id": aid,
                    "message": "write tool awaits approval — approve in Heimdall"}
        # auto -> fall through and execute
    handler = td.get("handler", "docker")
    return {
        "http": _http_call, "docker_exec": _docker_exec,
        "docker": _docker_op, "sql": _sql_query,
    }.get(handler, lambda *a: {"ok": False, "error": f"unknown handler '{handler}'"})(td, kwargs, deps)


# ---------------------------------------------------------------- vault / telemetry / approvals

class Vault:
    """Fernet-encrypted credential store keyed by app id.

    - No key -> plaintext file (degraded mode, warned)
    - Legacy plaintext file + key -> migrated to encrypted on first load
    """

    def __init__(self, path=None, key=None):
        self.path = path
        self._fernet = None
        if key:
            try:
                from cryptography.fernet import Fernet
                self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
            except Exception as e:
                print(f"[mcp] vault key invalid, falling back to plaintext: {e}")
        self._data = self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return {}
        raw = open(self.path, "rb").read()
        if not raw:
            return {}
        if self._fernet:
            try:
                return json.loads(self._fernet.decrypt(raw).decode())
            except Exception:
                pass  # not encrypted (or wrong key) -> legacy path below
        try:
            data = json.loads(raw.decode())
        except Exception:
            return {}
        if self._fernet:
            self._save(data)  # migrate legacy plaintext -> encrypted
        return data

    def _save(self, data):
        if not self.path:
            return
        blob = json.dumps(data).encode()
        if self._fernet:
            blob = self._fernet.encrypt(blob)
        open(self.path, "wb").write(blob)

    def get(self, app_id):
        return self._data.get(app_id) or {}

    def set(self, app_id, creds):
        self._data[app_id] = creds
        self._save(self._data)


_TELEMETRY = deque(maxlen=200)
_TELEMETRY_LOCK = threading.Lock()


def _log_call(tool, app_id, ok, ms):
    with _TELEMETRY_LOCK:
        _TELEMETRY.append({"ts": time.time(), "tool": tool, "app": app_id, "ok": ok, "ms": ms})


def drain_telemetry():
    """Return and clear the tool-call telemetry ring buffer (heartbeat consumer)."""
    with _TELEMETRY_LOCK:
        out = list(_TELEMETRY)
        _TELEMETRY.clear()
    return out


class ApprovalGate:
    """Per-app write policy: deny (default) | prompt (approval round-trip) | auto.

    A `prompt` write call returns approval_required + id; decide(approve=True)
    executes the stored call in-process and keeps the result fetchable by id.
    """

    TTL = 600  # pending approvals expire after 10 min

    def __init__(self, policy=None, allow_writes=False):
        p = policy or {}
        self._apps = dict(p.get("apps") or {})
        self._default = p.get("default") or ("auto" if allow_writes else "deny")
        self._pending = {}
        self._lock = threading.Lock()

    def verdict(self, app_id):
        return self._apps.get(app_id, self._default)

    def register(self, td, kwargs, deps):
        aid = uuid.uuid4().hex[:12]
        with self._lock:
            self._pending[aid] = {"id": aid, "tool": td["name"], "app": td["_app"],
                                  "args": kwargs, "ts": time.time(), "status": "pending",
                                  "result": None, "td": td, "deps": deps}
        return aid

    def list_pending(self):
        now = time.time()
        out = []
        with self._lock:
            for aid in list(self._pending):
                item = self._pending[aid]
                if item["status"] == "pending" and now - item["ts"] > self.TTL:
                    item["status"] = "expired"
                if item["status"] == "pending":
                    out.append({"id": aid, "tool": item["tool"], "app": item["app"],
                                "args": item["args"], "ts": item["ts"]})
        return out

    def decide(self, aid, approve):
        with self._lock:
            item = self._pending.get(aid)
            if not item:
                return None, "not_found"
            if item["status"] != "pending":
                return item, "already_" + item["status"]
            if not approve:
                item["status"] = "denied"
                return item, "denied"
            item["status"] = "approved"
        td = dict(item["td"])
        td["write"] = False  # already approved — bypass the gate when executing
        result = _dispatch(td, item["args"], item["deps"])
        with self._lock:
            item["result"] = result
        return item, "executed"


# ---------------------------------------------------------------- tool factory

# Tier 0: auto-generic tools registered for every INSTALLED app (no manifest needed).
_GENERIC_TOOLS = (
    {"name": "_docker_status", "description": "Docker container status (read-only)",
     "handler": "docker", "op": "status",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "_logs", "description": "Tail container logs (read-only)",
     "handler": "docker", "op": "logs",
     "inputSchema": {"type": "object", "properties": {
         "lines": {"type": "integer", "description": "number of log lines", "default": 100}}}},
)

def _ann(ps, default):
    """JSON-schema type -> python annotation string (for exec'd signature)."""
    if ps.get("enum"):
        base = "Literal[" + ", ".join(repr(v) for v in ps["enum"]) + "]"
    else:
        base = {"string": "str", "integer": "int", "number": "float",
                "boolean": "bool", "array": "list", "object": "dict"}.get(ps.get("type", "string"), "str")
    return f"Optional[{base}]" if default is None else base


def _make_run(td, deps):
    """Build a typed tool function from a manifest inputSchema.

    Uses exec() so the signature carries real annotations/defaults -> FastMCP
    derives the exact inputSchema (required vs optional) for tools/list.
    Param names come from the manifest and are validated; defaults are repr'd.
    """
    schema = td.get("inputSchema") or {}
    required = set(schema.get("required", []))
    parts, ns = [], {"_dispatch": _dispatch, "_td": td, "_deps": deps,
                     "Optional": Optional, "Literal": Literal}
    for pname, pspec in (schema.get("properties") or {}).items():
        if not _NAME_RE.match(pname):
            continue
        default = pspec.get("default", ...)
        if pname not in required and default is ...:
            default = None
        default_txt = "" if default is ... else " = " + repr(default)
        parts.append(f"{pname}: {_ann(pspec, default)}{default_txt}")
    exec(f"def run({', '.join(parts)}):\n    return _dispatch(_td, locals(), _deps)", ns)
    run = ns["run"]

    def run_with_telemetry(**kwargs):
        t0 = time.monotonic()
        result = {"ok": False}
        try:
            result = _dispatch(td, kwargs, deps)
            return json.dumps(result)
        finally:
            _log_call(td.get("name", ""), td.get("_app", ""), bool(result.get("ok")),
                      round((time.monotonic() - t0) * 1000))

    # copy the exec'd signature (annotations/defaults) onto the telemetry wrapper
    run_with_telemetry.__signature__ = inspect.signature(run)
    return run_with_telemetry


def build_gateway(catalog_getter, docker_fn, vault_getter, get_host_port, allow_writes=False, gate=None):
    """Construct the FastMCP server and register one tool per manifest entry,
    plus Tier 0 generic status/logs tools for every installed app."""
    mcp = FastMCP("appvault")
    gate = gate or ApprovalGate(allow_writes=allow_writes)
    deps = {"docker_fn": docker_fn, "vault": vault_getter,
            "get_host_port": get_host_port, "gate": gate}
    seen = set()
    for app in catalog_getter() or []:
        app_id = app.get("id")
        for td in app.get("mcp", {}).get("tools", []) or []:
            td = dict(td)
            name = td.get("name")
            if not name or name in seen:
                print(f"[mcp] skipping duplicate/unnamed tool {name!r}")
                continue
            seen.add(name)
            td["_app"] = app_id
            mcp.add_tool(_make_run(td, deps), name=name,
                         description=td.get("description", f"{app_id} tool"))
            print(f"[mcp] registered {name}")
        if app.get("_installed"):
            for g in _GENERIC_TOOLS:
                name = f"{app_id}{g['name']}"
                if name in seen:
                    continue
                seen.add(name)
                td = dict(g)
                td["_app"] = app_id
                mcp.add_tool(_make_run(td, deps), name=name, description=g["description"])
                print(f"[mcp] registered {name} (generic)")
    return mcp


# ---------------------------------------------------------------- server

def _wrap_auth(asgi_app, api_key):
    """401 unless the request carries the agent API key (Bearer or X-Api-Key)."""
    if not api_key:
        return asgi_app
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class ApiKeyAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            key = (request.headers.get("Authorization", "") or "").removeprefix("Bearer ").strip()
            if not key:
                key = request.headers.get("X-Api-Key", "") or ""
            if key != api_key:
                return JSONResponse({"error": "Unauthorized",
                                     "message": "valid API key required"}, status_code=401)
            return await call_next(request)

    return ApiKeyAuth(asgi_app)


def start_gateway(catalog_getter=None, docker_fn=None, get_host_port=None, vault=None,
                  host="0.0.0.0", port=8087, api_key="", allow_writes=False,
                  write_policy=None, gate=None):
    """Build the gateway and serve it via uvicorn (call from a daemon thread)."""
    import uvicorn

    vault = vault or Vault(None)
    gate = gate or ApprovalGate(write_policy, allow_writes)
    get_host_port = get_host_port or (lambda cname: None)
    docker_fn = docker_fn or (lambda *a, **k: (False, "docker unavailable"))
    mcp = build_gateway(catalog_getter or (lambda: []), docker_fn, vault.get, get_host_port,
                        allow_writes, gate)
    app = _wrap_auth(mcp.streamable_http_app(), api_key)
    print(f"[mcp] serving {len(mcp._tool_manager._tools) if hasattr(mcp, '_tool_manager') else '?'} tools "
          f"on {host}:{port} (write policy: {gate.verdict('*') or 'deny'})")
    uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")).run()
