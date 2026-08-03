# MCP Manifests — Authoring Guide

The MCP gateway (`mcp_gateway.py`, :8087) exposes installed apps as LLM-callable tools.
**Manifests are data, not code** — they live in the central catalog
(`appvault-cloud/static/catalog.json`), ship to agents via the existing catalog sync,
and require **zero gateway changes** per app.

## Tool registration rules

1. Every catalog app with an `mcp.tools[]` list registers one MCP tool per entry.
2. **Tier 0 (no manifest needed):** every *installed* app automatically gets
   `{app_id}_docker_status` and `{app_id}_logs` (read-only) from the gateway.
   A manifest tool with the same name wins over the generic one.
3. Tools appear regardless of install status; uninstalled apps return a graceful
   error envelope (`{"ok": false, ...}`).

## Manifest schema

```json
"mcp": {
  "credential": { "type": "api_key", "header": "X-APP-API-KEY", "via": "setup_wizard" },
  "tools": [
    {
      "name": "app_action",            // unique across the whole server
      "description": "What the tool does (the LLM reads this — be specific)",
      "handler": "http",               // http | docker_exec | docker | sql
      "write": false,                  // true => approval-gated (denied by default)
      "inputSchema": {
        "type": "object",
        "required": ["id"],
        "properties": {
          "id":   { "type": "string", "description": "resource id" },
          "page": { "type": "integer", "description": "page number", "default": 1 }
        }
      },
      "method": "GET",                 // http only
      "path": "/api/v1/items/{id}",    // http only; {param} replaced from args
      "base_url": null,                // optional; overrides local port discovery
      "cmd": ["sh", "-c", "echo {msg}"],  // docker_exec only; {param} replaced
      "op": "status",                  // docker only: status | logs | inspect
      "lines": 100,                    // docker logs: tail count
      "db": {                          // sql only
        "engine": "postgres",          // postgres | mariadb
        "container": "app-central-postgres",
        "user": "baserow", "db": "baserow", "password": "baserow_secret"
      },
      "query": "SELECT ... {query}"    // sql only; {query} interpolated
    }
  ]
}
```

## Handlers

| handler | what it does | how it reaches the app |
|---|---|---|
| `http` | calls the app's REST API | `http://127.0.0.1:<mapped-port><path>` (AppVault container) or `base_url` (external site) |
| `docker_exec` | runs a command inside the container | `docker exec app-<id> <cmd...>` — read-only by convention; never `tty`/privileged |
| `docker` | container status / logs / inspect | `docker ps --filter`, `docker logs --tail N` |
| `sql` | read-only DB query | `docker exec <db-container> psql/mariadb` — **always wrapped in `BEGIN TRANSACTION READ ONLY … ROLLBACK`** so even DML cannot persist |

## Conventions & guardrails

- **Read-only by default.** `write: true` tools are denied until the Phase 2
  approval gate ships (`MCP_ALLOW_WRITES=1` is dev-only).
- **Credentials** come from the vault (`<STORAGE_PATH>/creds.json`, Phase 2:
  encrypted), keyed by app id: `{"<app_id>": {"header": "X-API-KEY", "value": "…"}}`.
  The `credential.header` documents which header the wizard should collect.
- **Output cap** 4 KB, **timeout** 15 s per call — keep tool responses small
  (return IDs + titles, not full documents).
- **Names** must be globally unique on the server; prefix with the app id
  (`n8n_…`, `uptime_kuma_…`). Site-prefixed for external sites
  (`clientA_wordpress_…` — Track B, Phase 2b).
- `{param}` interpolation works in `path` and `cmd` strings; unmatched params go
  to the query string (GET) or JSON body (POST).

## Worked examples (live in the catalog)

- **n8n** — `n8n_list_workflows` (http GET), `n8n_get_workflow` (http GET, `{id}`),
  `n8n_activate_workflow` (http POST, `write: true`) + `X-N8N-API-KEY` credential
- **Uptime Kuma** — `uptime_kuma_status_page` (http GET `/api/status-page/{slug}`)
- **Baserow** — `baserow_query` (sql against `app-central-postgres`, read-only)

## Adding a new app

1. Edit `appvault-cloud/static/catalog.json` → add `mcp` to the app entry.
2. Validate: `python -c "import json; json.load(open('appvault-cloud/static/catalog.json'))"`.
3. Deploy the catalog (Coolify redeploy of appvault-cloud) → agents pick it up on
   next `sync_catalog` — no agent/gateway restart needed for new tools on next
   gateway boot; dynamic re-registration is a later enhancement.
4. Verify: `tools/list` on a test agent shows the new tools (see the e2e harness pattern).
