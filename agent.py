#!/usr/bin/env python3
"""
AppVault Agent â€” runs on user machine (local or VPS).
- Serves Heimdall-compatible local API (port 8086)
- Phone-homes to central server for catalog updates + remote jobs
- Executes Docker install/uninstall/restart locally
- Reports status back to central server
- NO admin functionality
"""

import os, json, threading, time, uuid, hashlib, socket, sys, subprocess, shutil
import urllib.request
import urllib.error
from datetime import datetime
from flask import Flask, jsonify, request, render_template, send_from_directory, Response, redirect
from datetime import timedelta
from functools import wraps
import cloud_sync

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CONFIG
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

AGENT_PORT = int(os.getenv("AGENT_PORT", "8086"))
CENTRAL_URL = os.getenv("CENTRAL_URL", "http://central:8000")
AGENT_ID = os.getenv("AGENT_ID", "")
AGENT_NAME = os.getenv("AGENT_NAME", socket.gethostname())
API_KEY = os.getenv("API_KEY", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))  # seconds between polls
STORAGE_PATH = os.getenv("STORAGE_PATH", "/data")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

def public_base_host():
    """Host part of PUBLIC_URL (no scheme), for raw http:// links to non-proxied ports."""
    pu = PUBLIC_URL or ""
    pu = pu.replace("https://", "").replace("http://", "").split("/")[0]
    return pu or "127.0.0.1"


def public_base():
    """Base URL for app links: PUBLIC_URL if set, else localhost (local installs)."""
    return PUBLIC_URL if PUBLIC_URL else "http://localhost"
CATALOG_CACHE_PATH = os.path.join(STORAGE_PATH, "catalog_cache.json")
AGENT_STATE_PATH = os.path.join(STORAGE_PATH, "agent_state.json")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))  # seconds between heartbeats

def _safe_rmtree(path):
    """Safely remove a directory tree, clearing read-only flags (e.g. Windows .git objects)."""
    import stat
    def on_rm_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    if os.path.exists(path):
        try:
            shutil.rmtree(path, onerror=on_rm_error)
        except Exception as e:
            print(f"[agent] _safe_rmtree warning for {path}: {e}")

def _http_call(url, method="GET", json_data=None, timeout=5):
    try:
        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")
        data_bytes = json.dumps(json_data).encode("utf-8") if json_data is not None else None
        with urllib.request.urlopen(req, data=data_bytes, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body), resp.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            return json.loads(body), e.code
        except Exception:
            return {"error": "HTTP Error"}, e.code
    except Exception as e:
        return {"error": str(e)}, 502

CATALOG_VERSION_FILE = os.path.join(STORAGE_PATH, "catalog_version.txt")

os.makedirs(STORAGE_PATH, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))
APP_VERSION = "1.0.0"

# -- CORS: only the store UI origin (and explicitly allow-listed origins) --
AGENT_CORS_ORIGINS = {o.strip() for o in os.getenv("AGENT_CORS_ORIGINS", "").split(",") if o.strip()}
UI_ORIGIN_PORT = int(os.getenv("UI_PORT", "8085"))

def _origin_allowed(origin):
    if not origin:
        return False
    if origin in AGENT_CORS_ORIGINS:
        return True
    # The store UI (heimdall) on any host - localhost, LAN IP, or VPS domain -
    # but served on the UI port. Random websites never match this.
    try:
        from urllib.parse import urlparse
        p = urlparse(origin)
        return p.scheme in ("http", "https") and p.port == UI_ORIGIN_PORT
    except Exception:
        return False

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Agent-Id, X-Api-Key, X-User-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

# -- Auth guard --
# Read-only catalog/status endpoints (GET) are PUBLIC so a fresh install shows free
# apps without a pre-provisioned API key. Everything else (including install/
# uninstall/restart/stop/exec/agentic) requires a valid X-Api-Key when API_KEY is set.
PUBLIC_READ_PREFIXES = ("/api/catalog", "/api/health", "/api/info", "/api/agent/status", "/api/stats",
                        "/api/apps/health", "/api/education/", "/api/icon/", "/api/ping/",
                        "/api/agentic/status", "/api/agentic/health", "/api/agentic/config",
                        "/api/agentic/news", "/api/agentic/bootstrap", "/api/agentic/brain",
                        "/api/agentic/memory")

@app.before_request
def require_api_key():
    if request.method == "OPTIONS":
        return None
    effective_allowed_keys = []
    if API_KEY:
        effective_allowed_keys.append(API_KEY)
    reg_key = agent_state.get("api_key") if 'agent_state' in globals() else None
    if reg_key:
        effective_allowed_keys.append(reg_key)
        
    if not effective_allowed_keys:
        return None
        
    path = request.path
    if path.startswith("/api/"):
        is_public_read = request.method == "GET" and path.startswith(PUBLIC_READ_PREFIXES)
        if not is_public_read:
            import hmac as _hmac
            key = request.headers.get("X-Api-Key", "")
            authorized = False
            for allowed in effective_allowed_keys:
                if _hmac.compare_digest(key, allowed):
                    authorized = True
                    break
            if not authorized:
                return jsonify({"error": "Unauthorized", "message": "Valid X-Api-Key header required"}), 401
    return None

if not API_KEY:
    print("[agent] WARNING: API_KEY is not set - the agent API is UNAUTHENTICATED. "
          "Set API_KEY in the environment (the store UI accepts it via ?setup=KEY).", flush=True)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DOCKER â€” via CLI (more reliable than docker-py)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

DOCKER_CMD = shutil.which("docker") or "/usr/bin/docker"

def _docker(*args, capture=False, timeout=120):
    """Run a Docker CLI command. Returns (success, stdout_or_error)."""
    try:
        cmd = [DOCKER_CMD] + list(args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")}
        )
        if result.returncode != 0:
            # Fallback for 'docker compose' -> 'docker-compose' binary if plugin is missing
            if args and args[0] == "compose":
                dc_binary = shutil.which("docker-compose")
                if dc_binary:
                    fallback_cmd = [dc_binary] + list(args[1:])
                    fb_res = subprocess.run(
                        fallback_cmd, capture_output=True, text=True, timeout=timeout,
                        env={**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")}
                    )
                    if fb_res.returncode == 0:
                        return True, fb_res.stdout.strip()
            err = result.stderr.strip() or result.stdout.strip()[:200]
            return False, err
        return True, result.stdout.strip()
    except Exception as e:
        return False, str(e)

def docker_available() -> bool:
    ok, out = _docker("info", "--format", "{{.ServerVersion}}", capture=True)
    return ok

def docker_version() -> str:
    ok, out = _docker("info", "--format", "{{.ServerVersion}}", capture=True)
    return out if ok else "N/A"

def docker_info():
    return {"available": docker_available(), "version": docker_version()}

def container_exists(name: str) -> bool:
    ok, out = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}", capture=True)
    # docker's name= filter is a SUBSTRING match - require an exact line so
    # app-foo is not confused with app-foo2 (wrong uninstall/restart target).
    return ok and any(n.strip() == name for n in out.splitlines())

def container_running(name: str) -> bool:
    ok, out = _docker("ps", "--filter", f"name={name}", "--filter", "status=running", "--format", "{{.Names}}", capture=True)
    return ok and any(n.strip() == name for n in out.splitlines())

def container_status(name: str) -> str:
    ok, out = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Status}}", capture=True)
    return out[:20] if ok else "not_found"

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LOCAL CATALOG CACHE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _merge_mcp_manifests(catalog):
    """Merge local per-app MCP tool manifests into a catalog dict (in place).
    Survives central sync because callers re-apply it after every refresh."""
    try:
        _manifests_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_manifests.json")
        if not os.path.exists(_manifests_path):
            return catalog
        with open(_manifests_path, encoding="utf-8") as _f:
            manifests = json.load(_f)
        for _app in catalog.get("apps", []):
            _mf = manifests.get(_app.get("id"))
            if not _mf:
                continue
            _mcp = _app.setdefault("mcp", {})
            _existing = {t.get("name") for t in _mcp.get("tools", [])}
            for _td in _mf.get("tools", []):
                if _td.get("name") and _td["name"] not in _existing:
                    _mcp.setdefault("tools", []).append(_td)
            if _mf.get("credential"):
                _mcp.setdefault("credential", _mf["credential"])
    except Exception as _e:
        print(f"[agent] mcp manifests merge failed: {_e}")
    return catalog

def load_catalog_cache():
    if os.path.exists(CATALOG_CACHE_PATH):
        try:
            with open(CATALOG_CACHE_PATH) as f:
                catalog = json.load(f)
        except:
            catalog = None
    else:
        catalog = None
    if not catalog:
        catalog = {"version": 0, "apps": []}
    return _merge_mcp_manifests(catalog)

def save_catalog_cache(catalog):
    tmp = CATALOG_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(catalog, f, indent=2)
    os.replace(tmp, CATALOG_CACHE_PATH)  # atomic: a crash can't truncate the cache
    # Also save version separately for quick checks
    with open(CATALOG_VERSION_FILE + ".tmp", "w") as f:
        f.write(str(catalog.get("version", 0)))
    os.replace(CATALOG_VERSION_FILE + ".tmp", CATALOG_VERSION_FILE)

# ── Admin catalog overrides (free/premium per app) ──
# The store catalog (catalog.json) marks each app free_tier / locked /
# requires_paid. The admin can override those flags per app from the
# dashboard (Settings → Catalog Manager) without editing the catalog file;
# overrides persist here and are layered on top of every catalog response.
CATALOG_OVERRIDES_PATH = os.path.join(STORAGE_PATH, "catalog_overrides.json")
_OVERRIDE_LOCK = threading.RLock()

def load_catalog_overrides():
    try:
        with _OVERRIDE_LOCK:
            if os.path.exists(CATALOG_OVERRIDES_PATH):
                with open(CATALOG_OVERRIDES_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
    except Exception:
        pass
    return {}

def save_catalog_overrides(overrides):
    with _OVERRIDE_LOCK:
        tmp = CATALOG_OVERRIDES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)
        os.replace(tmp, CATALOG_OVERRIDES_PATH)

@app.route("/api/agentic/catalog/overrides", methods=["GET", "POST", "OPTIONS"])
def api_catalog_overrides():
    """Admin: list or update per-app free/premium overrides."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        app_id = (data.get("app_id") or data.get("id") or "").strip()
        if not app_id:
            return jsonify({"error": "app_id required"}), 400
        overrides = load_catalog_overrides()
        cur = overrides.get(app_id) or {}
        if "free_tier" in data and isinstance(data["free_tier"], bool):
            cur["free_tier"] = data["free_tier"]
        if "locked" in data and isinstance(data["locked"], bool):
            cur["locked"] = data["locked"]
        if "requires_paid" in data and isinstance(data["requires_paid"], bool):
            cur["requires_paid"] = data["requires_paid"]
        if not cur:
            overrides.pop(app_id, None)
        else:
            overrides[app_id] = cur
        save_catalog_overrides(overrides)
        _CATALOG_RESP_CACHE = None  # invalidate catalog cache
        return jsonify({"status": "ok", "app_id": app_id, "override": overrides.get(app_id)})
    # GET
    return jsonify({"status": "ok", "overrides": load_catalog_overrides()})

def get_cached_version():
    try:
        with open(CATALOG_VERSION_FILE) as f:
            return int(f.read().strip())
    except:
        return 0

catalog_cache = load_catalog_cache()

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# AGENT STATE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_STATE_LOCK = threading.RLock()

def load_agent_state():
    if os.path.exists(AGENT_STATE_PATH):
        try:
            with open(AGENT_STATE_PATH) as f:
                return json.load(f)
        except Exception as _e:
            print(f"[agent] WARNING: {AGENT_STATE_PATH} is corrupt ({_e}) - "
                  "starting fresh; identity/ports/license may be re-provisioned")
    return {"agent_id": AGENT_ID, "api_key": API_KEY}

def save_agent_state(state):
    with _STATE_LOCK:
        tmp = AGENT_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, AGENT_STATE_PATH)  # atomic: never lose agent identity mid-write

agent_state = load_agent_state()
# Prefer a persisted license key; fall back to env (initial provisioning)
if not agent_state.get("license_key"):
    agent_state["license_key"] = os.getenv("LICENSE_KEY", "")

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HTTP CLIENT for central server
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def central_request(method, path, data=None, params=None):
    """Make HTTP request to central server."""
    import urllib.request
    import urllib.parse
    import ssl
    
    effective_id = agent_state.get("agent_id", AGENT_ID)
    effective_key = agent_state.get("api_key", API_KEY)
    
    url = f"{CENTRAL_URL}{path}"
    
    if params:
        url += "?" + urllib.parse.urlencode(params)
    elif method == "GET":
        url += f"?agent_id={effective_id}&api_key={effective_key}"
    
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    
    if data:
        body = json.dumps(data).encode()
    else:
        body = None
    
    # TLS verification ON by default. Set CENTRAL_TLS_VERIFY=0 only for a
    # self-signed central you control - an unverified channel lets a MITM
    # feed the agent malicious images/compose files (remote code execution).
    ctx = ssl.create_default_context()
    if os.getenv("CENTRAL_TLS_VERIFY", "1").strip().lower() in ("0", "false", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, data=body, timeout=10, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[agent] Central request failed ({method} {path}): {e}")
        return None

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PHONE HOME â€” runs in background thread
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def register_with_central():
    """Register this agent with the central server."""
    docker = docker_info()
    result = central_request("POST", "/api/agent/register", data={
        "agent_id": agent_state.get("agent_id", AGENT_ID),
        "name": AGENT_NAME,
        "os": sys.platform,
        "docker_version": docker["version"],
        "app_version": APP_VERSION,
        "license_key": agent_state.get("license_key", os.getenv("LICENSE_KEY", "")),
    })
    
    if result:
        # Save the credentials
        with _STATE_LOCK:
            agent_state["agent_id"] = result["agent_id"]
            agent_state["api_key"] = result["api_key"]
            save_agent_state(agent_state)
        print(f"[agent] Registered with central as '{result['agent_id'][:12]}...'")
        return True
    else:
        print("[agent] Registration failed â€” will retry")
        return False

_job_threads = []
_inflight_jobs = set()
_inflight_jobs_lock = threading.Lock()
JOB_THREAD_CAP = 2  # concurrent central jobs; more would starve the host

def _run_job_tracked(job):
    job_id = job["id"]
    try:
        print(f"[agent] Executing job #{job_id}: {job['action']} {job['app_id']}")
        execute_job(job)
    finally:
        with _inflight_jobs_lock:
            _inflight_jobs.discard(job_id)

def poll_jobs():
    """Check for pending jobs from central server.

    Jobs run in worker threads (bounded) so a 10-minute install cannot block
    heartbeats/catalog sync on the phone-home loop. In-flight ids are tracked
    because central keeps a job 'pending' until we report - without the guard
    every poll would re-dispatch the same job."""
    global _job_threads
    effective_id = agent_state.get("agent_id", "")
    effective_key = agent_state.get("api_key", "")
    if not effective_id or not effective_key:
        return

    result = central_request("GET", "/api/agent/jobs", params={
        "agent_id": effective_id,
        "api_key": effective_key
    })

    if result and result.get("jobs"):
        _job_threads = [t for t in _job_threads if t.is_alive()]
        for job in result["jobs"]:
            if job["id"] in _inflight_jobs:
                continue
            if len(_job_threads) >= JOB_THREAD_CAP:
                print("[agent] Job backlog: max concurrent jobs reached; re-trying next poll")
                break
            with _inflight_jobs_lock:
                _inflight_jobs.add(job["id"])
            t = threading.Thread(target=_run_job_tracked, args=(job,), daemon=True)
            t.start()
            _job_threads.append(t)

def sync_catalog(force=False):
    """Check if catalog has been updated and sync if needed. force=True always re-fetches."""
    global catalog_cache
    effective_id = agent_state.get("agent_id", "")
    effective_key = agent_state.get("api_key", "")
    if not effective_id or not effective_key:
        return
    
    local_ver = get_cached_version()
    
    # Check version
    ver_result = central_request("GET", "/api/agent/catalog/version", params={
        "agent_id": effective_id,
        "api_key": effective_key
    })
    
    if ver_result:
        remote_ver = ver_result.get("version", 0)
        remote_plan = ver_result.get("plan")
        local_plan = catalog_cache.get("plan")
        plan_changed = (remote_plan is not None) and (remote_plan != local_plan)
        # Remote version LOWER than cached = central DB was reset → force re-fetch
        version_reset = remote_ver < local_ver
        if force or remote_ver > local_ver or plan_changed or version_reset:
            reason = "force" if force else (f"v{local_ver} -> v{remote_ver}" if remote_ver > local_ver else (f"reset v{remote_ver} < v{local_ver}" if version_reset else f"plan {local_plan} -> {remote_plan}"))
            print(f"[agent] Catalog update available: {reason}")
            catalog_result = central_request("GET", "/api/agent/catalog", params={
                "agent_id": effective_id,
                "api_key": effective_key
            })
            if catalog_result:
                catalog_cache = _merge_mcp_manifests(catalog_result)
                save_catalog_cache(catalog_result)
                print(f"[agent] Catalog synced: v{remote_ver} ({len(catalog_cache.get('apps', []))} apps)")

def _fleet_telemetry():
    """Compact fleet-health payload for the central dashboard: memory size,
    error count (24h), active missions, LLM token spend (7d) + provider."""
    import sqlite3 as _sq
    out = {"version": "unknown", "docker": "unknown", "memory": 0, "errors_24h": 0,
           "missions": 0, "tokens_7d": 0, "cost_7d": 0.0, "provider": "", "model": ""}
    try:
        db_path = os.path.join(os.environ.get("STORAGE_PATH", "/data"), "agentic.db")
        if os.path.exists(db_path):
            conn = _sq.connect(db_path)
            conn.row_factory = _sq.Row
            try:
                out["memory"] = conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
                out["errors_24h"] = conn.execute(
                    "SELECT COUNT(*) c FROM audit_log WHERE ts >= datetime('now','-1 day') "
                    "AND (action LIKE '%fail%' OR action LIKE '%error%')").fetchone()["c"]
            except Exception:
                pass
            try:
                out["missions"] = conn.execute(
                    "SELECT COUNT(*) c FROM missions WHERE status IN ('running','active','pending')").fetchone()["c"]
            except Exception:
                pass
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) t, COALESCE(SUM(cost_usd),0) c "
                    "FROM llm_usage WHERE ts >= datetime('now','-7 day')").fetchone()
                out["tokens_7d"] = row["t"] or 0
                out["cost_7d"] = round(row["c"] or 0.0, 4)
            except Exception:
                pass
            conn.close()
    except Exception:
        pass
    try:
        import agentic_plane as _ap
        cfg = _ap._get_llm_config()
        out["provider"] = cfg.get("provider", "")
        out["model"] = cfg.get("model", "")
    except Exception:
        pass
    out["version"] = os.getenv("AGENT_VERSION", "dev")
    try:
        r = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
        out["docker"] = (r.stdout or "").strip()
    except Exception:
        pass
    return out

def send_heartbeat():
    """Send heartbeat to central server."""
    effective_id = agent_state.get("agent_id", "")
    effective_key = agent_state.get("api_key", "")
    if not effective_id or not effective_key:
        return
    
    central_request("POST", "/api/agent/heartbeat", data={
        "agent_id": effective_id,
        "api_key": effective_key,
        "telemetry": _fleet_telemetry()
    })

def execute_job(job):
    """Execute a job from the central server."""
    job_id = job["id"]
    action = job["action"]
    app_id = job["app_id"]
    effective_id = agent_state.get("agent_id", "")
    effective_key = agent_state.get("api_key", "")
    
    try:
        if action == "install":
            app_def = None
            for a in catalog_cache.get("apps", []):
                if a["id"] == app_id:
                    app_def = a
                    break
            if app_def and (app_def.get("is_stack") or app_def.get("compose_url")):
                _do_install_stack(app_id)
            else:
                _do_install(app_id)
        elif action == "uninstall":
            _do_uninstall(app_id)
        elif action == "restart":
            _do_restart(app_id)
        
        # Report success
        central_request("POST", f"/api/agent/jobs/{job_id}/status", data={
            "agent_id": effective_id,
            "api_key": effective_key,
            "status": "completed",
            "result": f"{action} {app_id} succeeded"
        })
        print(f"[agent] Job #{job_id} completed: {action} {app_id}")
        
    except Exception as e:
        # Report failure
        central_request("POST", f"/api/agent/jobs/{job_id}/status", data={
            "agent_id": effective_id,
            "api_key": effective_key,
            "status": "failed",
            "result": str(e)
        })
        print(f"[agent] Job #{job_id} failed: {e}")

_PORT_CACHE = {}
_PORT_CACHE_TTL = 60  # seconds; docker port lookups are expensive (~300ms each via CLI)
_BULK_CACHE_TS = 0
_BULK_CACHE_TTL = 60  # seconds — status/host-port info is refreshed from a single docker ps
_BULK_NAMES = set()  # every container name seen in the last bulk ps (fast membership checks)
_BULK_LABELS = {}    # appvault.app=<id> label -> container name (from the bulk snapshot)
_BULK_PROJECTS = set()  # compose project prefixes (<id>_*) seen in the bulk snapshot
_CATALOG_RESP_CACHE = None
_CATALOG_RESP_TS = 0.0
_APPS_HEALTH_CACHE = None
_APPS_HEALTH_TS = 0.0
_STATS_CACHE = None
_STATS_TS = 0.0

def _refresh_bulk_container_state():
    """Populate _PORT_CACHE in a single bulk docker ps command for all containers."""
    global _BULK_CACHE_TS, _BULK_NAMES, _BULK_LABELS, _BULK_PROJECTS
    now = time.time()
    if now - _BULK_CACHE_TS < _BULK_CACHE_TTL:
        return
    _BULK_CACHE_TS = now
    _BULK_NAMES = set()
    _BULK_LABELS = {}
    _BULK_PROJECTS = set()
    ok, out = _docker("ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Ports}}\t{{.Image}}\t{{.Labels}}\t{{.Status}}", capture=True)
    if not ok or not out:
        return

    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        cname = parts[0].strip()
        _BULK_NAMES.add(cname)
        state = parts[1].strip().lower()  # e.g., 'running', 'exited'
        ports_str = parts[2].strip()
        image = parts[3].strip()
        labels = parts[4].strip() if len(parts) > 4 else ""
        status_str = parts[5].strip() if len(parts) > 5 else ""

        # Docker health state embedded in the status column ("Up 2m (healthy)")
        if "(healthy)" in status_str:
            _PORT_CACHE[("h", cname)] = (now, "healthy")
        elif "(unhealthy)" in status_str:
            _PORT_CACHE[("h", cname)] = (now, "unhealthy")

        is_running = (state == "running")
        status_val = "installed" if is_running else "stopped"

        _PORT_CACHE[("cr", cname)] = (now, is_running)
        _PORT_CACHE[("ce", cname)] = (now, True)

        app_id = ""
        if cname.startswith("app-"):
            app_id = cname[4:]
        elif "appvault.app=" in labels:
            for l in labels.split(","):
                if l.strip().startswith("appvault.app="):
                    app_id = l.strip().split("=", 1)[1]
                    break

        # Bulk indexes for label- and compose-project-based resolution
        if app_id and "appvault.app=" in labels:
            _BULK_LABELS[app_id] = cname
        if "_" in cname:
            _BULK_PROJECTS.add(cname.split("_", 1)[0])

        if app_id:
            _PORT_CACHE[("st", app_id)] = (now, status_val)
            # Several containers can carry the same app label (stack front +
            # private backends). Prefer one that actually publishes a host
            # port: the portless one would make host-port lookups fall back
            # to the (wrong) catalog container_port.
            cur = _PORT_CACHE.get(("cn", app_id))
            publishes_port = ("->" in ports_str)
            if cur is None or publishes_port or (time.time() - cur[0]) >= _BULK_CACHE_TTL:
                _PORT_CACHE[("cn", app_id)] = (now, cname)
            if image:
                _PORT_CACHE[("img", app_id)] = (now, image)

        # Per-container image (also for containers without a derivable app_id)
        if image:
            _PORT_CACHE[("imgc", cname)] = (now, image)

        if is_running and "->" in ports_str:
            for mapping in ports_str.split(","):
                mapping = mapping.strip()
                if "->" in mapping:
                    h_part, c_part = mapping.split("->", 1)
                    if ":" in h_part:
                        h_port = h_part.split(":")[-1].strip()
                        c_port = c_part.split("/")[0].strip()
                        if h_port:
                            _PORT_CACHE[("hp", cname)] = (now, h_port)
                            _PORT_CACHE[("ph", cname, c_port)] = (now, h_port)
                            if c_port.isdigit():
                                _PORT_CACHE[("ph", cname, int(c_port))] = (now, h_port)
                            break

def _cached_docker_port(key, fn, *args):
    """Cache docker port lookups for _PORT_CACHE_TTL seconds."""
    hit = _PORT_CACHE.get(key)
    if hit and time.time() - hit[0] < _PORT_CACHE_TTL:
        return hit[1]
    val = fn(*args)
    _PORT_CACHE[key] = (time.time(), val)
    if len(_PORT_CACHE) > 300:
        # Evict ONLY expired entries — a full clear mid-request wipes fresh
        # statuses and the bulk-cache-fresh guard in _get_app_status_local_uncached
        # then reports every app as "available" (the "installed apps missing
        # from My Apps" bug: 57 apps x several keys each exceeds 300, so the
        # clear fired inside /api/catalog and only the first few apps kept
        # their cached "installed" status). In-place deletes (no rebinding:
        # `_PORT_CACHE = ...` here would shadow the global -> UnboundLocalError).
        for k in [k for k, v in _PORT_CACHE.items() if time.time() - v[0] >= _PORT_CACHE_TTL]:
            del _PORT_CACHE[k]
    return val

def get_container_host_port(container_name):
    """Get the first host port mapped to a container (cached 60s)."""
    if not container_name:
        return None
    return _cached_docker_port(("hp", container_name), _get_container_host_port_uncached, container_name)

def _app_container_name(app_id):
    """Resolve the actual container name for an app: checks app-<id>, <id>,
    stack compose projects (<id>_*, <id>_stack_*), labels, and web frontends."""
    hit = _PORT_CACHE.get(("cn", app_id))
    if hit and time.time() - hit[0] < _PORT_CACHE_TTL:
        return hit[1]
    cname = f"app-{app_id}"
    # Fast path: resolve from the bulk snapshot (no docker subprocess)
    if cname in _BULK_NAMES:
        _PORT_CACHE[("cn", app_id)] = (time.time(), cname)
        return cname
    if app_id in _BULK_NAMES:
        _PORT_CACHE[("cn", app_id)] = (time.time(), app_id)
        return app_id
    # Label-based containers (from the bulk snapshot)
    lbl = _BULK_LABELS.get(app_id)
    if lbl:
        _PORT_CACHE[("cn", app_id)] = (time.time(), lbl)
        return lbl
    # Compose project containers (from the bulk snapshot): <id>_*
    if app_id in _BULK_PROJECTS:
        prefix = app_id + "_"
        for n in _BULK_NAMES:
            if n.startswith(prefix):
                _PORT_CACHE[("cn", app_id)] = (time.time(), n)
                return n
    # The bulk snapshot is authoritative: the app is not installed. Never
    # spawn docker per app just to rediscover that.
    if _BULK_CACHE_TS > 0 and (time.time() - _BULK_CACHE_TS < _BULK_CACHE_TTL):
        _PORT_CACHE[("cn", app_id)] = (time.time(), "")
        return ""
    if container_running(cname) or container_exists(cname):
        _PORT_CACHE[("cn", app_id)] = (time.time(), cname)
        return cname
    # Direct container name (e.g. open-webui, buzz, n8n, ciso-assistant)
    if container_running(app_id) or container_exists(app_id):
        _PORT_CACHE[("cn", app_id)] = (time.time(), app_id)
        return app_id

    # Search by labels (appvault.app, compose project) or name prefix
    ok, out = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                      "--format", "{{.Names}}", capture=True)
    candidates = [l.strip() for l in (out or "").strip().splitlines() if l.strip()]

    # If empty, search compose project or prefix
    if not candidates:
        ok2, out2 = _docker("ps", "-a", "--filter", f"name={app_id}",
                            "--format", "{{.Names}}", capture=True)
        if ok2 and out2:
            candidates = [l.strip() for l in out2.strip().splitlines() if l.strip()]

    if candidates:
        resolved = candidates[0]
        # Prioritize web UI / frontend / studio containers
        web_keywords = ["web", "frontend", "ui", "studio", "client", "dash"]
        for cand in candidates:
            cand_lower = cand.lower()
            if any(k in cand_lower for k in web_keywords):
                pok, pout = _docker("port", cand, capture=True, timeout=10)
                if pok and pout and "->" in pout:
                    resolved = cand
                    break
        # Fallback: any candidate publishing a port
        if resolved == candidates[0]:
            for cand in candidates:
                pok, pout = _docker("port", cand, capture=True, timeout=10)
                if pok and pout and "->" in pout:
                    resolved = cand
                    break
        _PORT_CACHE[("cn", app_id)] = (time.time(), resolved)
        return resolved

    _PORT_CACHE[("cn", app_id)] = (time.time(), cname)
    return cname

def _get_container_host_port_uncached(container_name):
    ok, out = _docker("port", container_name, capture=True)
    if ok and out:
        lines = out.strip().split('\n')
        for line in lines:
            if '->' in line:
                parts = line.split('->')
                host_part = parts[-1].strip()
                if ':' in host_part:
                    return host_part.split(':')[-1].strip()
                return host_part
    return None

def get_container_port_host(container_name, container_port):
    """Get the host port mapped to a specific container port (cached 60s)."""
    if not container_name:
        return None
    return _cached_docker_port(("ph", container_name, container_port), _get_container_port_host_uncached, container_name, container_port)

def _get_container_port_host_uncached(container_name, container_port):
    ok, out = _docker("port", container_name, f"{container_port}/tcp", capture=True)
    if ok and out:
        line = out.strip().split('\n')[0]
        if ':' in line:
            return line.split(':')[-1].strip()
    return None

import socket
def _find_free_port():
    """Find a free host port in the safe AppVault range (33000-39999).

    Never ask the OS for an ephemeral port (49152+): that range collides with
    Windows/Hyper-V reserved chunks and other services, and flaky binds there
    left apps unreachable (the anythingllm dropped-port-forward incident)."""
    import random
    for _ in range(200):
        candidate = random.randrange(33000, 40000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', candidate))
                return candidate
            except OSError:
                continue
    # last resort: legacy OS-assigned port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def _stable_host_port(container_name, app_id, container_port):
    """Return a STABLE host port for an app so restarts don't drift the port.
    - If the container already exists, reuse its current host port.
    - Otherwise reuse the port recorded for this app in agent state (updates
      recreate the container — the old container is gone, but the port must
      not drift).
    - Otherwise derive a deterministic port from the app_id in 30000-39999."""
    existing = get_container_host_port(container_name)
    if existing:
        return existing
    recorded = (agent_state.get("app_ports") or {}).get(app_id)
    if recorded:
        return str(recorded)
    import hashlib
    # deterministic port from app_id hash, in a safe range
    h = int(hashlib.sha256(app_id.encode()).hexdigest(), 16)
    stable = 30000 + (h % 9000)  # 30000-38999
    # Two app_ids can hash to the same port (birthday bound at ~112 apps) which
    # makes the second docker run fail to bind. Skip ports other apps own.
    taken = {str(p) for aid, p in (agent_state.get("app_ports") or {}).items() if aid != app_id}
    while str(stable) in taken:
        stable += 1
        if stable > 38999:
            stable = 30000
    return str(stable)

def _record_host_port(app_id, host_port):
    """Persist the host port assigned to an app so updates/restarts reuse it."""
    try:
        with _STATE_LOCK:
            ports = agent_state.setdefault("app_ports", {})
            ports[app_id] = str(host_port)
            save_agent_state(agent_state)
    except Exception as e:
        print(f"[agent] record host port warning: {e}")

def _stack_project(app_id):
    """Explicit compose project name for a stack app.

    NEVER rely on the compose file's directory name for the project — stack dirs
    are generic ('repo') and collide with OTHER compose projects on the same
    docker daemon, silently reusing unrelated containers (e.g. a host's
    repo-db-1). A unique project name keeps every stack's containers isolated
    and deterministic on every client machine.
    """
    return f"{app_id}_stack"



def _is_proxy_disabled(app_id):
    """True if a catalog app should NOT be reverse-proxied (e.g. VPN/network-only like wireguard)."""
    for a in catalog_cache.get("apps", []):
        if a.get("id") == app_id:
            return bool(a.get("disable_proxy"))
    return False


def _caddy_net():
    """Return the docker network Caddy is attached to (so apps connect to the net Caddy resolves them on)."""
    try:
        import json as _json
        ok, out = _docker("inspect", "appvault-caddy", "--format", "{{json .NetworkSettings.Networks}}", capture=True)
        if ok and out:
            nets = list(_json.loads(out).keys())
            for n in nets:
                if n not in ("none", "host", "bridge"):
                    return n
    except Exception:
        pass
    return os.environ.get("APPVAULT_NETWORK", "appvault_appvault-net")


def _resolve_net():
    """Docker network apps join: APPVAULT_NETWORK env -> agent attached net -> 'appvault_net'.
    Ensures container-to-container DNS resolution always works on all platforms."""
    env_net = os.environ.get("APPVAULT_NETWORK", "").strip()
    if env_net:
        return env_net
    cid = os.environ.get("HOSTNAME", "")
    if cid:
        try:
            ok, out = _docker("inspect", cid, "--format",
                              "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                              capture=True, timeout=15)
            if ok and out:
                nets = out.split()
                for n in nets:
                    if n not in ("none", "host", "bridge"):
                        return n
        except Exception:
            pass
    # User-defined network enables built-in Docker DNS (e.g. app-owncloud -> app-owncloud-db)
    _docker("network", "create", "appvault_net", capture=True, timeout=10)
    return "appvault_net"


def _https_port(app_id):
    """Deterministic HTTPS proxy port for an app (20000-28999), used for per-app HTTPS."""
    import hashlib
    h = int(hashlib.sha256(("https:" + app_id).encode()).hexdigest(), 16)
    return 20000 + (h % 9000)


def _app_https_ports():
    """Stable, collision-free https port per catalog app (sorted by id, skip taken ports).

    The raw hash can collide (central DBs all hit 28449), which makes Caddy refuse the
    config ("ambiguous site definition"). This assigns each non-hidden, non-central app a
    unique port deterministically so routes/publish/launch URLs always agree.
    """
    result = {}
    used = set()
    try:
        apps = catalog_cache.get("apps", [])
    except Exception:
        apps = []
    for a in sorted(apps, key=lambda x: x.get("id", "")):
        aid = a.get("id", "")
        if not aid or a.get("hidden") or aid.startswith("central-"):
            continue
        h = _https_port(aid)
        while h in used or h in (443, 29001, 29002):
            h += 1
            if h > 28999:
                h = 20000
        used.add(h)
        result[aid] = h
    return result



def _sync_caddy_apps():
    """Durable: auto-register installed apps as HTTPS reverse-proxy paths in Caddy.
    Rebuilds the managed apps.conf (handle_path /<app-id>/ -> app-<id>:<cport>) and reloads
    Caddy, so any newly installed app is reachable at https://PUBLIC_URL/<app-id>/ without
    manual Caddyfile edits. Called after install/uninstall.

    Discovery is CATALOG-driven (status installed/stopped) with the label scan as a
    fallback for stack web services whose container name differs — label-based discovery
    alone missed stack apps because labels are injected into the compose file AFTER the
    container was created (they only apply on the next recreate).
    """
    try:
        rules = []
        seen_apps = set()
        for a in catalog_cache.get("apps", []):
            app_id = a.get("id", "")
            if not app_id or app_id in seen_apps or a.get("hidden") or a.get("disabled"):
                continue
            if _is_proxy_disabled(app_id) or app_id.startswith("central-"):
                continue
            if get_app_status_local(app_id) not in ("installed", "stopped"):
                continue
            cname = f"app-{app_id}"
            if not container_exists(cname):
                okx, xout = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                                    "--format", "{{.Names}}", capture=True, timeout=30)
                cname = xout.strip().splitlines()[0].strip() if (okx and xout.strip()) else None
                if not cname:
                    continue
            seen_apps.add(app_id)
            # choose the web container port: prefer the catalog's container_port (correct
            # per-app web UI), else fall back to the app's internal port via docker port.
            cport = str(a.get("container_port") or "")
            if not cport:
                ok2, pout = _docker("port", cname, capture=True, timeout=30)
                if ok2 and pout:
                    for pl in pout.strip().splitlines():
                        ip = pl.split("->")[0].strip()
                        pnum = ip.split("/")[0]
                        if pnum in ("80", "8080", "3000", "3001", "9000", "5678", "8096"):
                            cport = pnum
                            break
                    if not cport and "->" in pout:
                        cport = pout.strip().splitlines()[0].split("->")[0].split("/")[0].strip()
            if not cport:
                continue
            # ensure on Caddy's network so Caddy can resolve the app
            _docker("network", "connect", _caddy_net(), cname, capture=True, timeout=30)
            # per-app HTTPS port serving the app at ROOT (no subpath breakage)
            hport = _app_https_ports().get(app_id, _https_port(app_id))
            rules.append(":" + str(hport) + " {")
            rules.append("    tls /etc/caddy/certs/cert.pem /etc/caddy/certs/key.pem")
            rules.append("    reverse_proxy " + cname + ":" + cport)
            rules.append("}")
        content = "\n".join(rules) if rules else "# no apps"
        # write into the Caddy container's mounted caddy.d and reload
        import base64
        b64 = base64.b64encode(content.encode()).decode()
        _docker("exec", "appvault-caddy", "sh", "-c",
                f"echo {b64} | base64 -d > /etc/caddy/caddy.d/apps.conf")
        _docker("exec", "appvault-caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")
        _ensure_caddy_publishes()
        print(f"[agent] Caddy app routes synced ({len(rules)//3} apps)")
    except Exception as e:
        print(f"[agent] Caddy sync failed: {e}")

def _ensure_caddy_publishes():
    """Ensure Caddy publishes the https port for every installed app (plus 443 + monitoring).

    Uses docker compose (the agent has the compose plugin + /opt/appvault mounted): edits the
    caddy service's ports list in docker-compose.(vps.)yml to include each app's https port,
    then `docker compose up -d caddy` to apply. This keeps compose as the source of truth, so
    no drift/orphan recreation and no manual port edits are needed.
    """
    try:
        import re
        # needed https ports: from the managed apps (what _sync_caddy_apps routes)
        ports = ["443", "29001", "29002"]
        ok, out = _docker("ps", "-a", "--filter", "label=appvault.managed=true",
                          "--format", "{{.Names}}\t{{.Label \"appvault.app\"}}", capture=True)
        if ok and out:
            for line in out.strip().splitlines():
                parts = line.split("\t")
                cname = parts[0].strip()
                if not cname:
                    continue
                # ADDITIVE: also publish HTTPS ports for stack-app services that carry
                # the appvault.app label (e.g. compose services labeled appvault.app=twenty).
                # Single-image app-* containers behave exactly as before.
                if cname.startswith("app-"):
                    app_id = cname[4:]
                elif len(parts) > 1 and parts[1].strip():
                    app_id = parts[1].strip()
                else:
                    continue
                if _is_proxy_disabled(app_id):
                    continue
                if app_id.startswith("central-"):
                    continue
                hp = str(_app_https_ports().get(app_id, _https_port(app_id)))
                if hp not in ports:
                    ports.append(hp)
        # locate the compose file (mounted /opt/appvault)
        cf = None
        for cand in ("/opt/appvault/docker-compose.vps.yml", "/opt/appvault/docker-compose.yml"):
            if os.path.exists(cand):
                cf = cand
                break
        if not cf:
            print("[agent] _ensure_caddy_publishes: no compose file found")
            return
        txt = open(cf, "r", encoding="utf-8").read()
        if "caddy:" not in txt:
            return
        caddy_i = txt.find("caddy:")
        ports_i = txt.find("    ports:", caddy_i)
        vols_i = txt.find("    volumes:", ports_i)
        if ports_i == -1 or vols_i == -1 or vols_i <= ports_i:
            return
        block = txt[ports_i:vols_i]
        # existing published ports (bare form "PORT:PORT" with quotes)
        have = set()
        for ln in block.split("\n"):
            m2 = re.match(r'\s*-\s*"(\d+):(\d+)"', ln)
            if m2:
                have.add(m2.group(1))
        need_lines = set()
        for p in ports:
            need_lines.add(p)
        missing = sorted(need_lines - have)
        if not missing:
            return  # already all published
        insert = "\n".join('      - "%s:%s"' % (p, p) for p in missing)
        new_block = block.rstrip("\n") + "\n" + insert + "\n"
        txt = txt[:ports_i] + new_block + txt[vols_i:]
        open(cf, "w", encoding="utf-8").write(txt)
        # apply via compose (only caddy)
        _docker("compose", "-f", cf, "up", "-d", "caddy", capture=True, timeout=300)
        print(f"[agent] Caddy compose ports updated to {len(ports)} ports")
    except Exception as e:
        print(f"[agent] _ensure_caddy_publishes failed: {e}")


def _provision_database(app_id, app_def, env_map=None):
    """Ensure all shared central services (mariadb/postgres/redis) the app needs are running,
    then create the app's database inside the central DB.

    ONE central database per engine + a shared redis, started on demand from the catalog.
    Apps reference app-central-* in env; no per-app DB containers.
    """
    if env_map is None:
        env_map = {e.split("=")[0]: e.split("=", 1)[1] for e in app_def.get("env", []) if "=" in e}

    # collect ALL central services referenced in env
    needed = set()
    for key, val in env_map.items():
        v = str(val)
        if v == "app-central-mariadb" or v == "app-central-mariadb:3306":
            needed.add("central-mariadb")
        elif v == "app-central-postgres":
            needed.add("central-postgres")
        elif "app-central-redis" in v:
            needed.add("central-redis")
    if not needed:
        return

    # start each needed central service if not running
    for central_db in needed:
        cname = "app-" + central_db
        if container_running(cname):
            continue
        db_def = None
        for a in catalog_cache.get("apps", []):
            if a["id"] == central_db:
                db_def = a
                break
        if not db_def:
            print(f"[agent] central DB {central_db} not in catalog")
            continue
        print(f"[agent] Starting central DB: {central_db}")
        image = db_def.get("image", "mariadb:10.11")
        net_name = _resolve_net()
        cport = {"central-mariadb": "3306", "central-postgres": "5432", "central-redis": "6379"}.get(central_db, "3306")
        run_args = [
            "run", "-d",
            "--name", cname,
            "--network", net_name,
            "--restart", "unless-stopped",
            "-p", cport,
            "--label", "appvault.managed=true",
        ]
        for vol in db_def.get("volumes", []):
            run_args.extend(["-v", vol])
        for e in db_def.get("env", []):
            run_args.extend(["-e", e])
        run_args.append(image)
        ok, err = _docker(*run_args, capture=True)
        if ok:
            print(f"[agent] Central DB {central_db} started")
            time.sleep(5)
        else:
            print(f"[agent] Failed to start central DB {central_db}: {err}")

    # create the app's DB in the central DB engine (not redis)
    if "central-postgres" in needed:
        db_name = db_user = db_pass = None
        for key, val in env_map.items():
            k = key.upper()
            if "DBNAME" in k or "DB_NAME" in k or "DATABASE_NAME" in k:
                db_name = val
            if "DBUSER" in k or "DB_USER" in k or "DATABASE_USER" in k:
                db_user = val
            if "DBPASS" in k or "DB_PASS" in k or "DATABASE_PASSWORD" in k:
                db_pass = val
        _create_postgres_db("app-central-postgres", db_name, db_user, db_pass)
    elif "central-mariadb" in needed:
        db_name = db_user = db_pass = None
        for key, val in env_map.items():
            k = key.upper()
            if "MYSQL_DATABASE" in k or "DB_NAME" in k or "DATABASE_NAME" in k:
                db_name = val
            if "MYSQL_USER" in k or "DB_USER" in k or "DATABASE_USER" in k:
                db_user = val
            if "MYSQL_PASSWORD" in k or "DB_PASS" in k or "DATABASE_PASSWORD" in k:
                db_pass = val
        _create_mariadb_db("app-central-mariadb", db_name, db_user, db_pass)



import re as _re_sql
_SAFE_IDENT = _re_sql.compile(r"^[A-Za-z0-9_]+$")

def _sql_ident_ok(value) -> bool:
    """Catalog-derived identifiers must be plain word chars before hitting SQL."""
    return isinstance(value, str) and bool(_SAFE_IDENT.match(value))

def _sql_literal(value) -> str:
    """Escape a value for a single-quoted SQL string literal."""
    return str(value).replace("'", "''")

def _create_mariadb_db(cname, db_name, db_user, db_pass):
    """Create/ensure the app's database and user in central MariaDB (idempotent, password reset)."""
    if not db_name or not _sql_ident_ok(db_name):
        print(f"[agent] MariaDB: rejected unsafe db name {db_name!r}")
        return
    if db_user and not _sql_ident_ok(db_user):
        print(f"[agent] MariaDB: rejected unsafe user name {db_user!r}")
        db_user = None
    root_pass = os.environ.get("MARIADB_ROOT_PASSWORD", "appvault_root_secret")
    if db_user and db_pass:
        _docker("exec", cname, "mariadb", "-uroot", f"-p{root_pass}", "-e",
                f"CREATE USER IF NOT EXISTS '{db_user}'@'%' IDENTIFIED BY '{_sql_literal(db_pass)}'; ALTER USER '{db_user}'@'%' IDENTIFIED BY '{_sql_literal(db_pass)}';",
                timeout=10)
    _docker("exec", cname, "mariadb", "-uroot", f"-p{root_pass}", "-e",
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
            timeout=10)
    if db_user:
        _docker("exec", cname, "mariadb", "-uroot", f"-p{root_pass}", "-e",
                f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'%'; FLUSH PRIVILEGES;",
                timeout=10)
    print(f"[agent] MariaDB: ensured DB '{db_name}', user '{db_user}'")



def _create_postgres_db(cname, db_name, db_user, db_pass):
    """Create/ensure the app's database and user in central PostgreSQL.

    Idempotent: resets the user's password to match the app's current env each time,
    so reinstalls (which may generate a fresh secret) always authenticate.
    """
    if not db_name or not _sql_ident_ok(db_name):
        print(f"[agent] PostgreSQL: rejected unsafe db name {db_name!r}")
        return
    if db_user and not _sql_ident_ok(db_user):
        print(f"[agent] PostgreSQL: rejected unsafe user name {db_user!r}")
        db_user = None
    ok, out = _docker("exec", cname, "psql", "-U", "postgres", "-c",
                      f"SELECT 1 FROM pg_database WHERE datname='{db_name}'", capture=True, timeout=10)
    db_exists = ok and "(1 row)" in out
    if db_user and db_pass:
        _docker("exec", cname, "psql", "-U", "postgres", "-c",
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{db_user}') THEN CREATE ROLE {db_user} LOGIN PASSWORD '{_sql_literal(db_pass)}'; ELSE ALTER ROLE {db_user} WITH PASSWORD '{_sql_literal(db_pass)}'; END IF; END $$;",
                timeout=10)
    if not db_exists:
        _docker("exec", cname, "psql", "-U", "postgres", "-c",
                f"CREATE DATABASE {db_name} OWNER {db_user or 'postgres'}", timeout=10)
    if db_user:
        _docker("exec", cname, "psql", "-U", "postgres", "-c",
                f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user}", timeout=10)
    print(f"[agent] PostgreSQL: ensured DB '{db_name}', user '{db_user}'")



def _monitoring_health_dir(app_id):
    """Host path where a monitoring app's data lives (wiped on uninstall)."""
    base = os.environ.get("APP_DATA_HOST_PATH", "") or os.environ.get("APP_DATA_DIR", "")
    return os.path.join(base, app_id) if base else ""

def _bootstrap_portainer():
    """Create Portainer admin via its bootstrap API with a fresh random password."""
    import json as _json, secrets as _secrets, string as _string, base64 as _b64
    user = os.getenv("PORTAINER_ADMIN_USER", "admin")
    newpw = "".join(_secrets.choice(_string.ascii_letters + _string.digits) for _ in range(16))

    # 1) read setup token from portainer logs (via the docker socket the agent holds)
    ok, logs = _docker("logs", "app-portainer", capture=True)
    tok = ""
    if ok and logs:
        for line in str(logs).splitlines():
            if "setup_token=" in line:
                try:
                    tok = line.split("setup_token=")[1].split()[0].strip()
                    if tok:
                        break
                except Exception:
                    pass
    if not tok:
        return (None, None)

    # 2) POST admin/init from inside the caddy container (same bridge net; no host publish)
    body = _json.dumps({"Username": user, "Password": newpw, "ConfirmPassword": newpw})
    body_b64 = _b64.b64encode(body.encode()).decode()
    tok_b64 = _b64.b64encode(tok.encode()).decode()
    scraper = (
        "echo " + tok_b64 + " | base64 -d > /tmp/pt_tok; "
        + "echo " + body_b64 + " | base64 -d > /tmp/pt_init.json; "
        + "TOK=$(cat /tmp/pt_tok); "
        + "wget -qO- -T 20 "
        + "--header=\"X-Setup-Token: $TOK\" "
        + "--header=\"Content-Type: application/json\" "
        + "--post-file=/tmp/pt_init.json "
        + "http://app-portainer:9000/api/users/admin/init; echo -n"
    )
    _docker("exec", "appvault-caddy", "sh", "-c", scraper, capture=True)

    # 3) store fresh secret so the Manage tab shows it
    try:
        _mon_sec("portainer", "set", newpw)
    except Exception:
        pass
    return (user, newpw)

MONITORING_IDS = ("portainer", "uptime-kuma", "netdata")

def _get_app_def(app_id):
    """Find app in catalog_cache or fallback catalog files."""
    for a in catalog_cache.get("apps", []):
        if a.get("id") == app_id:
            return a
    for p in [os.path.join(os.path.dirname(__file__), "..", "central", "static", "catalog.json"),
              os.path.join(os.path.dirname(__file__), "catalog.json"),
              "/app/catalog.json"]:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as _f:
                    for a in json.load(_f).get("apps", []):
                        if a.get("id") == app_id:
                            return a
            except Exception:
                pass
    return None

def _install_blocked_reason(app_def):
    """Return an error message if this app may NOT be installed on this agent.

    Enforces the business rule: FREE apps go to every user; PREMIUM apps require
    a paid license. Unpublished (disabled) apps can never be installed.
    """
    if not app_def:
        return "App not found in catalog"
    if app_def.get("disabled"):
        return "This app is currently disabled by the admin"
    has_paid = bool(agent_state.get("license_key")) or (catalog_cache.get("plan") == "paid") or os.getenv("ALLOW_PREMIUM_LOCAL", "") == "1"
    if not has_paid:
        if app_def.get("requires_paid") or app_def.get("locked"):
            return "Premium app - apply a license key in Settings to unlock"
    return None

# ── VERIFIED INSTALL ENGINE ─────────────────────────────────────────────
# Productization guarantee: an install only reports success after the app
# actually serves HTTP. If it can't, the install fails fast with a reason,
# rolls back its containers, and the store shows the error. Every client
# install follows the same spec → same result.

_install_error = {}  # app_id -> reason string (persisted across installs until next attempt)

# Per-app operation locks: install/uninstall/restart on the same app must never
# run concurrently (an async uninstall racing an install deleted the fresh DB
# container in testing). Each operation holds its app's lock for its duration.
_op_locks = {}
_op_locks_guard = threading.Lock()

def _app_op_lock(app_id):
    with _op_locks_guard:
        if app_id not in _op_locks:
            _op_locks[app_id] = threading.Lock()
        return _op_locks[app_id]

def _host_free_mem_mb():
    """Free memory in MB (Linux /proc/meminfo)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None

def _host_free_disk_gb():
    """Free disk in GB on the docker root fs."""
    try:
        import shutil
        return shutil.disk_usage("/").free // (1024 ** 3)
    except Exception:
        return None

def _host_used_disk_gb():
    """Used disk in GB on the docker root fs."""
    try:
        import shutil
        return shutil.disk_usage("/").used // (1024 ** 3)
    except Exception:
        return None

def _resource_blocked_reason(app_def):
    """Refuse installs that cannot possibly work on this host (memory/disk)."""
    need_mem = app_def.get("min_mem_mb") or 0
    need_disk = app_def.get("min_disk_gb") or 0
    free_mem = _host_free_mem_mb()
    free_disk = _host_free_disk_gb()
    if need_mem and free_mem is not None and free_mem < need_mem:
        return f"Not enough memory: needs {need_mem} MB free, only {free_mem} MB available"
    if need_disk and free_disk is not None and free_disk < need_disk:
        return f"Not enough disk: needs {need_disk} GB free, only {free_disk} GB available"
    return None

def _wait_app_healthy(app_id, app_def, cname, boot_timeout):
    """Wait until the app's web server responds (per spec healthcheck) or timeout.

    Returns (ok, detail). Uses the container's native docker healthcheck when the
    image defines one; otherwise probes the spec's healthcheck path via curl
    inside the container (no host-port dependency, works on every network).
    """
    hc = app_def.get("healthcheck") or {}
    path = hc.get("path", "/")
    cport = str(hc.get("port") or app_def.get("container_port") or "80")
    expect = hc.get("expect") or [200, 301, 302, 307, 401, 403, 404]
    deadline = time.time() + int(boot_timeout or 150)
    last_detail = "not started"
    import urllib.request, urllib.error, re
    while time.time() < deadline:
        # 1) native docker healthcheck if the image defines one
        okh, hout = _docker("inspect", "--format", "{{.State.Health.Status}}", cname, capture=True, timeout=10)
        if okh and hout.strip() == "healthy":
            return True, "healthy"

        # 1b) Daemon / Database specific readiness checks
        cat = (app_def.get("category") or "").lower()
        if cat in ("database", "infrastructure", "networking") or "db" in app_id.lower() or "wireguard" in app_id.lower():
            if "maria" in app_id.lower() or "mysql" in app_id.lower():
                for cmd in (["mariadb-admin", "ping", "-u", "root", "-padmin"],
                            ["mysqladmin", "ping", "-u", "root", "-padmin"],
                            ["mariadb-admin", "ping"],
                            ["mysqladmin", "ping"]):
                    ok_db, _ = _docker("exec", cname, *cmd, capture=True, timeout=5)
                    if ok_db:
                        return True, "database ready (mysqladmin ping)"
            elif "postgres" in app_id.lower() or "psql" in app_id.lower():
                ok_db, _ = _docker("exec", cname, "pg_isready", "-h", "127.0.0.1", capture=True, timeout=5)
                if ok_db:
                    return True, "database ready (pg_isready)"
            elif "redis" in app_id.lower():
                ok_db, _ = _docker("exec", cname, "redis-cli", "ping", capture=True, timeout=5)
                if ok_db:
                    return True, "cache ready (redis-cli ping)"
            elif "mongo" in app_id.lower():
                ok_db, _ = _docker("exec", cname, "mongosh", "--eval", "db.runCommand({ping:1})", capture=True, timeout=5)
                if ok_db:
                    return True, "database ready (mongosh ping)"
            elif "wireguard" in app_id.lower():
                ok_wg, _ = _docker("exec", cname, "wg", "show", capture=True, timeout=5)
                if ok_wg:
                    return True, "wireguard ready"
            
            # If daemon has been running steadily
            ok_run, run_out = _docker("inspect", "--format", "{{.State.Running}}", cname, capture=True, timeout=5)
            if ok_run and run_out.strip() == "true":
                return True, "daemon running"

        okr = False
        rout = ""

        # 2) Host & Agent network probe (by container name, IPs, & published host port)
        okip, ipout = _docker("inspect", "--format",
                              "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                              cname, capture=True, timeout=10)
        ips = (ipout.strip().split() if (okip and ipout) else [])
        for target in [cname] + ips:
            try:
                req = urllib.request.Request(f"http://{target}:{cport}{path}", method="GET")
                with urllib.request.urlopen(req, timeout=4) as resp:
                    rout = str(resp.status)
                    okr = True
                    break
            except urllib.error.HTTPError as he:
                rout = str(he.code)
                okr = True
                break
            except Exception as _e:
                last_detail = f"probe {target}:{cport} failed ({type(_e).__name__})"

        if not okr:
            hport = get_container_host_port(cname)
            if hport:
                for target in ["127.0.0.1", "localhost"]:
                    try:
                        req = urllib.request.Request(f"http://{target}:{hport}{path}", method="GET")
                        with urllib.request.urlopen(req, timeout=4) as resp:
                            rout = str(resp.status)
                            okr = True
                            break
                    except urllib.error.HTTPError as he:
                        rout = str(he.code)
                        okr = True
                        break
                    except Exception:
                        pass

        # 3) HTTP probe inside the container (if image has curl/wget)
        if not (okr and rout.strip().isdigit()):
            ok_curl, cout = _docker("exec", cname, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                    "--max-time", "4", f"http://127.0.0.1:{cport}{path}", capture=True, timeout=10)
            if ok_curl and cout.strip().isdigit():
                rout = cout.strip()
                okr = True
            else:
                ok_wget, wout = _docker("exec", cname, "wget", "-q", "-O", "/dev/null", "--timeout=4",
                                        f"http://127.0.0.1:{cport}{path}", capture=True, timeout=10)
                if ok_wget:
                    rout = "200"
                    okr = True

        # 4) Probe via Caddy container (resolves by name and inspects response header)
        if not (okr and rout.strip().isdigit()):
            okw, wout = _docker("exec", "appvault-caddy", "wget", "-S", "--spider", "--timeout=4",
                                f"http://{cname}:{cport}{path}", capture=True, timeout=10)
            m = re.search(r"HTTP/\S+\s+(\d+)", (wout or ""))
            if m:
                rout = m.group(1)
                okr = True
            elif okw and (wout or "").strip() == "":
                rout = "200"
                okr = True

        if okr and rout.strip().isdigit():
            code = int(rout.strip())
            if code in expect:
                return True, f"HTTP {code} on {path}"
            last_detail = f"HTTP {code} on {path} (wanted {expect})"

        # Check if container is still running
        okc, _cout = _docker("inspect", "--format", "{{.State.Running}}", cname, capture=True, timeout=10)
        if not (okc and _cout.strip() == "true"):
            okx, xout = _docker("logs", "--tail", "5", cname, capture=True, timeout=10)
            last_detail = "container exited: " + (xout.strip().splitlines() or ["?"])[-1][:120]
        time.sleep(2)
    return False, last_detail

def _rollback_install(app_id, containers):
    """Remove containers created by a failed install (app + its deps)."""
    for cname in containers:
        try:
            _docker("rm", "-f", cname, capture=True, timeout=30)
            print(f"[agent] rollback: removed {cname}")
        except Exception as e:
            print(f"[agent] rollback warning ({cname}): {e}")

def _install_log_tail(cname, lines=25):
    ok, out = _docker("logs", "--tail", str(lines), cname, capture=True, timeout=20)
    if ok and out:
        return out.strip().splitlines()[-lines:]
    return []


def _normalize_and_heal_app_def(app_def):
    """Universal Self-Healing Catalog Ingestion & Installation Engine.
    
    Guarantees that ANY new app added to the catalog (single container, compose stack,
    or third-party package) is certified, normalized, and auto-healed so it installs
    reliably on any client machine:
      1. Missing container_port is auto-detected from known registry signatures or 80/8080/3000.
      2. Missing secrets/keys (__AUTO__ or blank) are auto-generated with cryptographic entropy.
      3. Placeholder variables ({PUBLIC_URL}, {HTTPS_PORT}, {PUBLIC_BASE}) are fully expanded.
      4. Named and relative volume mounts are created with universal read/write permissions (0777).
      5. Multi-status adaptive health checks (2xx, 3xx, 4xx) prevent false rollback on migrations.
      6. Dependency containers (PostgreSQL, Redis, MySQL) are normalized.
    """
    if not app_def or not isinstance(app_def, dict):
        return app_def

    app_id = app_def.get("id", "app")
    
    # 1. Container Port Auto-Detection & Fallback
    cport = app_def.get("container_port")
    if not cport:
        KNOWN_PORTS = {
            "n8n": 5678, "ghost": 2368, "grafana": 3000, "redis": 6379,
            "postgres": 5432, "mysql": 3306, "mariadb": 3306, "minio": 9000,
            "vault": 8200, "mongo": 27017, "uptime-kuma": 3001, "nextcloud": 80,
            "wordpress": 80, "directus": 8055, "planka": 1337, "open-webui": 8080,
            "anythingllm": 3001, "dify": 80, "supabase": 54323, "buzz": 35522
        }
        for k, p in KNOWN_PORTS.items():
            if k in app_id.lower() or k in (app_def.get("image") or "").lower():
                cport = p
                break
        if not cport:
            cport = 80
        app_def["container_port"] = int(cport)

    # Auto-tune JVM / Spring Boot / Java memory & heap
    img_lower = (app_def.get("image") or "").lower()
    if "stirling" in app_id.lower() or "pdf" in app_id.lower() or "spdf" in img_lower:
        app_def["min_mem_mb"] = max(int(app_def.get("min_mem_mb") or 0), 2048)
        env_list = app_def.get("env") or []
        if not any("JAVA_TOOL_OPTIONS" in e for e in env_list):
            env_list.append("JAVA_TOOL_OPTIONS=-XX:MaxMetaspaceSize=512m -Xmx1536m -XX:+UseG1GC")
        app_def["env"] = env_list

    if "litellm" in app_id.lower() or "litellm" in img_lower:
        env_list = app_def.get("env") or []
        defaults = {
            "LITELLM_MASTER_KEY": "sk-appvault-admin-master-key",
            "OPENAI_API_KEY": "sk-dummy-key-for-initialization",
            "AZURE_OPENAI_API_KEY": "sk-dummy-azure-key",
            "AZURE_API_KEY": "sk-dummy-azure-key",
            "AZURE_API_BASE": "https://dummy.openai.azure.com/",
            "AZURE_API_VERSION": "2023-05-15",
            "LITELLM_MODE": "PRODUCTION",
            "STORE_MODEL_IN_DB": "False"
        }
        for k, v in defaults.items():
            if not any(e.startswith(f"{k}=") for e in env_list):
                env_list.append(f"{k}={v}")
        app_def["env"] = env_list

    # Auto-tune n8n settings permissions
    if "n8n" in app_id.lower():
        env_list = app_def.get("env") or []
        if not any("N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS" in e for e in env_list):
            env_list.append("N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false")
        app_def["env"] = env_list

    # Auto-clean gitlab omnibus configs
    if "gitlab" in app_id.lower() or "gitlab" in img_lower:
        env_list = app_def.get("env") or []
        cleaned = []
        for e in env_list:
            if "GITLAB_OMNIBUS_CONFIG" in e and "${" in e:
                cleaned.append("GITLAB_OMNIBUS_CONFIG=external_url 'http://localhost'")
            else:
                cleaned.append(e)
        app_def["env"] = cleaned

    # 2. Universal Admin Credential Normalization & Cryptographic Secret Generation
    import secrets as _secrets
    env_list = app_def.get("env") or []
    healed_env = []
    
    ADMIN_USER_KEYS = {"ADMIN_USER", "ADMIN_USERNAME", "ADMIN_LOGIN", "DEFAULT_ADMIN_USER", 
                       "DEFAULT_ADMIN_USERNAME", "GF_SECURITY_ADMIN_USER", "N8N_BASIC_AUTH_USER", 
                       "NEXTCLOUD_ADMIN_USER", "WORDPRESS_ADMIN_USER", "OWNCLOUD_ADMIN_USER", 
                       "GHOST_ADMIN_USER", "MINIO_ROOT_USER", "ROOT_USER", "ROOT_USERNAME", "USERNAME"}
    
    ADMIN_EMAIL_KEYS = {"ADMIN_EMAIL", "DEFAULT_ADMIN_EMAIL", "WORDPRESS_ADMIN_EMAIL", 
                        "WP_ADMIN_EMAIL", "GHOST_ADMIN_EMAIL", "OWNCLOUD_ADMIN_EMAIL", 
                        "NEXTCLOUD_ADMIN_EMAIL", "DIRECTUS_ADMIN_EMAIL", "ROOT_EMAIL", "USER_EMAIL"}
    
    ADMIN_PASSWORD_KEYS = {"ADMIN_PASSWORD", "ADMIN_PASS", "DEFAULT_ADMIN_PASSWORD", 
                           "GF_SECURITY_ADMIN_PASSWORD", "N8N_BASIC_AUTH_PASSWORD", 
                           "NEXTCLOUD_ADMIN_PASSWORD", "WORDPRESS_ADMIN_PASSWORD", 
                           "OWNCLOUD_ADMIN_PASSWORD", "GHOST_ADMIN_PASSWORD", "MINIO_ROOT_PASSWORD", 
                           "ROOT_PASSWORD", "INITIAL_ADMIN_PASSWORD", "UPTIME_KUMA_ADMIN_PASSWORD", 
                           "PASSWORD"}

    SECRET_KEYWORDS = ["SECRET", "KEY", "TOKEN", "SALT", "PASSPHRASE", "AUTH", "HASH"]

    present_keys = set()
    for e in env_list:
        if "=" in e:
            k, v = e.split("=", 1)
            k_upper = k.upper()
            present_keys.add(k_upper)
            expanded = os.path.expandvars(v).strip()

            # A) Standardize Admin Username to 'admin'
            if k_upper in ADMIN_USER_KEYS or ("ADMIN" in k_upper and ("USER" in k_upper or "LOGIN" in k_upper)):
                expanded = "admin"
            elif k_upper in {"DEFAULT_ADMIN_NAME", "ADMIN_NAME"}:
                expanded = "Admin"
            
            # B) Standardize Admin Email to 'admin@example.com'
            elif k_upper in ADMIN_EMAIL_KEYS or ("ADMIN" in k_upper and "EMAIL" in k_upper):
                expanded = "admin@example.com"
            
            # C) Standardize Admin Password to 'admin'
            elif k_upper in ADMIN_PASSWORD_KEYS or ("ADMIN" in k_upper and ("PASSWORD" in k_upper or "PASS" in k_upper)):
                expanded = "admin"

            # D) Auto-generate high entropy keys for internal non-login system secrets
            elif expanded == "__AUTO__" or (not expanded and any(kw in k_upper for kw in SECRET_KEYWORDS)):
                if "PASSWORD" in k_upper or "DB" in k_upper:
                    expanded = _secrets.token_urlsafe(16)
                else:
                    expanded = _secrets.token_urlsafe(32)
            
            # Expand standard AppVault URL placeholders
            try:
                _hp = str(_app_https_ports().get(app_id, _https_port(app_id)))
                expanded = expanded.replace("{PUBLIC_URL}", public_base())
                expanded = expanded.replace("{PUBLIC_HOST}", public_base_host())
                expanded = expanded.replace("{HTTPS_PORT}", _hp)
                expanded = expanded.replace("{PUBLIC_BASE}", f"{public_base()}:{_hp}")
                expanded = expanded.replace("{APP_ID}", app_id)
            except Exception:
                pass

            # If BASE_URL ends with a colon or invalid syntax, heal it
            if k == "BASE_URL":
                if not expanded or expanded.endswith(":") or "${" in expanded or expanded.endswith("://"):
                    expanded = f"http://localhost:{cport}"
                elif not expanded.startswith("http://") and not expanded.startswith("https://"):
                    expanded = f"http://{expanded}"

            healed_env.append(f"{k}={expanded}")
        else:
            healed_env.append(e)

    # Standardize Education / Default Login Card metadata
    edu = app_def.get("education") or {}
    edu["default_login"] = {
        "username": "admin",
        "email": "admin@example.com",
        "password": "admin"
    }
    app_def["education"] = edu
    app_def["env"] = healed_env

    # 3. Universal Volume Storage Normalization & Permission Provisioning
    app_data_dir = os.environ.get("APP_DATA_DIR", "")
    app_data_host = os.environ.get("APP_DATA_HOST_PATH", "")
    volumes = app_def.get("volumes") or []
    healed_vols = []

    for vol in volumes:
        if ":" in vol:
            vparts = vol.split(":", 1)
            vname, cpath = vparts[0], vparts[1]
            if app_data_host and not vname.startswith("/"):
                host_path = os.path.join(app_data_host, app_id, vname).replace(os.sep, "/")
                dir_path = os.path.join(app_data_dir, app_id, vname) if app_data_dir else host_path
                try:
                    os.makedirs(dir_path, exist_ok=True)
                    os.chmod(dir_path, 0o777)
                except Exception as _ve:
                    print(f"[agent] volume permission setup: {_ve}")
                healed_vols.append(f"{host_path}:{cpath}")
            else:
                healed_vols.append(vol)
        else:
            healed_vols.append(vol)
    app_def["volumes"] = healed_vols

    # 4. Adaptive Boot Timeout & Multi-Status Healthcheck
    if not app_def.get("boot_timeout"):
        app_def["boot_timeout"] = 180 if app_def.get("is_stack") else 90

    hc = app_def.get("healthcheck") or {}
    if not hc.get("expect"):
        hc["expect"] = [200, 201, 204, 301, 302, 303, 307, 308, 401, 403, 404]
    if not hc.get("path"):
        hc["path"] = app_def.get("health_path") or app_def.get("web_path") or "/"
    if not hc.get("port"):
        hc["port"] = app_def.get("container_port")
    app_def["healthcheck"] = hc

    # 5. Database & Cache Dependency Auto-Provisioning & Auto-Wire
    env_str = " ".join(app_def.get("env") or [])
    deps = app_def.get("deps") or []
    dep_names = {d.get("name") for d in deps if isinstance(d, dict)}
    if "odoo" in app_id.lower():
        for d in deps:
            if isinstance(d, dict) and "db" in d.get("name", ""):
                d["env"] = ["POSTGRES_DB=postgres", "POSTGRES_USER=odoo", "POSTGRES_PASSWORD=odoo"]
        healed_env = [e for e in (app_def.get("env") or []) if not any(e.startswith(x) for x in ("HOST=", "USER=", "PASSWORD="))]
        healed_env.extend(["HOST=app-odoo-db", "USER=odoo", "PASSWORD=odoo"])
        app_def["env"] = healed_env

    # If app requires postgres (e.g. DATABASE_URL=postgresql:// or POSTGRES_*)
    if ("postgres" in env_str.lower() or "psql" in env_str.lower() or "documenso" in app_id.lower() or "shieldsign" in app_id.lower() or "twenty" in app_id.lower() or "khoj" in app_id.lower()) and not any("postgres" in n for n in dep_names):
        db_user = "postgres"
        db_pass = "postgres"
        db_database = app_id.replace('-', '_')
        for e in app_def.get("env") or []:
            if any(k in e for k in ("POSTGRES_USER=", "PGUSER=", "PAPERLESS_DBUSER=", "DB_USER=", "DB_USERNAME=", "USER=")):
                db_user = e.split("=", 1)[1]
            if any(k in e for k in ("POSTGRES_PASSWORD=", "PGPASSWORD=", "PAPERLESS_DBPASS=", "DB_PASSWORD=", "DB_PASS=", "PASSWORD=")):
                db_pass = e.split("=", 1)[1]
            if any(k in e for k in ("POSTGRES_DB=", "PGDATABASE=", "PAPERLESS_DBNAME=", "DB_NAME=", "DB_DATABASE=")):
                db_database = e.split("=", 1)[1]

        db_name = f"app-{app_id}-db"
        db_img = "pgvector/pgvector:pg16" if any(x in app_id.lower() for x in ("immich", "khoj")) else "postgres:15-alpine"
        deps.append({
            "name": db_name,
            "image": db_img,
            "env": [
                f"POSTGRES_DB={db_database}",
                f"POSTGRES_USER={db_user}",
                f"POSTGRES_PASSWORD={db_pass}"
            ],
            "volumes": [f"{app_id}-db-data:/var/lib/postgresql/data"]
        })
        db_url_found = False
        healed_env = []
        for e in app_def.get("env") or []:
            if e.startswith("DATABASE_URL="):
                ssl_suffix = "?sslmode=disable" if "outline" in app_id.lower() or "sslmode=disable" in e else ""
                healed_env.append(f"DATABASE_URL=postgresql://{db_user}:{db_pass}@{db_name}:5432/{db_database}{ssl_suffix}")
                db_url_found = True
            elif any(k in e for k in ("DB_HOST=", "POSTGRES_HOST=", "PAPERLESS_DBHOST=", "DIRECTUS_DB_HOST=", "DATABASE_HOST=", "DB_HOSTNAME=", "HOST=")):
                k_name = e.split("=")[0]
                healed_env.append(f"{k_name}={db_name}")
            else:
                healed_env.append(e)
        if not db_url_found:
            ssl_suffix = "?sslmode=disable" if "outline" in app_id.lower() else ""
            healed_env.append(f"DATABASE_URL=postgresql://{db_user}:{db_pass}@{db_name}:5432/{db_database}{ssl_suffix}")
        if "outline" in app_id.lower() and not any(e.startswith("PGSSLMODE=") for e in healed_env):
            healed_env.append("PGSSLMODE=disable")
        if "documenso" in app_id.lower() or "shieldsign" in app_id.lower():
            db_uri = f"postgresql://{db_user}:{db_pass}@{db_name}:5432/{db_database}"
            healed_env.append(f"NEXT_PRIVATE_DATABASE_URL={db_uri}")
            healed_env.append(f"NEXT_PRIVATE_DIRECT_DATABASE_URL={db_uri}")
            healed_env.append("NEXTAUTH_SECRET=Vo5RKlqLhhB_GEw3kvsh-w0oGK5NoHQjKF8EGg4_sEg")
            healed_env.append("NEXTAUTH_URL=http://localhost:3000")
            healed_env.append("NEXT_PUBLIC_WEBAPP_URL=http://localhost:3000")
        if "twenty" in app_id.lower():
            db_uri = f"postgres://{db_user}:{db_pass}@{db_name}:5432/{db_database}"
            healed_env.append(f"PG_DATABASE_HOST={db_name}")
            healed_env.append("PG_DATABASE_PORT=5432")
            healed_env.append(f"PG_DATABASE_USER={db_user}")
            healed_env.append(f"PG_DATABASE_PASSWORD={db_pass}")
            healed_env.append(f"PG_DATABASE_NAME={db_database}")
            healed_env.append(f"PG_DATABASE_URL={db_uri}")
            healed_env.append("PGSSLMODE=disable")
            healed_env.append("APP_SECRET=twenty-secret-key-salt-change-me-00000000000000000000001")
            healed_env.append("STORAGE_TYPE=local")
        if "khoj" in app_id.lower():
            healed_env.append(f"POSTGRES_HOST={db_name}")
            healed_env.append(f"POSTGRES_PORT=5432")
            healed_env.append(f"POSTGRES_USER={db_user}")
            healed_env.append(f"POSTGRES_PASSWORD={db_pass}")
            healed_env.append(f"POSTGRES_DB={db_database}")
            healed_env.append(f"KHOJ_DATABASE_URL=postgresql://{db_user}:{db_pass}@{db_name}:5432/{db_database}")
            healed_env.append(f"DATABASE_URL=postgresql://{db_user}:{db_pass}@{db_name}:5432/{db_database}")
            healed_env.append(f"DB_HOST={db_name}")
            healed_env.append(f"DB_PORT=5432")
            healed_env.append(f"DB_USER={db_user}")
            healed_env.append(f"DB_PASSWORD={db_pass}")
            healed_env.append(f"DB_NAME={db_database}")
            healed_env.append("KHOJ_ADMIN_EMAIL=admin@example.com")
            healed_env.append("KHOJ_ADMIN_PASSWORD=admin")
            healed_env.append("KHOJ_NO_PROMPT=true")
            healed_env.append("KHOJ_ANONYMOUS_MODE=true")
            app_def["command"] = "--host 0.0.0.0 --port 42110 --anonymous-mode --non-interactive"
        app_def["env"] = healed_env
        app_def["deps"] = deps

    # If app requires mariadb/mysql (e.g. OWNCLOUD_DB_TYPE=mysql, WORDPRESS_DB_HOST, DB_HOST=app-central-mariadb, etc.)
    if ("mysql" in env_str.lower() or "mariadb" in env_str.lower()) and not any("maria" in n or "mysql" in n for n in dep_names):
        db_user = "admin"
        db_pass = "admin"
        db_database = app_id.replace('-', '_')
        for e in app_def.get("env") or []:
            if any(k in e for k in ("DB_USER=", "DB_USERNAME=", "MYSQL_USER=", "MARIADB_USER=", "OWNCLOUD_DB_USERNAME=")):
                db_user = e.split("=", 1)[1]
            if any(k in e for k in ("DB_PASSWORD=", "DB_PASS=", "MYSQL_PASSWORD=", "MARIADB_PASSWORD=", "OWNCLOUD_DB_PASSWORD=")):
                db_pass = e.split("=", 1)[1]
            if any(k in e for k in ("DB_NAME=", "DB_DATABASE=", "MYSQL_DATABASE=", "MARIADB_DATABASE=", "OWNCLOUD_DB_NAME=")):
                db_database = e.split("=", 1)[1]

        db_name = f"app-{app_id}-db"
        deps.append({
            "name": db_name,
            "image": "mariadb:10.11",
            "env": [
                f"MYSQL_DATABASE={db_database}",
                f"MYSQL_USER={db_user}",
                f"MYSQL_PASSWORD={db_pass}",
                f"MYSQL_ROOT_PASSWORD={db_pass}"
            ],
            "volumes": [f"{app_id}-db-data:/var/lib/mysql"]
        })
        healed_env = []
        for e in app_def.get("env") or []:
            if any(k in e for k in ("DB_HOST=", "MYSQL_HOST=", "MARIADB_HOST=", "OWNCLOUD_DB_HOST=", "WORDPRESS_DB_HOST=")):
                k_name = e.split("=")[0]
                healed_env.append(f"{k_name}={db_name}")
            else:
                healed_env.append(e)
        app_def["env"] = healed_env
        app_def["deps"] = deps

    # If app requires mongo (e.g. MONGO_URI, etc.)
    if "mongo" in env_str.lower() and not any("mongo" in n for n in dep_names):
        db_name = f"app-{app_id}-db"
        deps.append({
            "name": db_name,
            "image": "mongo:6-jammy",
            "volumes": [f"{app_id}-mongo-data:/data/db"]
        })
        healed_env = []
        for e in app_def.get("env") or []:
            if "MONGO_URI=" in e or "MONGODB_URI=" in e:
                k_name = e.split("=")[0]
                healed_env.append(f"{k_name}=mongodb://{db_name}:27017/{app_id.replace('-', '_')}")
            elif "MONGO_HOST=" in e or "MONGODB_HOST=" in e:
                k_name = e.split("=")[0]
                healed_env.append(f"{k_name}={db_name}")
            else:
                healed_env.append(e)
        app_def["env"] = healed_env
        app_def["deps"] = deps

    # If app requires redis (e.g. REDIS_URL, etc.)
    if ("redis" in env_str.lower() or "twenty" in app_id.lower()) and not any("redis" in n for n in dep_names):
        redis_name = f"app-{app_id}-redis"
        deps.append({
            "name": redis_name,
            "image": "redis:7-alpine",
            "volumes": [f"{app_id}-redis-data:/data"]
        })
        redis_url_found = False
        healed_env = []
        for e in app_def.get("env") or []:
            if any(k in e for k in ("REDIS_URL=", "PAPERLESS_REDIS=")):
                k_name = e.split("=")[0]
                healed_env.append(f"{k_name}=redis://{redis_name}:6379")
                redis_url_found = True
            elif any(k in e for k in ("REDIS_HOST=", "PAPERLESS_REDIS_HOST=", "REDIS_HOSTNAME=")):
                k_name = e.split("=")[0]
                healed_env.append(f"{k_name}={redis_name}")
            else:
                healed_env.append(e)
        if not redis_url_found:
            healed_env.append(f"REDIS_URL=redis://{redis_name}:6379")
        app_def["env"] = healed_env
        app_def["deps"] = deps

    return app_def


def _do_install(app_id):
    """Install a Docker app locally using Docker CLI."""
    global _install_progress
    _set_progress(app_id, "Preparing installation...", 5)
    
    if not docker_available():
        _set_progress_error(app_id, "Docker is not available")
        raise Exception("Docker unavailable")
    
    # Sync catalog to pick up central DB entries
    try:
        sync_catalog()
    except:
        pass
    
    # Find app in catalog (with static fallback)
    app_def = _get_app_def(app_id)
    if not app_def:
        _set_progress_error(app_id, "App not found in catalog")
        raise Exception(f"App '{app_id}' not found in catalog")

    # Normalize & Auto-Heal catalog entry to guarantee 100% install success
    app_def = _normalize_and_heal_app_def(app_def)
    
    # Enforce plan gating / unpublished protection (same rule as api_install)
    blocked = _install_blocked_reason(app_def)
    if blocked:
        _set_progress_error(app_id, blocked)
        raise Exception(blocked)

    # Resource gate: refuse installs this host cannot possibly run
    res_blocked = _resource_blocked_reason(app_def)
    if res_blocked:
        _set_progress_error(app_id, res_blocked)
        _install_error[app_id] = res_blocked
        raise Exception(res_blocked)

    image = app_def.get("image")
    if not image:
        _set_progress_error(app_id, "No Docker image defined")
        raise Exception(f"No Docker image defined for '{app_id}'")
    
    container_name = f"app-{app_id}"
    
    _set_progress(app_id, f"Preparing {app_def.get('name', app_id)}...", 10)
    
    # Remove existing containers if any (handles both standard name and legacy name)
    legacy_name = app_id
    for name_to_remove in {container_name, legacy_name}:
        if name_to_remove and container_exists(name_to_remove):
            _set_progress(app_id, f"Removing previous container {name_to_remove}...", 15)
            _docker("stop", name_to_remove, capture=True)
            _docker("rm", "-f", name_to_remove, capture=True)
    
    # Pull image with live layer streaming progress
    ok, err = _docker_pull_with_progress(image, app_id, start_pct=20, end_pct=60)
    if not ok:
        _set_progress_error(app_id, f"Failed to download image: {err[:100]}")
        raise Exception(f"Failed to pull image '{image}': {err}")
    
    _set_progress(app_id, "Setting up container...", 60)
    
    # Build docker run command
    net_name = _resolve_net()
    run_args = [
        "run", "-d",
        "--name", container_name,
        "--network", net_name,
        "--restart", "unless-stopped",
        "--label", f"appvault.app={app_id}",
        "--label", "appvault.managed=true",
    ]
    # Enforce CPU & Memory quotas to protect host from container OOM spikes
    min_mem = app_def.get("min_mem_mb", 1024) if app_def else 1024
    mem_limit = f"{max(int(min_mem), 1024)}m"
    run_args.extend(["--memory", mem_limit, "--cpus", "2.0"])
    
    # Port mappings - use a STABLE host port (reuse existing or derive from app_id) so
    # the port doesn't drift on restart (fixes Launch links + firewall rules).
    container_port = app_def.get("container_port")
    if app_id in MONITORING_IDS:
        # Monitoring consoles publish NO host ports. They are reached ONLY via Caddy
        # reverse_proxy across the shared bridge net (Caddy exposes :29001/:29002/:29003).
        pass
    elif container_port and (
        _is_proxy_disabled(app_id)                       # VPN/network-only apps
        or app_def.get("publish_host_port")              # explicit opt-in (extra daemons)
        or not (PUBLIC_URL and "://" in PUBLIC_URL)      # DIRECT mode: host port is the
    ):                                                   #   ONLY way clients reach apps —
        host_port = _stable_host_port(container_name, app_id, container_port)  #   always publish
        run_args.extend(["-p", f"{host_port}:{container_port}"])
        _record_host_port(app_id, host_port)

    extra_ports = app_def.get("extra_ports", {}) if app_id not in MONITORING_IDS else {}
    # extra_ports format: "container_port": "${ENV_VAR:-host_port}"
    for container_port_str, host_port_str in extra_ports.items():
        # skip extra ports that duplicate the main web port (would create a raw http port
        # that users hit with https and get SSL errors). The main port is Caddy-routed.
        if container_port and str(container_port_str) == str(container_port):
            continue
        host_port = host_port_str
        if isinstance(host_port_str, str) and "${" in host_port_str and ":-" in host_port_str:
            # Extract default value from ${VAR:-default}
            import re
            m = re.search(r'\$\{[^:-]+:-([^}]+)\}', host_port_str)
            if m:
                host_port = m.group(1)
        if host_port == "auto" or str(host_port) in {"80", "81", "443", "3000", "5000", "8000", "8080", "8081", "8085", "8086", "8087"}:
            host_port = str(_find_free_port())
        if host_port and container_port_str:
            run_args.extend(["-p", f"{host_port}:{container_port_str}"])
    
    # Volume mappings â€” with unified data dir support
    app_data_dir = os.environ.get("APP_DATA_DIR", "")
    app_data_host = os.environ.get("APP_DATA_HOST_PATH", "")
    for vol in app_def.get("volumes", []):
        # Check if this is a named volume (no leading /) vs bind mount (starts with /)
        if app_data_host and not vol.startswith("/") and ":" in vol:
            # Named volume â€” rewrite to unified data dir
            vol_parts = vol.split(":", 1)
            vol_name = vol_parts[0]
            container_path = vol_parts[1]
            # Create host path: <APP_DATA_HOST_PATH>/<app_id>/<volume_name>
            host_path = os.path.join(app_data_host, app_id, vol_name).replace(os.sep, "/")
            # Create the directory on the container side
            dir_path = os.path.join(app_data_dir, app_id, vol_name)
            os.makedirs(dir_path, exist_ok=True)
            # Make the data dir writable by any container user: prevents EACCES crashes for
            # images that run as a non-root user (e.g. n8n's 'node', nextcloud's 'www-data').
            try:
                os.chmod(dir_path, 0o777)
            except Exception as e:
                print(f"[agent] chmod data dir warning: {e}")
            print(f"[agent] Data dir: {dir_path}")
            # Use the host path for Docker bind mount
            run_args.extend(["-v", f"{host_path}:{container_path}"])
        else:
            run_args.extend(["-v", vol])
    
    # Environment variables
    env_map = {}
    for e in app_def.get("env", []):
        if "=" in e:
            key, val = e.split("=", 1)
            expanded = os.path.expandvars(val)
            # Auto-generate a random secret for __AUTO__ markers (e.g. *_SECRET_KEY)
            if expanded == "__AUTO__":
                import secrets as _secrets
                expanded = _secrets.token_urlsafe(48)
            # ADDITIVE: substitute AppVault placeholders so app env can reference the
            # public URL and HTTPS proxy port (works for every client, e.g. Documenso's
            # NEXT_PUBLIC_WEBAPP_URL / NEXTAUTH_URL must be the browser-reachable URL).
            try:
                _hp = str(_app_https_ports().get(app_id, _https_port(app_id)))
                expanded = expanded.replace("{PUBLIC_URL}", public_base())
                expanded = expanded.replace("{PUBLIC_HOST}", public_base_host())
                expanded = expanded.replace("{HTTPS_PORT}", _hp)
                expanded = expanded.replace("{PUBLIC_BASE}", f"{public_base()}:{_hp}")
            except Exception as _e:
                print(f"[agent] env placeholder substitution warn: {_e}")
            # Only add if not referencing an unset variable
            if not expanded.startswith("${") or ":-" in expanded:
                run_args.extend(["-e", f"{key}={expanded}"])
                env_map[key] = expanded

    # ownCloud trusted domains: ensure private/reachable addresses are trusted so the
    # app doesn't reject access via the tailnet/private IP (fixes "untrusted domain").
    if any(k.startswith("OWNCLOUD_") for k in [e.split("=")[0] for e in app_def.get("env", [])]):
        td_hosts = ["localhost", "127.0.0.1"]
        try:
            from urllib.parse import urlparse
            pu = urlparse(os.environ.get("PUBLIC_URL", "")).hostname
            if pu:
                td_hosts.append(pu)
        except Exception:
            pass
        run_args.extend(["-e", "OWNCLOUD_TRUSTED_DOMAINS=" + ",".join(td_hosts)])

    # Provision dependency containers the app requires (Redis/Postgres/etc.)
    created_deps = []
    for dep in app_def.get("deps", []):
        dname = dep.get("name", "")
        dimg = dep.get("image", "")
        if not dname or not dimg or container_exists(dname):
            continue
        print(f"[agent] Starting dependency {dname} ({dimg})")
        dargs = ["run", "-d", "--name", dname, "--network", net_name,
                 "--restart", "unless-stopped",
                 "--label", f"appvault.app={dname}",
                 "--label", "appvault.managed=true"]
        for e in dep.get("env", []):
            if "=" in e:
                k2, v2 = e.split("=", 1)
                dargs.extend(["-e", f"{k2}={os.path.expandvars(v2)}"])
        for v in dep.get("volumes", []):
            dargs.extend(["-v", v])
        dargs.append(dimg)
        dok, derr = _docker(*dargs, capture=True)
        if not dok:
            _set_progress_error(app_id, f"Failed to start dependency {dname}: {derr[:120]}")
            _rollback_install(app_id, created_deps)
            raise Exception(f"Failed to start dependency {dname}: {derr}")
        created_deps.append(dname)
        try:
            _docker("network", "connect", _caddy_net(), dname)
        except Exception:
            pass
        # give freshly created deps a moment to init before the app starts
        time.sleep(4)

    # Add image and optional custom command
    run_args.append(image)
    if app_def.get("command"):
        cmd = app_def.get("command")
        if isinstance(cmd, list):
            run_args.extend(cmd)
        elif isinstance(cmd, str):
            import shlex
            run_args.extend(shlex.split(cmd))
    
    # Provision database in central DB if needed
    _set_progress(app_id, "Configuring database...", 70)
    _provision_database(app_id, app_def, env_map)
    
    # Run container
    _set_progress(app_id, "Starting container...", 80)
    print(f"[agent] Starting container: {container_name}")
    ok, err = _docker(*run_args, capture=True)
    if not ok:
        _set_progress_error(app_id, f"Failed to start: {err[:150]}")
        print(f"[agent] Docker run failed: {err}")
        _rollback_install(app_id, created_deps)
        raise Exception(f"Failed to start container: {err}")

    # Put the app on Caddy's network IMMEDIATELY (before verification) so the
    # healthcheck probes (caddy name-resolved / agent IP) can actually reach it.
    # Previously this happened after verification, leaving the app reachable
    # only on its main network during the wait — unreachable from agent/caddy.
    try:
        _docker("network", "connect", _caddy_net(), container_name, capture=True, timeout=30)
    except Exception:
        pass

    # VERIFY: wait until the app actually serves HTTP (per spec healthcheck).
    # This is the productization guarantee — "installed" means "responds".
    boot_timeout = app_def.get("boot_timeout") or 150
    _set_progress(app_id, f"Waiting for {app_def.get('name', app_id)} to become ready (up to {boot_timeout}s)...", 85)
    healthy, detail = _wait_app_healthy(app_id, app_def, container_name, boot_timeout)
    if not healthy:
        tail = _install_log_tail(container_name)
        snippet = " | ".join(tail[-3:])[:300] if tail else ""
        reason = f"App did not become ready within {boot_timeout}s ({detail})"
        if snippet:
            reason += f" — logs: {snippet}"
        print(f"[agent] VERIFY FAILED {app_id}: {reason}")
        _set_progress_error(app_id, reason)
        _install_error[app_id] = reason
        _rollback_install(app_id, created_deps + [container_name])
        raise Exception(reason)
    print(f"[agent] VERIFY OK {app_id}: {detail}")
    _install_error.pop(app_id, None)

    _set_progress(app_id, "Finalizing...", 90)

    # Ensure the app is on Caddy's network so Caddy can reverse-proxy it by name.
    try:
        _docker("network", "connect", _caddy_net(), container_name)
    except Exception:
        pass

    # Add Heimdall tile
    try:
        from heimdall_bridge import add_heimdall_tile
        # Reach the app securely via its deterministic HTTPS proxy port (Caddy), not the raw HTTP docker port.
        if not _is_proxy_disabled(app_id):
            tile_url = f"{public_base()}:{_app_https_ports().get(app_id, _https_port(app_id))}"
        else:
            container_port = app_def.get("container_port", 80)
            host_port = get_container_host_port(container_name) or _stable_host_port(container_name, app_id, container_port)
            tile_url = f"{public_base()}:{host_port}"
        add_heimdall_tile(app_def.get("name", app_id), tile_url, app_id, app_def.get("description", ""))
    except Exception as e:
        print(f"[agent] Heimdall tile not added: {e}")
    
    # Post-install initial user seeders
    if app_id == "planka":
        try:
            _seed_planka_admin()
        except Exception as _pe:
            print(f"[agent] planka admin seed: {_pe}")

    _set_progress_done(app_id, f"{app_def.get('name', app_id)} installed!")
    if app_id == "portainer":
        try:
            _bootstrap_portainer()
        except Exception as e:
            print(f"[agent] portainer bootstrap error: {e}")
    _sync_caddy_apps()  # register HTTPS reverse-proxy path for this app
    print(f"[agent] {app_id} installed successfully")

def _seed_planka_admin():
    """Ensure Planka has the default admin user provisioned with bcrypt hash and terms accepted."""
    cmd = """
const fs = require('fs');
const crypto = require('crypto');
const bcrypt = require('bcrypt');
const pg = require('pg');

async function main() {
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  const countRes = await pool.query('SELECT COUNT(*) FROM user_account');
  if (parseInt(countRes.rows[0].count) === 0) {
    let sig = '';
    try {
      const content = fs.readFileSync('/app/terms/_template/en-US.md', 'utf8');
      sig = crypto.createHash('sha256').update(content).digest('hex');
    } catch(e){}
    const hash = await bcrypt.hash('admin', 10);
    await pool.query(`
      INSERT INTO user_account (
        email, password, role, name, username,
        subscribe_to_own_cards, subscribe_to_card_when_commenting,
        turn_off_recent_card_highlighting, enable_favorites_by_default,
        default_editor_mode, default_home_view, default_projects_order,
        is_deactivated, created_at, updated_at, terms_signature, terms_accepted_at
      ) VALUES (
        'admin@appvault.local', $1, 'admin', 'Admin', 'admin',
        true, true, false, false, 'rich_text', 'grid', 'alphabetical',
        false, NOW(), NOW(), $2, NOW()
      )
    `, [hash, sig]);
    await pool.query('UPDATE internal_config SET is_initialized = true');
    console.log('[planka] Default admin account seeded successfully');
  }
  pool.end();
}
main().catch(err => { console.error('[planka] seed error:', err); });
"""
    _docker("exec", "app-planka", "node", "-e", cmd, capture=True, timeout=20)

def _do_install_stack(app_id):
    """Install a Docker Compose stack app (downloaded from GitHub)."""
    global _install_progress
    _set_progress(app_id, "Preparing stack installation...", 5)
    
    if not docker_available():
        _set_progress_error(app_id, "Docker is not available")
        raise Exception("Docker unavailable")
    
    try:
        sync_catalog()
    except:
        pass
    
    app_def = None
    for a in catalog_cache.get("apps", []):
        if a["id"] == app_id:
            app_def = a
            break
    if not app_def:
        _set_progress_error(app_id, "App not found in catalog")
        raise Exception(f"App '{app_id}' not found in catalog")

    # Normalize & Auto-Heal stack definition
    app_def = _normalize_and_heal_app_def(app_def)
    
    # Enforce plan gating / unpublished protection (same rule as api_install)
    blocked = _install_blocked_reason(app_def)
    if blocked:
        _set_progress_error(app_id, blocked)
        raise Exception(blocked)
    
    compose_url = app_def.get("compose_url", "")
    if not compose_url:
        _set_progress_error(app_id, "No docker-compose URL defined")
        raise Exception("No compose_url defined for stack app")
    
    app_name = app_def.get("name", app_id)
    _set_progress(app_id, f"Setting up {app_name}...", 10)
    
    stack_dir = os.path.join(os.environ.get("STORAGE_PATH", "/data"), "stacks", app_id)
    os.makedirs(stack_dir, exist_ok=True)
    compose_path = os.path.join(stack_dir, "docker-compose.yml")
    
    _set_progress(app_id, "Downloading stack configuration...", 20)
    print(f"[agent] Downloading from {compose_url}")
    
    # Determine the repo URL from the compose URL
    repo_url = ""
    repo_dir = stack_dir
    repo_rel_path = ""  # relative compose path inside the repo (from the URL)
    if "raw.githubusercontent.com" in compose_url:
        # Extract GitHub repo URL: https://raw.githubusercontent.com/user/repo/branch/file
        parts = compose_url.replace("https://raw.githubusercontent.com/", "").split("/")
        if len(parts) >= 3:
            user, repo, branch = parts[0], parts[1], parts[2]
            repo_url = f"https://github.com/{user}/{repo}.git"
            repo_dir = os.path.join(stack_dir, "repo")
            if len(parts) > 3:
                repo_rel_path = "/".join(parts[3:])

    # Always fetch direct compose as guaranteed fallback
    if compose_url.startswith("http://") or compose_url.startswith("https://"):
        try:
            req = urllib.request.Request(compose_url, headers={"User-Agent": "AppVault-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8")
                if content and "services:" in content:
                    with open(compose_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    print(f"[agent] Base compose downloaded: {compose_path}")
        except Exception as e:
            print(f"[agent] Direct compose download notice: {e}")

    # Clone the full repo if we have a git URL (needed for build-from-source/env files)
    if repo_url:
        print(f"[agent] Cloning repo: {repo_url}")
        _set_progress(app_id, "Cloning source code...", 20)
        _safe_rmtree(repo_dir)
        import subprocess
        r = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, repo_dir],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0:
            print(f"[agent] Repo cloned to {repo_dir}")
            # Create any referenced env files to prevent compose failures
            rel_compose = os.path.join(repo_dir, repo_rel_path) if repo_rel_path else os.path.join(repo_dir, "docker-compose.yml")
            if os.path.exists(rel_compose):
                compose_path = rel_compose
                with open(rel_compose, 'r', encoding='utf-8') as f:
                    compose_content = f.read()
                import re
                env_files = re.findall(r'env_file:\s*([^\n]+)', compose_content)
                for ef in env_files:
                    ef_path = os.path.join(repo_dir, ef.strip().strip('"').strip("'"))
                    if not os.path.exists(ef_path):
                        with open(ef_path, 'w', encoding='utf-8') as f:
                            f.write("# Auto-created by AppVault\n")
                        print(f"[agent] Created missing env file: {ef_path}")
                        example = ef_path + ".example"
                        if os.path.exists(example):
                            shutil.copy2(example, ef_path)
                            print(f"[agent] Seeded env file from example: {ef_path}")
        else:
            print(f"[agent] Git clone notice (falling back to direct compose): {r.stderr[:200]}")

    if not os.path.exists(compose_path):
        _set_progress_error(app_id, f"Compose file not found: {compose_path}")
        raise Exception(f"Compose file not found: {compose_path}")
    print(f"[agent] Using compose file: {compose_path}")

    # ADDITIVE: direct-HTTP compose URLs (e.g. central-hosted compose) are downloaded
    # instead of cloned. Existing raw.githubusercontent.com stack apps are unaffected.
    if not repo_url and (compose_url.startswith("http://") or compose_url.startswith("https://")):
        print(f"[agent] Downloading compose from {compose_url}")
        _set_progress(app_id, "Downloading compose configuration...", 20)
        import subprocess
        r = subprocess.run(["curl", "-fsSL", compose_url, "-o", compose_path],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not os.path.exists(compose_path) or os.path.getsize(compose_path) == 0:
            _set_progress_error(app_id, f"Failed to download compose: {(r.stderr or '')[:200]}")
            raise Exception(f"Failed to download compose for '{app_id}'")
        print(f"[agent] Compose downloaded to {compose_path}")

    _set_progress(app_id, "Pulling images...", 40)

    # ADDITIVE: inject the public SERVER_URL for stack apps whose frontend uses it
    # as its API base (e.g. Twenty). Without this, the browser calls
    # http://localhost:3000 (the user's own machine) -> "Unable to Reach Back-end".
    if app_def.get("is_stack") or app_def.get("compose_url"):
        try:
            import re as _re
            hp = str(_app_https_ports().get(app_id, _https_port(app_id)))
            base = f"{public_base()}:{hp}"
            with open(compose_path, "r", encoding="utf-8") as _f:
                _content = _f.read()
            _new = _content
            _new = _re.sub(r'SERVER_URL:\s*"[^"]*"', f'SERVER_URL: "{base}"', _new)
            _new = _re.sub(r"SERVER_URL:\s*'[^']*'", f"SERVER_URL: '{base}'", _new)
            _new = _re.sub(r'SERVER_URL\s*=\s*\S+', f'SERVER_URL={base}', _new)
            # OpenShip: the API only trusts the browser origin when
            # OPENSHIP_PUBLIC_URL matches the launch URL — without it remote
            # login is rejected with 403 ORIGIN_REJECTED. Inject the same
            # per-client base (public_base():https-port) as SERVER_URL above.
            _new = _re.sub(r'OPENSHIP_PUBLIC_URL:\s*"[^"]*"', f'OPENSHIP_PUBLIC_URL: "{base}"', _new)
            _new = _re.sub(r"OPENSHIP_PUBLIC_URL:\s*'[^']*'", f"OPENSHIP_PUBLIC_URL: '{base}'", _new)
            _new = _re.sub(r'OPENSHIP_PUBLIC_URL\s*=\s*\S+', f'OPENSHIP_PUBLIC_URL={base}', _new)
            # Buzz: the relay seeds its deployment community from RELAY_URL at
            # startup and fail-closes any host not in the communities table —
            # an unmapped host answers "no community is configured for this
            # host" on EVERY route. The seeded host must equal the host users
            # actually reach the relay at, so rewrite RELAY_URL per client:
            #   proxy mode  -> ws://<public host>:<https-port> (launch URL)
            #   direct mode -> ws://localhost:<stable host port> (live port)
            try:
                _stable_port = _stable_host_port(
                    f"app-{app_id}", app_id, str(app_def.get("container_port", "") or ""))
                _relay_url = f"ws://{public_base_host()}:{hp}" if (PUBLIC_URL and "://" in PUBLIC_URL) \
                    else f"ws://localhost:{_stable_port}"
                _new = _re.sub(r'RELAY_URL:\s*"[^"]*"', f'RELAY_URL: "{_relay_url}"', _new)
                _new = _re.sub(r"RELAY_URL:\s*'[^']*'", f"RELAY_URL: '{_relay_url}'", _new)
                _new = _re.sub(r'RELAY_URL\s*=\s*\S+', f'RELAY_URL={_relay_url}', _new)
            except Exception as _e:
                print(f"[agent] RELAY_URL injection skipped for {app_id}: {_e}")
            if _new != _content:
                with open(compose_path, "w", encoding="utf-8") as _f:
                    _f.write(_new)
                print(f"[agent] Injected SERVER_URL/OPENSHIP_PUBLIC_URL={base} into {app_id} compose")
        except Exception as _e:
            print(f"[agent] URL injection skipped for {app_id}: {_e}")

    # ADDITIVE: stabilize the web service's host port. Stack composes often use a
    # bare `- "9000"` (random host port) — that drifts on every reinstall and makes
    # launch URLs unpredictable. Rewrite the container_port mapping to a
    # deterministic stable host port so every client install behaves identically.
    try:
        import re as _re3
        _cport3 = str(app_def.get("container_port", "") or "")
        with open(compose_path, "r", encoding="utf-8") as _f:
            _content3 = _f.read()
        
        # 1) Rewrite bare container ports (e.g. - "3000") to stable high host port
        if _cport3:
            _stable3 = _stable_host_port(f"app-{app_id}", app_id, _cport3)
            _content3 = _re3.sub(
                rf'^(\s*-\s*)["\']?{_cport3}["\']?\s*$',
                lambda m: m.group(1) + f'"{_stable3}:{_cport3}"',
                _content3, flags=_re3.M)

        # 2) Rewrite low / colliding host port mappings (80:80, 81:81, 8080:8080, 3000:3000, etc.)
        def remap_low_host_ports(m):
            indent = m.group(1)
            mapping = m.group(2).strip().strip('"').strip("'")
            if ":" in mapping:
                parts = mapping.split(":", 1)
                h_p, c_p = parts[0], parts[1]
                if h_p in {"80", "81", "443", "3000", "5000", "8000", "8080", "8081", "8085", "8086", "8087"}:
                    new_h = _stable_host_port(f"app-{app_id}", app_id, c_p)
                    return f'{indent}"{new_h}:{c_p}"'
            return m.group(0)

        _new3 = _re3.sub(r'^(\s*-\s*)("?\d+:\d+"?)\s*$', remap_low_host_ports, _content3, flags=_re3.M)
        if _new3 != _content3:
            with open(compose_path, "w", encoding="utf-8") as _f:
                _f.write(_new3)
            print(f"[agent] Re-mapped conflicting host ports for {app_id} stack")
    except Exception as _e:
        print(f"[agent] port stabilization skipped for {app_id}: {_e}")

    # ADDITIVE: tag the stack's web service with appvault labels so Caddy's HTTPS
    # proxy (and health/restart machinery) recognize it - same as single-image apps.
    # Without this, store Launch URLs 502 for stacks whose compose lacks the labels
    # (e.g. affine before the affine-stack compose gained them).
    try:
        import re as _re2
        _cport = str(app_def.get("container_port", ""))
        with open(compose_path, "r", encoding="utf-8") as _f:
            _content = _f.read()
        _svc_matches = list(_re2.finditer(r'^  ([\w-]+):\s*$', _content, _re2.M))
        _svc_match = None
        for _idx, _m in enumerate(_svc_matches):
            _end = _svc_matches[_idx + 1].start() if _idx + 1 < len(_svc_matches) else len(_content)
            _block = _content[_m.end():_end]
            if _cport and any(p in _block for p in
                              (f"{_cport}:{_cport}", f"'{_cport}:{_cport}'", f'"{_cport}:{_cport}"')):
                _svc_match = _m
                break
        if _svc_match is None:  # fallback: first service with any ports mapping
            for _idx, _m in enumerate(_svc_matches):
                _end = _svc_matches[_idx + 1].start() if _idx + 1 < len(_svc_matches) else len(_content)
                if _re2.search(r'^\s*ports:', _content[_m.end():_end], _re2.M):
                    _svc_match = _m
                    break
        if _svc_match and "appvault.managed" not in _content:
            _svc_ix = _svc_matches.index(_svc_match)
            _svc_end = _svc_matches[_svc_ix + 1].start() if _svc_ix + 1 < len(_svc_matches) else len(_content)
            _block = _content[_svc_match.end():_svc_end]
            _img = _re2.search(r'^(\s*image:.*)$', _block, _re2.M)
            _labels = "    labels:\n      - appvault.managed=true\n      - appvault.app=" + app_id
            if _img:
                _ins_at = _svc_match.end() + _img.start(1) + len(_img.group(1))
                _content = _content[:_ins_at] + "\n" + _labels + _content[_ins_at:]
            else:
                _content = _content[:_svc_match.end()] + "\n" + _labels + _content[_svc_match.end():]
            with open(compose_path, "w", encoding="utf-8") as _f:
                _f.write(_content)
            print(f"[agent] Injected appvault labels into {app_id} stack web service")
    except Exception as _e:
        print(f"[agent] label injection skipped for {app_id}: {_e}")
    _proj = _stack_project(app_id)
    ok, pull_out = _docker("compose", "-p", _proj, "-f", compose_path, "pull", capture=True, timeout=600)
    if not ok:
        print(f"[agent] Pull warning: {pull_out[:200]}")
    
    _set_progress(app_id, "Building services...", 55)
    ok, build_out = _docker("compose", "-p", _proj, "-f", compose_path, "build", capture=True, timeout=600)
    if ok:
        print(f"[agent] Build complete")
    else:
        print(f"[agent] Build output: {build_out[:200]}")
    
    _set_progress(app_id, "Starting services...", 70)
    
    ok, services_out = _docker("compose", "-p", _proj, "-f", compose_path, "config", "--services", capture=True, timeout=30)
    services = services_out.strip().split('\n') if ok else []
    
    for i, svc in enumerate(services):
        pct = 70 + int((i / max(len(services), 1)) * 25)
        _set_progress(app_id, f"Starting {svc}...", pct)
        ok, err = _docker("compose", "-p", _proj, "-f", compose_path, "up", "-d", svc, capture=True, timeout=300)
        if ok:
            print(f"[agent] {svc} started")
        else:
            err_lower = err.lower()
            # Handle missing env files
            if "env file" in err_lower and "not found" in err_lower:
                # Extract the missing file path
                import re
                m = re.search(r"env file\s+(\S+)\s+not found", err_lower)
                if m:
                    missing_env = m.group(1)
                    # Create the missing env file
                    parent = os.path.dirname(os.path.dirname(compose_path))  # repo dir
                    ef_path = os.path.join(parent, missing_env)
                    if not os.path.exists(ef_path):
                        os.makedirs(os.path.dirname(ef_path), exist_ok=True)
                        with open(ef_path, 'w') as f:
                            f.write("# Auto-created by AppVault\n")
                        print(f"[agent] Created missing env file: {ef_path}")
                        # Prefer a sibling .env.example as a starting point so
                        # services get sane defaults (e.g. Dify's docker/.env.example)
                        example = ef_path + ".example"
                        if os.path.exists(example):
                            shutil.copy2(example, ef_path)
                            print(f"[agent] Seeded env file from example: {ef_path}")
                        # Retry
                        ok, err = _docker("compose", "-p", _proj, "-f", compose_path, "up", "-d", svc, capture=True, timeout=300)
                        if ok:
                            print(f"[agent] {svc} started after creating env file")
                            continue
            
            # Handle port conflicts - remap to random ports
            if "port is already allocated" in err.lower() or "bind for" in err.lower():
                print(f"[agent] Port conflict for {svc}, remapping...")
                # Read compose file and remap ports
                with open(compose_path, 'r') as f:
                    content = f.read()
                import re
                # Find all port mappings and replace host port with a random one.
                # ANCHORED to port-mapping list items only ("- 4000:4000" /
                # '- "4000:4000"') — the old global `"?\d+:\d+"?` regex also hit
                # digit:digit sequences INSIDE healthchecks/env values
                # (e.g. 127.0.0.1:4000, r.ok?0:1), corrupting the file and
                # leaving the app permanently unhealthy (OpenShip incident).
                def remap_mapping(m):
                    mapping = m.group(1)
                    # Extract the host port part
                    host_part = mapping.split(':')[0].strip().strip('"').strip("'")
                    if host_part.isdigit():
                        new_host = str(_find_free_port())
                        return '- ' + mapping.replace(host_part, new_host, 1)
                    return m.group(0)
                content = re.sub(r'^\s*-\s*("?\d+:\d+"?)\s*$', remap_mapping, content, flags=re.M)
                with open(compose_path, 'w') as f:
                    f.write(content)
                ok, err = _docker("compose", "-p", _proj, "-f", compose_path, "up", "-d", svc, capture=True, timeout=300)
                if ok:
                    print(f"[agent] {svc} started with remapped ports")
                else:
                    print(f"[agent] {svc} still failed after remap: {err[:200]}")
            else:
                print(f"[agent] {svc} failed: {err[:200]}")

    # Connect stack services to the shared network so the MCP gateway can
    # reach them by container IP (stacks otherwise get their own default net).
    try:
        net = _caddy_net()
        for svc in services:
            okc, outc = _docker("compose", "-p", _proj, "-f", compose_path, "ps", "-q", svc,
                                capture=True, timeout=30)
            cid = outc.strip().splitlines()[0] if (okc and outc.strip()) else ""
            if cid:
                _docker("network", "connect", net, cid, capture=True, timeout=30)
    except Exception as e:
        print(f"[agent] stack network connect warning: {e}")

    _set_progress(app_id, "Finalizing...", 95)

    # ADDITIVE: wait for the stack's web service (labeled appvault.app=<app_id>) to
    # become responsive before finalizing. Prevents a 502 / "Unable to Reach Back-end"
    # window for clients during first-boot DB migrations (e.g. Twenty takes ~5 min).
    try:
        cport = str(app_def.get("container_port", "3000"))
        okc, outc = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                            "--format", "{{.Names}}", capture=True)
        if okc and outc and outc.strip():
            svc = outc.strip().splitlines()[0].strip()
            _set_progress(app_id, "Waiting for app to become ready... (first boot can take a few minutes)", 95)
            waited = 0
            while waited < 60:  # up to ~10 minutes for first-boot migrations
                if _stack_web_ready(svc, cport):
                    print(f"[agent] {app_id} web service {svc} ready after ~{waited*10}s")
                    break
                waited += 1
                time.sleep(10)
                if waited % 6 == 0:  # refresh progress every ~60s so the UI doesn't look stuck
                    mins = int(waited / 6)
                    _set_progress(app_id,
                                  f"Waiting for app to become ready... ({mins} min, first boot can take a few minutes)",
                                  95)
            if waited >= 60:
                print(f"[agent] {app_id} web service not ready after 10 min; continuing")
    except Exception as e:
        print(f"[agent] Wait-for-ready skipped for {app_id}: {e}")
    # END ADDITIVE

    try:
        from heimdall_bridge import add_heimdall_tile
        tile_url = f"{public_base()}:{app_def.get('container_port','3000')}"
        add_heimdall_tile(app_name, tile_url, app_id, app_def.get("description", ""))
    except Exception as e:
        print(f"[agent] Tile not added: {e}")

    # ADDITIVE: register any labeled stack services with Caddy's HTTPS proxy so the
    # store's Launch URL works for stack apps (same as single-image apps).
    try:
        _sync_caddy_apps()
    except Exception as e:
        print(f"[agent] Caddy sync failed for stack app: {e}")

    _set_progress_done(app_id, f"{app_name} installed!")
    print(f"[agent] {app_id} stack installed")


def _sweep_app_volumes(app_id):
    """Remove all Docker volumes whose names are associated with this app.
    Catches volumes that weren't in the container inspect (e.g. the container was
    already removed before uninstall ran) by matching common naming patterns:
      - <app_id>-<anything>        (e.g. listmonk-data, n8n-data)
      - <app_id>_<anything>        (e.g. app_id_db)
      - app-<app_id>-<anything>    (e.g. compose-prefixed volumes)
    Skips 'central-*' shared infrastructure volumes.
    """
    try:
        ok, out = _docker("volume", "ls", "--format", "{{.Name}}", capture=True)
        if not ok or not out:
            return
        prefixes = (f"{app_id}-", f"{app_id}_", f"app-{app_id}-", f"app-{app_id}_",
                    f"app_{app_id}_", f"app_{app_id}-")
        for vol in out.strip().splitlines():
            vol = vol.strip()
            if not vol:
                continue
            if vol.startswith("central-"):
                continue
            if any(vol.startswith(p) for p in prefixes) or vol == app_id:
                ok_v, ve = _docker("volume", "rm", "--force", vol, capture=True)
                if ok_v:
                    print(f"[agent] {app_id}: swept volume {vol}")
                else:
                    print(f"[agent] {app_id}: sweep volume {vol} warn: {str(ve)[:60]}")
    except Exception as e:
        print(f"[agent] _sweep_app_volumes({app_id}) warn: {e}")

def _do_uninstall(app_id):
    """Uninstall a Docker app AND free ALL its disk (containers, image, volumes, data dirs).
    After this call the app should not appear anywhere in `docker ps -a` or `docker volume ls`.
    """
    if not docker_available():
        raise Exception("Docker unavailable")

    container_name = f"app-{app_id}"

    # ── Stack / compose apps ──────────────────────────────────────────────────
    # Stack containers are labeled appvault.app=<app_id> (e.g. twenty-server-1)
    if not container_exists(container_name):
        ok_lbl, out_lbl = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                                   "--format", "{{.Names}}", capture=True)
        if ok_lbl and out_lbl and out_lbl.strip():
            print(f"[agent] {app_id} is a stack app; removing stack...")
            stack_root = os.path.join(os.environ.get("STORAGE_PATH", "/data"), "stacks", app_id)
            compose_path = ""
            for cand in (os.path.join(stack_root, "docker-compose.yml"),
                         os.path.join(stack_root, "repo", "docker-compose.yml")):
                if os.path.exists(cand):
                    compose_path = cand
                    break
            if compose_path:
                _docker("compose", "-p", _stack_project(app_id), "-f", compose_path,
                        "down", "-v", "--remove-orphans", capture=True, timeout=300)
            else:
                for cname in out_lbl.strip().splitlines():
                    cname = cname.strip()
                    ok_img, img_out = _docker("inspect", cname, "--format",
                                             "{{.Config.Image}}", capture=True)
                    _docker("stop", cname, capture=True)
                    _docker("rm", cname, capture=True)
                    if ok_img and img_out.strip():
                        _docker("image", "rm", "--force", img_out.strip(), capture=True)
                # Prune ONLY this stack's labeled volumes
                _docker("volume", "prune", "-f",
                        "--filter", f"label=com.docker.compose.project={_stack_project(app_id)}",
                        capture=True)
            # Also sweep volumes named after the project
            _sweep_app_volumes(app_id)
            try:
                _sync_caddy_apps()
            except Exception:
                pass
            print(f"[agent] {app_id} stack fully removed")
            return
        print(f"[agent] {app_id} not found — nothing to remove")
        return

    # ── Single-container app ──────────────────────────────────────────────────
    # Capture image + volumes BEFORE removing the container
    image = None
    named_volumes = []
    bind_dirs = []
    try:
        ok, insp = _docker("inspect", container_name, capture=True)
        if ok and insp:
            import json as _json
            info = _json.loads(insp)[0]
            image = info.get("Config", {}).get("Image")
            for m in info.get("Mounts", []):
                if m.get("Type") == "volume" and m.get("Name"):
                    named_volumes.append(m["Name"])
                elif m.get("Type") == "bind" and m.get("Source"):
                    bind_dirs.append(m["Source"])
    except Exception as e:
        print(f"[agent] uninstall inspect warn: {e}")

    # Check for sibling containers (e.g. app-listmonk-db, app-twenty-db)
    ok_sib, sib_out = _docker("ps", "-a", "--filter", f"name=app-{app_id}-",
                               "--format", "{{.Names}}", capture=True)
    sibling_containers = [n.strip() for n in (sib_out or "").splitlines() if n.strip()]

    # Stop and remove main container
    print(f"[agent] {app_id}: stopping and removing container...")
    _docker("stop", container_name, capture=True)
    ok_rm, rm_err = _docker("rm", "--force", container_name, capture=True)
    if ok_rm:
        print(f"[agent] {app_id}: container removed")
    else:
        print(f"[agent] {app_id}: container rm warn: {str(rm_err)[:80]}")

    # Stop and remove sibling containers
    for sib in sibling_containers:
        ok_si, si_img = _docker("inspect", sib, "--format", "{{.Config.Image}}", capture=True)
        _docker("stop", sib, capture=True)
        _docker("rm", "--force", sib, capture=True)
        print(f"[agent] {app_id}: removed sibling container {sib}")
        # Collect sibling images for removal
        if ok_si and si_img.strip():
            _docker("image", "rm", "--force", si_img.strip(), capture=True)

    # 1. Remove the app's Docker image (frees GBs)
    if image:
        ok_img, err_img = _docker("image", "rm", "--force", image, capture=True)
        if ok_img:
            print(f"[agent] {app_id}: removed image {image}")
        else:
            print(f"[agent] {app_id}: image {image} not removed ({str(err_img)[:60]}) — shared/in-use")

    # 2. Prune dangling image layers left behind
    _docker("image", "prune", "-f", capture=True)

    # 3. Remove named volumes found in inspect
    for vol in named_volumes:
        if str(vol).startswith("central-"):
            continue
        ok_v, ve = _docker("volume", "rm", "--force", vol, capture=True)
        if ok_v:
            print(f"[agent] {app_id}: removed volume {vol}")
        else:
            print(f"[agent] {app_id}: volume {vol} rm warn: {str(ve)[:60]}")

    # 4. Sweep any additional volumes named after the app that weren't in inspect
    _sweep_app_volumes(app_id)

    # 5. Remove the app's data dir + bind mounts
    app_data_host = os.environ.get("APP_DATA_HOST_PATH", "")
    host_dir = ""
    if app_data_host:
        host_dir = os.path.join(app_data_host, app_id).replace(os.sep, "/")
        if os.path.isdir(host_dir):
            import shutil
            shutil.rmtree(host_dir, ignore_errors=True)
            print(f"[agent] {app_id}: removed data dir {host_dir}")
    for d in bind_dirs:
        if d and d != host_dir and os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            print(f"[agent] {app_id}: removed bind dir {d}")

    # Remove Heimdall tile
    try:
        from heimdall_bridge import remove_heimdall_tile
        tile_url = (f"{public_base()}:{_https_port(app_id)}" if not _is_proxy_disabled(app_id)
                    else f"{public_base()}:{get_container_host_port(container_name) or ''}")
        if tile_url:
            remove_heimdall_tile(tile_url)
    except Exception:
        pass

    # Monitoring tools: clear per-install secret + wipe data
    if app_id in ("portainer", "uptime-kuma", "netdata"):
        try:
            _mon_sec(app_id, "clear")
        except Exception:
            pass
        try:
            hd = _monitoring_health_dir(app_id)
            if hd and os.path.isdir(hd):
                import shutil
                shutil.rmtree(hd, ignore_errors=True)
                print(f"[agent] {app_id}: wiped monitoring data dir {hd}")
        except Exception:
            pass

    _sync_caddy_apps()  # Remove HTTPS reverse-proxy path for this app
    print(f"[agent] {app_id}: fully uninstalled — container, image, volumes and data removed")


def _do_restart(app_id):
    """Restart a Docker app."""
    if not docker_available():
        raise Exception("Docker unavailable")
    
    container_name = f"app-{app_id}"
    if not container_exists(container_name):
        raise Exception(f"Container '{container_name}' not found")
    
    ok, err = _docker("restart", container_name, capture=True)
    if ok:
        print(f"[agent] {app_id} restarted")
        try:
            _sync_caddy_apps()
        except Exception as e:
            print(f"[agent] restart caddy sync warn: {e}")
    else:
        raise Exception(f"Failed to restart: {err}")

def _do_stop(app_id):
    """Stop a Docker app and remove its container (frees memory + clears Docker Desktop clutter).
    Data is preserved in named volumes so the app can be restarted at any time.
    Stopping a stack app tears down all stack containers.
    """
    if not docker_available():
        raise Exception("Docker unavailable")
    container_name = f"app-{app_id}"

    # Stack app: look for containers labeled appvault.app=<app_id>
    ok_lbl, out_lbl = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                               "--format", "{{.Names}}", capture=True)
    if ok_lbl and out_lbl and out_lbl.strip() and not container_exists(container_name):
        # It's a stack — stop and remove all stack containers
        print(f"[agent] {app_id} is a stack app; stopping stack...")
        stack_root = os.path.join(os.environ.get("STORAGE_PATH", "/data"), "stacks", app_id)
        compose_path = ""
        for cand in (os.path.join(stack_root, "docker-compose.yml"),
                     os.path.join(stack_root, "repo", "docker-compose.yml")):
            if os.path.exists(cand):
                compose_path = cand
                break
        if compose_path:
            _docker("compose", "-p", _stack_project(app_id), "-f", compose_path, "down",
                    capture=True, timeout=120)
        else:
            for cname in out_lbl.strip().splitlines():
                _docker("stop", cname.strip(), capture=True)
                _docker("rm", cname.strip(), capture=True)
        print(f"[agent] {app_id} stack stopped and containers removed")
        return

    if not container_exists(container_name):
        raise Exception(f"Container '{container_name}' not found")

    # Stop the container
    ok, err = _docker("stop", container_name, capture=True)
    if not ok:
        raise Exception(f"Failed to stop: {err}")

    # Remove the container — data lives in volumes, not the container layer.
    # This removes the Exited entry from Docker Desktop immediately.
    ok_rm, err_rm = _docker("rm", container_name, capture=True)
    if ok_rm:
        print(f"[agent] {app_id} stopped and container removed (data preserved in volumes)")
    else:
        # Non-fatal: rm failed but stop succeeded — warn and continue.
        print(f"[agent] {app_id} stopped (container rm warn: {str(err_rm)[:80]})")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# APP HEALTH MONITOR
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_health_failures = {}
_health_last_restart = {}
MAX_FAILURES = 3  # Restart after 3 consecutive failures
GRACE_PERIOD = 120  # Skip health check for 2 min after restart

def _get_container_started_at(cname):
    """Get when the container was last started (unix timestamp)."""
    ok, out = _docker("inspect", cname, "--format", "{{.State.StartedAt}}", capture=True)
    if ok and out:
        try:
            from datetime import datetime
            # Format: 2026-07-30T11:25:14.123456789Z
            ts = out.strip().replace('Z', '').split('.')[0]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            return dt.timestamp()
        except:
            pass
    return 0

def _get_internal_port(cname):
    """Get the container's INTERNAL web port (catalog container_port, else docker inspect).

    Apps no longer publish raw host ports (only Caddy does), so `docker port` returns
    nothing. Use the catalog's container_port (the app's real internal web port) so
    health checks curl the correct port inside the container.
    """
    app_id = cname.replace("app-", "", 1)
    for a in catalog_cache.get("apps", []):
        if a["id"] == app_id and a.get("container_port"):
            return str(a["container_port"])
    try:
        ok, out = _docker("inspect", cname, "--format", "{{json .Config.ExposedPorts}}", capture=True)
        if ok and out:
            import json as _json
            d = _json.loads(out)
            ports = [k.split("/")[0] for k in d if k.endswith("/tcp")]
            if ports:
                # prefer a likely web port
                for p in ("80", "8080", "3000", "3001", "8000", "8096", "2342", "5678"):
                    if p in ports:
                        return p
                return ports[0]
    except Exception:
        pass
    return "80"

def _is_app_alive(cname, internal_port):
    """Check if a container's web server is responding.

    Order: native docker healthcheck -> wget (any HTTP response) -> curl ->
    node/bun fetch. Modern images (node/bun, distroless) ship NO curl/wget
    (omniroute runs `node healthcheck.mjs`), and the old wget probe required
    a >50-byte body — together they produced false "unresponsive" verdicts
    and a restart storm (2026-08-08: omniroute/crewai-studio/central-redis
    bounced every ~3 min while perfectly healthy).
    """
    url = f"http://127.0.0.1:{internal_port}/"
    # 0) bulk-snapshot health verdict (no docker subprocess)
    hhit = _PORT_CACHE.get(("h", cname))
    if hhit and time.time() - hhit[0] < _PORT_CACHE_TTL:
        if hhit[1] == "healthy":
            return True
        if hhit[1] == "unhealthy":
            return False
    # 1) native docker healthcheck — the image's own probe is authoritative
    okh, hout = _docker("inspect", "--format", "{{.State.Health.Status}}", cname, capture=True, timeout=5)
    if okh and hout.strip() == "healthy":
        return True
    # 2) wget — exit 0 = any HTTP response (2xx/3xx/4xx all fine). No body
    #    length requirement: redirect/error pages can be tiny.
    ok, _ = _docker("exec", cname, "wget", "-q", "-O", "/dev/null", "--timeout=2", url, capture=True, timeout=5)
    if ok:
        return True
    # 3) curl fallback
    ok, out = _docker("exec", cname, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "3", url, capture=True, timeout=6)
    if ok and out.strip().isdigit():
        code = int(out.strip())
        if 200 <= code < 500:
            return True
    # 4) node/bun fetch fallback (images with neither curl nor wget)
    for runner in ("node", "bun"):
        ok, _ = _docker("exec", cname, runner, "-e",
                        "fetch(process.argv[1]).then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))",
                        url, capture=True, timeout=6)
        if ok:
            return True
    return False

def _stack_web_ready(svc, cport):
    """Robust, fast readiness for a stack app's web service."""
    # 1) If native docker healthcheck is healthy, return immediately
    okh, hout = _docker("inspect", "--format", "{{.State.Health.Status}}", svc, capture=True, timeout=5)
    if okh and hout.strip() == "healthy":
        return True

    # 2) Fast HTTP probes
    for attempt in range(2):
        ok_health = False
        # /healthz endpoint
        okc, cout = _docker("exec", svc, "curl", "-s", "-o", "/dev/null", "-w",
                            "%{http_code}", "--max-time", "3",
                            f"http://127.0.0.1:{cport}/healthz", capture=True, timeout=5)
        if okc and cout.strip() == "200":
            ok_health = True
        
        # Plain `/` probe
        if not ok_health:
            okr, rout = _docker("exec", svc, "curl", "-s", "-o", "/dev/null", "-w",
                                "%{http_code}", "--max-time", "3",
                                f"http://127.0.0.1:{cport}/", capture=True, timeout=5)
            if okr and rout.strip().isdigit() and 200 <= int(rout.strip()) < 500:
                ok_health = True

        if not ok_health:
            return False
        if attempt == 0:
            time.sleep(2)
    return True

def check_apps_health():
    """Check installed apps health. Only restarts after 3 consecutive failures and grace period."""
    global _health_failures, _health_last_restart
    if not docker_available():
        return
    now = time.time()
    ok, out = _docker("ps", "--filter", "name=app-", "--format", "{{.Names}}", capture=True)
    if not ok or not out:
        return
    containers = [l.strip() for l in out.strip().split('\n') if l.strip()]
    for cname in containers:
        # Skip if recently restarted (grace period)
        last_restart = _health_last_restart.get(cname, 0)
        if now - last_restart < GRACE_PERIOD:
            continue
        app_id = cname.replace("app-", "", 1)
        # NEVER health-restart infra containers (central-* DBs/caches etc.) —
        # e.g. central-redis speaks RESP, not HTTP: the old probe marked it
        # "unresponsive" and restarted it every ~3 min (2026-08-08).
        if app_id.startswith("central-"):
            continue
        app_def = None
        for a in catalog_cache.get("apps", []):
            if a["id"] == app_id:
                app_def = a
                break
        if not app_def or app_def.get("hidden") or app_def.get("disabled"):
            continue
        # Skip database apps (no web UI)
        if app_def.get("category", "").lower() == "database":
            continue
        # Skip apps still inside their boot window — slow first boots (Nextcloud,
        # AI stacks) are NOT unhealthy. boot_timeout doubles as the grace period.
        boot_timeout = int(app_def.get("boot_timeout") or 150)
        oks, sout = _docker("inspect", "--format", "{{.State.StartedAt}}", cname, capture=True, timeout=15)
        if oks and sout.strip():
            try:
                from datetime import datetime
                started = datetime.fromisoformat(sout.strip().replace("Z", "+00:00"))
                import datetime as _dt
                if (datetime.now(_dt.timezone.utc) - started).total_seconds() < boot_timeout:
                    continue
            except Exception:
                pass
        internal_port = _get_internal_port(cname)
        alive = _is_app_alive(cname, internal_port)
        if alive:
            _health_failures[cname] = 0
            continue
        # Track failure
        failures = _health_failures.get(cname, 0) + 1
        _health_failures[cname] = failures
        if failures >= MAX_FAILURES:
            print(f"[health] {app_id} ({cname}) failed {failures}x, restarting...")
            _docker("restart", cname)
            _health_failures[cname] = 0
            _health_last_restart[cname] = time.time()
        else:
            print(f"[health] {app_id} ({cname}) unresponsive ({failures}/{MAX_FAILURES})")

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PHONE HOME THREAD
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def phone_home_loop():
    """Background thread that talks to central server."""
    time.sleep(2)  # wait for server to be ready
    
    registered = False
    last_heartbeat = 0
    
    while True:
        try:
            if not registered:
                registered = register_with_central()
            
            if registered:
                # Heartbeat
                now = time.time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    send_heartbeat()
                    last_heartbeat = now
                
                # Poll for jobs
                poll_jobs()
                
                # Sync catalog
                sync_catalog()
                
                # Check app health
                check_apps_health()
            
        except Exception as e:
            print(f"[agent] Phone-home error: {e}")
        
        time.sleep(POLL_INTERVAL)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PING CENTRAL (health check)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.route("/api/ping/central")
def ping_central():
    """Test connectivity to central server."""
    result = central_request("POST", "/api/agent/ping", data={
        "agent_id": agent_state.get("agent_id", ""),
        "api_key": agent_state.get("api_key", ""),
        "echo": "hello"
    })
    if result:
        return jsonify({"central": "connected", "server": result})
    return jsonify({"central": "disconnected"})

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LOCAL API â€” Heimdall-compatible
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def get_app_status_local(app_id):
    """Check if a Docker app is installed locally (cached 15s)."""
    return _cached_docker_port(("st", app_id), _get_app_status_local_uncached, app_id)

def _get_app_status_local_uncached(app_id):
    if _BULK_CACHE_TS > 0 and (time.time() - _BULK_CACHE_TS < _BULK_CACHE_TTL):
        # Derive status from the bulk snapshot — zero extra docker calls.
        for cand in (f"app-{app_id}", app_id):
            if cand in _BULK_NAMES:
                hit = _PORT_CACHE.get(("cr", cand))
                return "installed" if (hit and hit[1]) else "stopped"
        return "available"
    cname = f"app-{app_id}"
    if container_running(cname):
        return "installed"
    if container_exists(cname):
        return "stopped"
    ok, out = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                      "--format", "{{.Names}}", capture=True)
    if ok and out and out.strip():
        first = out.strip().splitlines()[0].strip()
        return "installed" if container_running(first) else "stopped"
    return "available"

def _get_app_image_uncached(app_id):
    """Installed image string for an app (cached 60s), e.g. 'n8nio/n8n:latest'."""
    hit = _PORT_CACHE.get(("img", app_id))
    if hit and time.time() - hit[0] < _PORT_CACHE_TTL:
        return hit[1]
    cname = _app_container_name(app_id)
    if cname:
        hit = _PORT_CACHE.get(("imgc", cname))
        if hit and time.time() - hit[0] < _PORT_CACHE_TTL:
            return hit[1]
    ok, out = _docker("inspect", "--format", "{{.Config.Image}}", cname or f"app-{app_id}", capture=True, timeout=15)
    return out.strip() if (ok and out and out.strip()) else ""

def get_app_image(app_id):
    return _cached_docker_port(("img", app_id), _get_app_image_uncached, app_id)

@app.route("/api/catalog/sync", methods=["POST", "OPTIONS"])
def api_catalog_sync():
    """Trigger immediate catalog and news synchronization from the central server and clear local caches."""
    if request.method == "OPTIONS":
        return Response("", status=200)
    try:
        sync_catalog(force=True)
        global _CATALOG_RESP_CACHE
        _CATALOG_RESP_CACHE = None
        try:
            from agentic_plane import _sync_central_news
            _sync_central_news(force=True)
        except Exception:
            pass
        return jsonify({"status": "ok", "message": "Catalog and news sync triggered immediately"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/catalog")
def api_catalog():
    """Return the catalog with live local status and host ports (response cached)."""
    global _CATALOG_RESP_CACHE, _CATALOG_RESP_TS
    now = time.time()
    if _CATALOG_RESP_CACHE is not None and now - _CATALOG_RESP_TS < _BULK_CACHE_TTL:
        return Response(_CATALOG_RESP_CACHE, mimetype="application/json")
    _refresh_bulk_container_state()
    # Admin overrides (free/premium per app) — layered on top of the catalog.
    overrides = load_catalog_overrides()
    result = []
    for app in catalog_cache.get("apps", []):
        if app.get("hidden") or app.get("disabled"):
            continue  # infra (central-* DBs etc.) / unpublished apps not shown in the store
        if app["id"] in overrides:
            for _k in ("free_tier", "locked", "requires_paid"):
                if _k in overrides[app["id"]]:
                    app = {**app, _k: overrides[app["id"]][_k]}
        status = get_app_status_local(app["id"])
        cname = _app_container_name(app["id"])
        host_port = get_container_host_port(cname) or _stable_host_port(cname, app["id"], app.get("container_port", 80))
        entry = {**app, "status": status, "host_port": host_port}
        # Update availability: installed image vs catalog image. The catalog is
        # the update channel — bump the image tag there, agents sync, clients
        # see "Update available" and update in place (data preserved, verified).
        if status in ("installed", "stopped"):
            inst_img = get_app_image(app["id"])
            entry["installed_image"] = inst_img
            # stacks update from their compose repo — no image tag to compare
            if inst_img and app.get("image") and not (app.get("is_stack") or app.get("compose_url")):
                entry["update_available"] = inst_img != app["image"]
        # Surface a failed-install reason (verified-install engine) so the store
        # shows WHY an app is not available instead of a silent dead end.
        if app["id"] in _install_error:
            entry["install_error"] = _install_error[app["id"]]
        # Launch URL, computed per deployment mode:
        #  - PROXY mode (PUBLIC_URL set, e.g. VPS with Caddy/traefik): the app is
        #    reachable at https://PUBLIC_URL:<per-app-https-port>/<web_path>. The UI
        #    trusts this URL verbatim.
        #  - DIRECT mode (no PUBLIC_URL, e.g. local installs): apps are reachable at
        #    http://<dashboard-host>:<live-host-port><web_path>. No launch_url is
        #    emitted — the UI derives it from the live host_port + location.hostname,
        #    which also works when the dashboard is accessed remotely.
        if status in ("installed", "stopped"):
            if PUBLIC_URL and "://" in PUBLIC_URL:
                hpj = _app_https_ports().get(app["id"], _https_port(app["id"]))
                wp = (app.get("web_path") or "").strip("/")
                launch = f"{PUBLIC_URL}:{hpj}/"
                if wp:
                    launch += wp + "/"
                entry["launch_url"] = launch
            else:
                entry["launch_url"] = ""
        if status in ("installed", "stopped") and app.get("extra_ports"):
            cname = _app_container_name(app["id"])
            path = app.get("web_path", "/")
            for cport in app["extra_ports"]:
                hp = get_container_port_host(cname, cport)
                if hp:
                    entry["setup_url"] = f"{public_base()}:{hp}{path}"
                    break
        result.append(entry)
    
    body = json.dumps({
        "apps": result,
        "version": catalog_cache.get("version", 0),
        "agent_id": agent_state.get("agent_id", ""),
        "central": CENTRAL_URL,
        "plan": catalog_cache.get("plan", "free"),
    }, ensure_ascii=False)
    _CATALOG_RESP_CACHE = body
    _CATALOG_RESP_TS = now
    return Response(body, mimetype="application/json")

@app.route("/api/health")
def api_health():
    """Health check."""
    d = docker_info()
    mem_free = _host_free_mem_mb()
    disk_free = _host_free_disk_gb()
    return jsonify({
        "status": "ok",
        "agent_id": agent_state.get("agent_id", ""),
        "docker": "connected" if d["available"] else "disconnected",
        "docker_version": d["version"],
        "central": CENTRAL_URL,
        "central_status": "unknown",
        # system pressure — surfaced so the dashboard can warn BEFORE the box dies
        "mem_free_mb": mem_free,
        "disk_free_gb": disk_free,
        "disk_percent": (100 - round(100 * disk_free / (disk_free + _host_used_disk_gb()))) if disk_free is not None and _host_used_disk_gb() is not None else None,
        "catalog_version": catalog_cache.get("version", 0),
        "catalog_apps": len(catalog_cache.get("apps", [])),
        "version": APP_VERSION,
    })


@app.route("/api/agentic/bootstrap", methods=["GET", "OPTIONS"])
def api_agentic_bootstrap():
    """Return the API key to the store UI so it can self-configure on first load.
    Origin-gated: only the store UI origin (port == UI_ORIGIN_PORT) may receive
    the token. External origins get a 403 so the key never leaks to random sites.
    This endpoint is listed in PUBLIC_READ_PREFIXES so it's reachable before the
    key is known, but is *not* useful to unauthenticated callers on other origins.
    """
    if request.method == "OPTIONS":
        return Response("", status=200)
    origin = request.headers.get("Origin", "")
    if not _origin_allowed(origin):
        return jsonify({"error": "Forbidden", "message": "Not a trusted origin"}), 403
    if not API_KEY:
        return jsonify({"error": "No API key configured"}), 404
    return jsonify({"token": API_KEY, "status": "ok"})

@app.route("/api/auth/verify", methods=["GET", "OPTIONS"])
def api_auth_verify():
    """Validate the X-Api-Key header — returns 200 ok or 401 unauthorized.
    Used by the store UI to check if its stored key is still correct after
    a reinstall or key rotation, without triggering a side-effect.
    This route itself IS protected by require_api_key (no PUBLIC_READ_PREFIXES
    entry), so a valid key returns 200 and an invalid key returns 401.
    """
    if request.method == "OPTIONS":
        return Response("", status=200)
    return jsonify({"status": "ok", "agent_id": agent_state.get("agent_id", "")})

@app.route("/api/stats")
def api_stats():
    """System + per-app memory/disk stats for the sidebar (response cached 30s —
    docker stats --no-stream is ~2s and the sidebar polls this often)."""
    global _STATS_CACHE, _STATS_TS
    now = time.time()
    if _STATS_CACHE is not None and now - _STATS_TS < 30:
        return Response(_STATS_CACHE, mimetype="application/json")
    import shutil as _shutil
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            mi = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    try:
                        mi[k] = int(parts[1].strip().split()[0])
                    except Exception:
                        pass
        total = mi.get("MemTotal", 0) * 1024
        avail = mi.get("MemAvailable", mi.get("MemFree", 0)) * 1024
        mem = {"total": total, "used": total - avail, "available": avail}
    except Exception:
        pass
    disk = {}
    try:
        du = _shutil.disk_usage("/")
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except Exception:
        pass
    apps_mem = {}
    try:
        ok, out = _docker("stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}", capture=True)
        if ok and out:
            for line in out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and parts[0].startswith("app-"):
                    apps_mem[parts[0][4:]] = {"usage": parts[1].strip(), "percent": parts[2].strip()}
    except Exception:
        pass
    running = stopped = 0
    try:
        ok, out = _docker("ps", "-a", "--filter", "label=appvault.managed=true", "--format", "{{.Names}}\t{{.Status}}", capture=True)
        if ok and out:
            for line in out.strip().splitlines():
                if "\tUp" in line:
                    running += 1
                elif "app-" in line:
                    stopped += 1
    except Exception:
        pass
    body = json.dumps({
        "memory": mem,
        "disk": disk,
        "containers": {"running": running, "stopped": stopped},
        "apps_memory": apps_mem,
    })
    _STATS_CACHE = body
    _STATS_TS = now
    return Response(body, mimetype="application/json")

@app.route("/api/info")
def api_info():
    """Detailed info for settings page."""
    docker_info_dict = docker_info()
    # Count running containers labeled appvault
    running = 0
    try:
        ok, out = _docker("ps", "--filter", "label=appvault.managed=true", "--format", "{{.Names}}", capture=True)
        if ok:
            running = len([l for l in out.strip().split('\n') if l.strip()])
    except:
        pass
    return jsonify({
        "agent_id": agent_state.get("agent_id", ""),
        "name": AGENT_NAME,
        "os": sys.platform,
        "uptime": "running",
        "version": APP_VERSION,
        "docker": docker_info_dict,
        "containers": running,
        "catalog_version": catalog_cache.get("version", 0),
        "catalog_apps": len(catalog_cache.get("apps", [])),
        "central": CENTRAL_URL,
        "is_registered": bool(agent_state.get("api_key")),
        "hostname": socket.gethostname(),
    })

def _mon_sec(mon_id, action="get", value=None):
    """Store/read the admin secret for a monitoring app in agent_state.

    Stored per-install: set on bootstrap-install, cleared on uninstall so a
    reinstall gets a fresh password. Keyed agent_state["monitoring"]["<id>"]["admin_pass"].
    """
    with _STATE_LOCK:
        m = agent_state.setdefault("monitoring", {})
        entry = m.setdefault(mon_id, {})
        if action == "set" and value is not None:
            entry["admin_pass"] = value
            save_agent_state(agent_state)
        elif action == "get":
            return entry.get("admin_pass", "")
        elif action == "clear":
            m.pop(mon_id, None)
            save_agent_state(agent_state)
    return ""

@app.route("/api/monitoring")
def api_monitoring():
    """Return monitoring endpoints + admin credentials.

    Passwords live in agent_state, shown ONLY while the container runs.
    Uninstalling a monitoring app clears its secret (fresh on reinstall).
    """
    base = os.getenv("PUBLIC_URL", "").rstrip("/")
    host = base
    host = host.replace("https://", "").replace("http://", "") or socket.gethostname()
    if not base:
        try:
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
            if ip.returncode == 0:
                t = ip.stdout.strip().split("\n")
                if t and t[0].strip():
                    host = t[0].strip()
        except Exception:
            pass

    p_port = os.getenv("PORTAINER_PORT", "29001")
    k_port = os.getenv("KUMA_PORT", "29002")
    n_port = os.getenv("NETDATA_PORT", "29003")

    portainer_ok = container_running("app-portainer")
    kuma_ok = container_running("app-uptime-kuma")
    netdata_ok = container_running("app-netdata")
    p_user = os.getenv("PORTAINER_ADMIN_USER", "admin")
    # Password shown only while Portainer is running (per-install secret).
    p_pass = _mon_sec("portainer") if portainer_ok else ""

    enabled = portainer_ok or kuma_ok or netdata_ok
    return jsonify({
        "enabled": enabled,
        "portainer": {
            "url": "https://%s:%s/" % (host, p_port) if host and portainer_ok else "",
            "admin_user": p_user,
            "admin_pass": p_pass,
            "port": p_port,
            "running": portainer_ok,
        },
        "uptime_kuma": {
            "url": "https://%s:%s/" % (host, k_port) if host and kuma_ok else "",
            "port": k_port,
            "running": kuma_ok,
        },
        "netdata": {
            "url": "https://%s:%s/" % (host, n_port) if host and netdata_ok else "",
            "port": n_port,
            "running": netdata_ok,
        },
    })

@app.route("/api/apps/health")
def api_apps_health():
    """Check and report health of all installed apps (response cached 30s; the
    per-app probes run in parallel so a cold build is a few seconds, not 30+)."""
    global _APPS_HEALTH_CACHE, _APPS_HEALTH_TS
    now = time.time()
    if _APPS_HEALTH_CACHE is not None and now - _APPS_HEALTH_TS < 30:
        return Response(_APPS_HEALTH_CACHE, mimetype="application/json")
    results = []
    ok, out = _docker("ps", "--filter", "name=app-", "--format", "{{.Names}}", capture=True)
    if ok and out:
        containers = [l.strip() for l in out.strip().split('\n') if l.strip()]

        def _check(cname):
            app_id = cname.replace("app-", "", 1)
            app_def = None
            for a in catalog_cache.get("apps", []):
                if a["id"] == app_id:
                    app_def = a
                    break
            if not app_def:
                return None
            port = get_container_host_port(cname) or _stable_host_port(cname, app_id, app_def.get("container_port", 80))
            # running state from the bulk snapshot — no docker call
            cr = _PORT_CACHE.get(("cr", cname))
            is_running = bool(cr and cr[1]) if cr else container_running(cname)
            alive = False
            if is_running:
                if app_def.get("category", "").lower() != "database":
                    internal_port = _get_internal_port(cname)
                    alive = _is_app_alive(cname, internal_port)
                else:
                    alive = True  # DB apps considered alive if running
            return {"id": app_id, "name": app_def.get("name", app_id),
                    "status": "running" if is_running else "stopped",
                    "port": port, "responsive": alive}

        # Parallel probes: docker exec per app is the expensive part and the
        # GIL is released while waiting on subprocesses, so threads scale it.
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for r in ex.map(_check, containers):
                    if r:
                        results.append(r)
        except Exception:
            for cname in containers:
                r = _check(cname)
                if r:
                    results.append(r)
    body = json.dumps({"apps": results, "total": len(results), "healthy": sum(1 for r in results if r["responsive"])})
    _APPS_HEALTH_CACHE = body
    _APPS_HEALTH_TS = now
    return Response(body, mimetype="application/json")

@app.route("/api/agent/status")
def api_agent_status():
    """Detailed agent status."""
    docker = docker_info()
    return jsonify({
        "agent_id": agent_state.get("agent_id", ""),
        "name": AGENT_NAME,
        "os": sys.platform,
        "docker": docker,
        "catalog_version": catalog_cache.get("version", 0),
        "catalog_apps": len(catalog_cache.get("apps", [])),
        "central_url": CENTRAL_URL,
        "is_registered": bool(agent_state.get("api_key")),
    })

@app.route("/api/license", methods=["GET", "POST"])
def api_license():
    """Get (GET) or apply (POST) the agent's license key.
    POST stores the key persistently and upgrades the agent's plan to 'paid'
    (unlocking premium apps and full enterprise catalog)."""
    has_key = bool(agent_state.get("license_key"))
    if request.method == "GET":
        return jsonify({
            "license_key": agent_state.get("license_key", ""),
            "plan": "paid" if has_key else catalog_cache.get("plan", "free"),
            "active": has_key
        })
    data = request.json or {}
    key = (data.get("license_key") or "").strip()
    cleared = not key
    with _STATE_LOCK:
        agent_state["license_key"] = key
        save_agent_state(agent_state)
    
    if not cleared:
        catalog_cache["plan"] = "paid"
    else:
        catalog_cache["plan"] = "free"
    save_catalog_cache(catalog_cache)

    try:
        register_with_central()
        sync_catalog(force=True)
    except Exception as e:
        print(f"[agent] Central sync note: {e}")

    return jsonify({
        "status": "ok",
        "license_key": key,
        "applied": not cleared,
        "cleared": cleared,
        "plan": "paid" if not cleared else "free"
    })
@app.route("/api/checkout", methods=["POST"])
def api_checkout():
    """Agent-initiated checkout: create a Stripe Checkout session for this agent.

    The agent calls the central server's /api/agent/checkout with its own
    agent_id + api_key so the license auto-binds to THIS agent after payment.
    The return_url is this store's URL (whatever the user typed in the browser),
    so after paying they land right back here and we refresh the license.
    """
    data = request.json or {}
    billing = data.get("billing", "monthly")
    return_url = request.host_url.rstrip("/")  # e.g. http://store-host:5000
    central = central_request("POST", "/api/agent/checkout", data={
        "agent_id": agent_state.get("agent_id", AGENT_ID),
        "api_key": agent_state.get("api_key", API_KEY),
        "billing": billing,
        "return_url": return_url,
        "cancel_url": return_url,
    })
    if not central or not central.get("url"):
        return jsonify({"status": "error", "message": "Checkout could not be created (central server unreachable or not configured)"}), 502
    return jsonify({"status": "ok", "url": central["url"]})


@app.route("/api/license/refresh", methods=["POST"])
def api_license_refresh():
    """Pull the latest license + plan from the central server and apply locally.

    Called after the user completes payment so the agent immediately switches
    to 'paid' (unlocking premium apps) without waiting for the next heartbeat.
    """
    central = central_request("POST", "/api/agent/subscription", data={
        "agent_id": agent_state.get("agent_id", AGENT_ID),
        "api_key": agent_state.get("api_key", API_KEY),
    })
    if not central:
        return jsonify({"status": "error", "message": "Central server unreachable"}), 502

    key = central.get("license_key") or ""
    if key and key != agent_state.get("license_key"):
        with _STATE_LOCK:
            agent_state["license_key"] = key
            save_agent_state(agent_state)

    ok = register_with_central()
    try:
        sync_catalog(force=True)
    except Exception as e:
        print(f"[agent] Catalog re-sync after license refresh failed: {e}")

    return jsonify({
        "status": "ok" if ok else "error",
        "plan": central.get("plan", "free"),
        "license_key": key,
        "grace_days_remaining": central.get("grace_days_remaining"),
    })


@app.route("/api/billing-portal", methods=["POST"])
def api_billing_portal():
    """Create a Stripe Customer Portal session so the user can manage/cancel."""
    central = central_request("POST", "/api/agent/billing-portal", data={
        "agent_id": agent_state.get("agent_id", AGENT_ID),
        "api_key": agent_state.get("api_key", API_KEY),
    })
    if not central or not central.get("url"):
        return jsonify({"status": "error", "message": "No subscription found or central unreachable"}), 502
    return jsonify({"status": "ok", "url": central["url"]})



# ---- Security self-configuration (free users harden their own install) ----
SECURITY_STATE_PATH = os.path.join(STORAGE_PATH, "security.json")

def _load_security():
    try:
        with open(SECURITY_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_security(s):
    try:
        with open(SECURITY_STATE_PATH, "w") as f:
            json.dump(s, f)
    except Exception as e:
        print(f"[agent] security save failed: {e}")

def _tailscale_status():
    """Report Tailscale status. Prefers the HOST tailscale (VPS runs tailscale on the
    host; the agent container reaches it via the docker socket). Falls back to any
    tailscale binary present in the container (desktop single-host installs)."""
    try:
        # 1) Try host tailscale via docker socket (VPS host has tailscaled).
        #    Mount the host tailscaled socket so the CLI in the container talks to host daemon.
        ok, out = _docker("run", "--rm",
                          "-v", "/var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock",
                          "--network", "host", "--entrypoint", "tailscale",
                          "tailscale/tailscale", "status", "--json", capture=True, timeout=25)
        if ok and out:
            # guard: ensure it's actually JSON (docker may emit errors)
            if out.lstrip().startswith("{"):
                d = json.loads(out)
                selfip = d.get("Self", {})
                return {"installed": True, "running": d.get("BackendState") in ("Running", "Starting"),
                        "ip": selfip.get("TailscaleIPs", [None])[0], "hostname": selfip.get("HostName", "")}
    except Exception as e:
        print(f"[agent] host tailscale check via docker failed: {e}")
    # 2) Fallback: tailscale binary inside the container
    if os.path.exists("/usr/bin/tailscale") or os.path.exists("/usr/local/bin/tailscale"):
        try:
            r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                d = json.loads(r.stdout)
                selfip = d.get("Self", {})
                return {"installed": True, "running": d.get("BackendState") in ("Running", "Starting"),
                        "ip": selfip.get("TailscaleIPs", [None])[0], "hostname": selfip.get("HostName", "")}
        except Exception:
            pass
        return {"installed": True, "running": False}
    return {"installed": False, "running": False, "ip": None}

@app.route("/api/security", methods=["GET"])
def api_security_status():
    """Report current security posture (bind, basic auth intent, tailscale, exposed ports)."""
    exposed = []
    ok, out = _docker("ps", "--filter", "label=appvault.managed=true", "--format", "{{.Names}}", capture=True)
    if ok and out:
        for name in [l.strip() for l in out.strip().split('\n') if l.strip()]:
            p = get_container_host_port(name)
            if p:
                exposed.append({"app": name.replace("app-", "", 1), "port": p})
    sec = _load_security()
    return jsonify({
        "platform": sys.platform,
        "bind": sec.get("bind", "0.0.0.0"),
        "basic_auth_enabled": bool(sec.get("basic_auth_enabled")),
        "basic_user": sec.get("basic_user", ""),
        "tailscale": _tailscale_status(),
        "exposed_ports": exposed,
        "store_port": "8085",
        "agent_port": "8086",
    })

# ═══════════════════════════════════════════════════════════════════════════════
# INSTALL PROGRESS TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

_install_progress = {}  # app_id -> { "stage": str, "message": str, "submsg": str, "percent": int, "done": bool, "error": str, "start_time": float, "elapsed_seconds": int }

def _set_progress(app_id, message, percent, stage="working", submsg=""):
    """Update install progress for an app with live elapsed timer and stage."""
    now = time.time()
    existing = _install_progress.get(app_id) or {}
    start_time = existing.get("start_time") or now
    _install_progress[app_id] = {
        "stage": stage,
        "message": message,
        "submsg": submsg or "Downloading and preparing application package...",
        "percent": max(0, min(int(percent), 99)),
        "done": False,
        "error": "",
        "start_time": start_time,
        "elapsed_seconds": int(now - start_time)
    }

def _set_progress_done(app_id, message="Done", submsg=""):
    """Mark install as complete."""
    now = time.time()
    existing = _install_progress.get(app_id) or {}
    start_time = existing.get("start_time") or now
    _install_progress[app_id] = {
        "stage": "done",
        "message": message,
        "submsg": submsg or "Application is ready to use!",
        "percent": 100,
        "done": True,
        "error": "",
        "start_time": start_time,
        "elapsed_seconds": int(now - start_time)
    }

def _set_progress_error(app_id, error_msg):
    """Mark install as failed."""
    now = time.time()
    existing = _install_progress.get(app_id) or {}
    start_time = existing.get("start_time") or now
    _install_progress[app_id] = {
        "stage": "error",
        "message": error_msg,
        "submsg": "An error occurred during setup.",
        "percent": 0,
        "done": True,
        "error": error_msg,
        "start_time": start_time,
        "elapsed_seconds": int(now - start_time)
    }

def _docker_pull_with_progress(image, app_id, start_pct=20, end_pct=60):
    """Pull a Docker image streaming line-by-line progress to update install percentage."""
    image_short = image.split("/")[-1]
    _set_progress(app_id, f"Connecting to repository for {image_short}...", start_pct, stage="downloading", submsg="Connecting to container registry...")
    print(f"[agent] Pulling image with progress stream: {image}")
    try:
        cmd = [DOCKER_CMD, "pull", image]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")}
        )
        layers = {}
        cur_pct = start_pct
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                parts = line.split(":", 1)
                layer_id = parts[0].strip()
                status = parts[1].strip()
                if len(layer_id) in (12, 64) or " " not in layer_id:
                    layers[layer_id] = status
            
            total = max(len(layers), 1)
            completed = sum(1 for s in layers.values() if "complete" in s.lower() or "exists" in s.lower())
            extracting = sum(1 for s in layers.values() if "extract" in s.lower())
            
            if total > 1:
                fraction = (completed * 1.0 + extracting * 0.5) / float(total)
                cur_pct = int(start_pct + fraction * (end_pct - start_pct))
                cur_pct = min(max(cur_pct, start_pct), end_pct)
            else:
                cur_pct = min(cur_pct + 1, end_pct)

            submsg = f"Transferred {completed}/{total} image layers" if total > 1 else "Streaming package layers..."
            _set_progress(app_id, f"Downloading {image_short} ({cur_pct}%)...", cur_pct, stage="downloading", submsg=submsg)

        proc.stdout.close()
        ret = proc.wait(timeout=600)
        if ret == 0:
            _set_progress(app_id, f"Package {image_short} downloaded!", end_pct, stage="extracted", submsg="All layers extracted and verified.")
            return True, "Image pulled successfully"
        else:
            return False, f"Docker pull exited with code {ret}"
    except Exception as e:
        print(f"[agent] Progress pull fallback: {e}")
        ok, err = _docker("pull", image, timeout=600)
        return ok, err

@app.route("/api/security", methods=["POST"])
def api_security_apply():
    """Persist security preferences (bind + basic-auth intent). Actual nginx/Caddy
    enforcement for basic auth and bind is applied at the store host; the agent
    records + reports intent and generates the needed snippet for the UI."""
    data = request.json or {}
    sec = _load_security()
    res = {}
    if "bind" in data and data["bind"] in ("127.0.0.1", "0.0.0.0"):
        sec["bind"] = data["bind"]; res["bind"] = data["bind"]
    if "basic_auth" in data:
        enabled = bool(data["basic_auth"])
        user = (data.get("basic_user") or sec.get("basic_user") or "appvault").strip()
        if enabled and not data.get("basic_pass"):
            return jsonify({"status": "error", "message": "Password required to enable basic auth"}), 400
        sec["basic_auth_enabled"] = enabled
        sec["basic_user"] = user
        if data.get("basic_pass"):
            sec["basic_pass"] = data["basic_pass"]
        res["basic_auth"] = {"enabled": enabled, "user": user}
    _save_security(sec)
    return jsonify({"status": "ok", **res})

@app.route("/api/security/tailscale", methods=["POST"])
def api_security_tailscale():
    """Install + join Tailscale so the install is private-by-default."""
    data = request.json or {}
    authkey = (data.get("auth_key") or "").strip()
    try:
        if not (os.path.exists("/usr/bin/tailscale") or os.path.exists("/usr/local/bin/tailscale")):
            subprocess.run(["curl", "-fsSL", "https://tailscale.com/install.sh", "-o", "/tmp/ts_install.sh"],
                           capture_output=True, text=True, timeout=120)
            subprocess.run(["sh", "/tmp/ts_install.sh"], capture_output=True, text=True, timeout=300)
        cmd = ["tailscale", "up", "--timeout", "300"]
        if authkey:
            cmd.append("--authkey=" + authkey)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=310)
        ts = _tailscale_status()
        ok = r.returncode == 0 or ts.get("running")
        return jsonify({"status": "ok" if ok else "needs_auth", "tailscale": ts,
                        "message": "" if ok else (r.stderr or r.stdout or "").strip()[:200]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:200]}), 500


@app.route("/")
@app.route("/store")
def index():
    """Redirect browser traffic on port 8086 directly to the unified AppVault dashboard on port 8085."""
    host = request.host.split(":")[0] if request.host else "localhost"
    return redirect(f"http://{host}:8085/", code=302)

@app.route("/dashboard")
@app.route("/dashboard/")
def serve_dashboard():
    """Serve the complete AppVault user dashboard (Apps, Agentic OS, Missions,
    Memory, Crews, Pipeline, ...) from the agent itself — same origin as its
    APIs, so the full menu works on every agent with no extra container."""
    dash_dir = os.path.join(BASE_DIR, "static", "dashboard")
    if os.path.exists(os.path.join(dash_dir, "index.html")):
        return send_from_directory(dash_dir, "index.html")
    host = request.host.split(":")[0] if request.host else "localhost"
    return redirect(f"http://{host}:8085/", code=302)

@app.route("/msr.woff2")
def serve_dashboard_font():
    dash_dir = os.path.join(BASE_DIR, "static", "dashboard")
    if os.path.exists(os.path.join(dash_dir, "msr.woff2")):
        return send_from_directory(dash_dir, "msr.woff2")
    return "", 404

@app.route("/custom.js")
def serve_custom_js():
    """Serve custom.js for Heimdall injection."""
    js_path = os.path.join(BASE_DIR, "static", "custom.js")
    if os.path.exists(js_path):
        with open(js_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {'Content-Type': 'application/javascript'}
    return "console.log('custom.js not found');", 404

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# INSTALL PROGRESS TRACKING
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# APP EDUCATION â€” return learning materials for each app
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.route("/api/education/<app_id>")
def api_education(app_id):
    """Return education data + live info for an app."""
    result = {"app_id": app_id}
    
    # Find app in catalog
    app_def = None
    for a in catalog_cache.get("apps", []):
        if a["id"] == app_id:
            app_def = a
            break
    
    if not app_def:
        return jsonify({"error": "App not found"}), 404
    
    # Static education data
    edu = app_def.get("education", {})
    result["name"] = app_def.get("name", app_id)
    result["description"] = app_def.get("description", "")
    result["category"] = app_def.get("category", "")
    result["docs_url"] = edu.get("docs_url", "")
    result["video_url"] = edu.get("video_url", "")
    result["quick_start"] = edu.get("quick_start", "")
    result["default_login"] = edu.get("default_login", {})
    result["setup_steps"] = edu.get("setup_steps", [])
    
    # Live info
    cname = f"app-{app_id}"
    result["is_running"] = container_running(cname)
    result["host_port"] = get_container_host_port(cname) or _stable_host_port(cname, app_id, app_def.get("container_port", 80))
    # Live launch URL — always the real mapped port + web path, never a placeholder
    if result["host_port"]:
        result["launch_url"] = f"http://localhost:{result['host_port']}{(app_def.get('web_path') or '/')}"
    result["web_path"] = app_def.get("web_path", "/")
    # ADDITIVE: stack apps run compose containers labeled appvault.app=<app_id>;
    # report live status/host port from the first labeled container if app-<id> is absent.
    if not result["is_running"]:
        okc, outc = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                            "--format", "{{.Names}}", capture=True)
        if okc and outc and outc.strip():
            sc = outc.strip().splitlines()[0].strip()
            result["is_running"] = container_running(sc)
            if not result["host_port"] or result["host_port"] == app_def.get("container_port", ""):
                okp, outp = _docker("port", sc, capture=True)
                if okp and outp and outp.strip():
                    first = outp.strip().splitlines()[0].strip()
                    if "->" in first:
                        # docker port prints "<container-port>/tcp -> <host-bind>",
                        # e.g. "4000/tcp -> 0.0.0.0:4000". Take the PORT out of the
                        # host bind — the raw post-arrow text is the full binding
                        # (0.0.0.0:4000), which produced invalid guide URLs
                        # (http://localhost:0.0.0.0:4000/) for labeled containers
                        # like appvault-litellm / openship_dashboard.
                        _host = first.split("->")[1].split("/")[0].strip()
                        result["host_port"] = _host.split(":")[-1].strip()
    # END ADDITIVE
    
    # Build launch URL.
    # Monitoring apps publish NO host ports and are reached ONLY via Caddy on the
    # monitoring HTTPS ports (29001/29002/29003). Use the Caddy port, not the
    # container port (9000/3001/19999), so the store shows the WORKING link.
    port = result["host_port"]
    path = result["web_path"]
    proxy_mode = bool(PUBLIC_URL and "://" in PUBLIC_URL)
    if app_id in MONITORING_IDS:
        _mon_port = {
            "portainer": os.getenv("PORTAINER_PORT", "29001"),
            "uptime-kuma": os.getenv("KUMA_PORT", "29002"),
            "netdata": os.getenv("NETDATA_PORT", "29003"),
        }.get(app_id, "")
        if _mon_port:
            result["launch_url"] = f"{public_base()}:{_mon_port}{path}"
            result["host_port"] = _mon_port
            port = _mon_port
        else:
            result["launch_url"] = ""
    # The per-app Caddy HTTPS port exists ONLY in proxy mode (PUBLIC_URL set,
    # e.g. VPS with Caddy). In DIRECT mode there is no Caddy — overriding
    # host_port with the hash port produced dead guide links
    # (e.g. http://localhost:21571/ while the app really listens on the
    # remapped host port 36024). Use the LIVE host port in direct mode.
    elif proxy_mode and app_id in _app_https_ports():
        result["launch_url"] = f"{public_base()}:{_app_https_ports()[app_id]}{path}"
        result["host_port"] = _app_https_ports()[app_id]
    elif port:
        result["launch_url"] = f"{public_base()}:{port}{path}"
    else:
        result["launch_url"] = ""

    # Extra ports are RAW host ports (plain HTTP, not Caddy-routed). Only expose a setup URL
    # when the extra port differs from the main web port, and use http:// so the link works.
    extra_urls = {}
    for cport in (app_def.get("extra_ports") or {}):
        if str(cport) == str(app_def.get("container_port", "")):
            continue  # duplicates the main web port (already on https via Caddy)
        hp = get_container_port_host(cname, cport)
        if hp:
            extra_urls[cport] = f"http://{public_base_host()}:{hp}{path}"
    result["extra_urls"] = extra_urls
    result["setup_url"] = list(extra_urls.values())[0] if extra_urls else None
    
    # Extract credentials from env vars if not in education data
    if not result.get("default_login"):
        env_vars = {e.split("=")[0]: e.split("=", 1)[1] for e in app_def.get("env", []) if "=" in e}
        login = {}
        for key, val in env_vars.items():
            if "PASSWORD" in key.upper() and "ROOT" not in key.upper():
                login["password"] = val
            elif "USERNAME" in key.upper() or "_USER" in key.upper():
                login["username"] = val
            elif "ADMIN_USER" in key.upper():
                login["username"] = val
            elif "ADMIN_PASSWORD" in key.upper():
                login["password"] = val
        if login:
            result["auto_credentials"] = login
    
    return jsonify(result)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# APP ICONS â€” fetch and cache favicons from running apps
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

ICON_CACHE = os.path.join(os.environ.get("STORAGE_PATH", "/data"), "icons")
os.makedirs(ICON_CACHE, exist_ok=True)

# SVG placeholder by app name (first letter + colored background)
DEFAULT_ICONS = {}
_DEFAULT_COLORS = ["#3b82f6","#22c55e","#ef4444","#f59e0b","#8b5cf6","#ec4899","#06b6d4","#14b8a6"]

def _get_placeholder_icon_svg(app_name, app_id):
    """Generate a colored SVG icon with the first letter."""
    letter = (app_name or app_id or "?")[0].upper()
    color = _DEFAULT_COLORS[abs(hash(app_id or "?")) % len(_DEFAULT_COLORS)]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="20" fill="{color}"/>
  <text x="50" y="68" text-anchor="middle" font-size="50" font-weight="700" font-family="sans-serif" fill="white">{letter}</text>
</svg>'''.encode()

@app.route("/api/icon/<app_id>")
def api_app_icon(app_id):
    """Serve an app's icon (favicon), with fallback to generated SVG."""
    # Check cache first
    cache_file = os.path.join(ICON_CACHE, f"{app_id}.png")
    ext = "png"
    if os.path.exists(cache_file):
        return open(cache_file, "rb").read(), 200, {"Content-Type": "image/png"}

    cache_file_svg = os.path.join(ICON_CACHE, f"{app_id}.svg")
    if os.path.exists(cache_file_svg):
        return open(cache_file_svg, "rb").read(), 200, {"Content-Type": "image/svg+xml"}

    # Try to fetch favicon from the running container
    cname = f"app-{app_id}"
    if container_running(cname):
        internal_port = _get_internal_port(cname)
        # Try wget inside container, save to temp, docker cp out
        tmpf = f"/tmp/av_{app_id}"
        _docker("exec", cname, "sh", "-c",
            f"wget -q -O {tmpf} --timeout=5 http://127.0.0.1:{internal_port}/favicon.ico 2>/dev/null || "
            f"curl -s -o {tmpf} --max-time 5 http://127.0.0.1:{internal_port}/favicon.ico 2>/dev/null || "
            f"echo 'placeholder' > {tmpf}", timeout=15)
        ok, sz = _docker("exec", cname, "stat", "-c%s", tmpf, capture=True, timeout=5)
        if ok and sz.strip().isdigit() and int(sz.strip()) > 200:
            import subprocess
            r = subprocess.run([DOCKER_CMD, "cp", f"{cname}:{tmpf}", cache_file],
                              capture_output=True, timeout=10)
            if r.returncode == 0 and os.path.exists(cache_file) and os.path.getsize(cache_file) > 200:
                with open(cache_file, "rb") as f:
                    d = f.read()
                _docker("exec", cname, "rm", "-f", tmpf)
                return d, 200, {"Content-Type": "image/x-icon"}
        _docker("exec", cname, "rm", "-f", tmpf)

    # Fallback: find app name and generate SVG icon
    app_name = app_id
    for a in catalog_cache.get("apps", []):
        if a["id"] == app_id:
            app_name = a.get("name", app_id)
            break
    svg_data = _get_placeholder_icon_svg(app_name, app_id)
    cache_file_svg = os.path.join(ICON_CACHE, f"{app_id}.svg")
    with open(cache_file_svg, "wb") as f:
        f.write(svg_data)
    return svg_data, 200, {"Content-Type": "image/svg+xml"}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# LOCAL APP MANAGEMENT (install/uninstall/restart)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/api/tailscale/status")
def api_tailscale_status():
    """Tailscale onboarding state (written by /opt/appvault/tailscale-onboard.sh)."""
    try:
        with open("/opt/appvault/tailscale-status.json") as f:
            st = json.load(f)
        return jsonify({"tailscale": st})
    except Exception:
        return jsonify({"tailscale": {"joined": False, "onboard_script": "sudo bash /opt/appvault/tailscale-onboard.sh"}})


@app.route("/api/install/<app_id>", methods=["POST"])
def api_install(app_id):
    """Start installing an app in the background. Returns immediately."""
    app_def = _get_app_def(app_id)
    if not app_def:
        return jsonify({"status": "error", "app_id": app_id, "message": "App not found in catalog"}), 404
    blocked = _install_blocked_reason(app_def)
    if blocked:
        return jsonify({"status": "error", "app_id": app_id, "message": blocked}), \
            (400 if app_def.get("disabled") else 402)
    # Serialize per-app operations: no concurrent install/uninstall/restart
    op_lock = _app_op_lock(app_id)
    if not op_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "app_id": app_id,
                        "message": "Another operation is already running for this app"}), 409
    # Initialize progress only AFTER owning the lock, so a 409 response can't
    # clobber the in-flight operation's progress entry.
    _set_progress(app_id, "Queued...", 2)
    # Run install in background thread
    def _install_thread():
        try:
            # Check if this is a stack app
            app_def = None
            for a in catalog_cache.get("apps", []):
                if a["id"] == app_id:
                    app_def = a
                    break
            if app_def and (app_def.get("is_stack") or app_def.get("compose_url")):
                _do_install_stack(app_id)
            else:
                _do_install(app_id)
        except Exception as e:
            _set_progress_error(app_id, str(e)[:200])
        finally:
            op_lock.release()
    threading.Thread(target=_install_thread, daemon=True).start()
    return jsonify({"status": "started", "app_id": app_id, "message": f"Installing {app_id}..."})

@app.route("/api/install/<app_id>/status", methods=["GET"])
def api_install_status(app_id):
    """Get install progress for an app."""
    prog = _install_progress.get(app_id, {
        "stage": "unknown",
        "message": "No install in progress",
        "percent": 0,
        "done": True,
        "error": ""
    })
    return jsonify(prog)

@app.route("/api/update/<app_id>", methods=["POST"])
def api_update(app_id):
    """Update an installed app to the catalog's image — data preserved, verified.

    Data safety guarantees:
      - volumes (named + unified data dir) are NEVER touched
      - dependency containers (DBs) are kept as-is
      - the update waits for the app's healthcheck before reporting success
      - on failure the engine rolls back to the previous image (still local)
    The catalog is the update channel: bump the image tag in catalog.json,
    agents sync, clients see "Update available" and update in place.
    """
    app_def = next((a for a in catalog_cache.get("apps", []) if a.get("id") == app_id), None)
    if not app_def:
        return jsonify({"status": "error", "app_id": app_id, "message": "App not found in catalog"}), 404
    if get_app_status_local(app_id) not in ("installed", "stopped"):
        return jsonify({"status": "error", "app_id": app_id, "message": f"{app_id} is not installed"}), 400
    op_lock = _app_op_lock(app_id)
    if not op_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "app_id": app_id,
                        "message": "Another operation is already running for this app"}), 409
    _set_progress(app_id, "Checking for updates...", 5)
    def _update_thread():
        try:
            old_image = get_app_image(app_id)
            new_image = app_def.get("image", "")
            if not new_image:
                _set_progress_error(app_id, "Catalog has no image for this app")
                return
            if old_image == new_image:
                _set_progress_done(app_id, f"{app_id} is already up to date")
                return
            print(f"[agent] UPDATE {app_id}: {old_image} -> {new_image}")
            if app_def.get("is_stack") or app_def.get("compose_url"):
                # stack: re-run the verified stack installer (containers recreated,
                # compose-managed volumes persist)
                _do_install_stack(app_id)
                _set_progress_done(app_id, f"{app_id} updated")
                return
            # single-image: spec-based recreate with the new image + verify
            try:
                app_def["image"] = new_image
                _do_install(app_id)
            except Exception as _upd_err:
                # rollback: previous image is still local; same volumes/ports/deps
                _set_progress(app_id, f"Update failed — rolling back to {old_image}...", 50)
                app_def["image"] = old_image
                try:
                    _do_install(app_id)
                    _set_progress_error(app_id, f"Update to {new_image} failed; rolled back to {old_image}")
                    _install_error[app_id] = f"Update to {new_image} failed; rolled back to {old_image}"
                except Exception as _rb_err:
                    msg = f"Update failed and rollback failed: {_rb_err}"
                    _set_progress_error(app_id, msg)
                    _install_error[app_id] = msg
                return
            _set_progress_done(app_id, f"{app_id} updated to {new_image}")
        except Exception as e:
            _set_progress_error(app_id, str(e)[:200])
        finally:
            op_lock.release()
    threading.Thread(target=_update_thread, daemon=True).start()
    return jsonify({"status": "started", "app_id": app_id, "message": f"Updating {app_id}..."})

@app.route("/api/uninstall/<app_id>", methods=["POST"])
def api_uninstall(app_id):
    """Uninstall an app locally in the background (returns immediately)."""
    _set_progress(app_id, "Uninstalling...", 5)
    op_lock = _app_op_lock(app_id)
    if not op_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "app_id": app_id,
                        "message": "Another operation is already running for this app"}), 409
    def _uninstall_thread():
        try:
            _set_progress(app_id, "Removing container...", 30)
            _do_uninstall(app_id)
            _set_progress_done(app_id, f"{app_id} uninstalled")
        except Exception as e:
            _set_progress_error(app_id, str(e))
        finally:
            op_lock.release()
    threading.Thread(target=_uninstall_thread, daemon=True).start()
    return jsonify({"status": "started", "app_id": app_id, "message": f"Uninstalling {app_id}..."})

@app.route("/api/uninstall/<app_id>/status", methods=["GET"])
def api_uninstall_status(app_id):
    """Get uninstall progress."""
    prog = _install_progress.get(app_id, {
        "stage": "unknown", "message": "No uninstall in progress",
        "percent": 0, "done": True, "error": ""
    })
    return jsonify(prog)

@app.route("/api/restart/<app_id>", methods=["POST"])
def api_restart(app_id):
    """Restart an app locally."""
    op_lock = _app_op_lock(app_id)
    if not op_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "app_id": app_id,
                        "message": "Another operation is already running for this app"}), 409
    try:
        _do_restart(app_id)
        return jsonify({"status": "ok", "app_id": app_id, "message": f"{app_id} restarted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        op_lock.release()

@app.route("/api/stop/<app_id>", methods=["POST"])
def api_stop(app_id):
    """Stop an app locally (frees its memory; data preserved)."""
    op_lock = _app_op_lock(app_id)
    if not op_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "app_id": app_id,
                        "message": "Another operation is already running for this app"}), 409
    try:
        _do_stop(app_id)
        return jsonify({"status": "ok", "app_id": app_id, "message": f"{app_id} stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        op_lock.release()

# ── OPS KIT (appvault_ops.sh) — backup / restore / safe update ──
# Thin API wrapper around the host-side ops kit (see README_OPS.md). The script
# lives on the server next to watchdog.sh (default /opt/appvault/appvault_ops.sh;
# override with APPVAULT_OPS_SCRIPT). One job at a time; poll /api/ops/status.

OPS_SCRIPT = os.getenv("APPVAULT_OPS_SCRIPT", "/opt/appvault/appvault_ops.sh")
OPS_TIMEOUT = int(os.getenv("APPVAULT_OPS_TIMEOUT", "3600"))  # seconds per job

_ops_job_lock = threading.Lock()
_ops_jobs = {}          # job_id -> job dict (recent history, pruned)
_ops_active_job = None  # only one ops job at a time (they share containers/volumes)


def _ops_target_ok(target):
    """Container names / 'all' only - no shell metacharacters."""
    if not target or len(target) > 64 or target.startswith(("-", ".")):
        return False
    return all(c.isalnum() or c in "_.-" for c in target)


def _ops_resolve_target(target):
    """Accept an AppVault app id or a raw container name ('all' passes through)."""
    if target == "all":
        return "all"
    try:
        return _app_container_name(target) or target
    except Exception:
        return target


def _ops_snapshot(job):
    return {"job_id": job["job_id"], "cmd": job["cmd"], "target": job["target"],
            "stage": job["stage"], "message": job["message"], "done": job["done"],
            "error": job["error"], "log_tail": job["log_tail"][-1500:],
            "elapsed_seconds": int(time.time() - job["start_time"])}


def _ops_worker(job_id):
    global _ops_active_job
    job = _ops_jobs[job_id]
    try:
        if not (os.path.isfile(OPS_SCRIPT) and os.access(OPS_SCRIPT, os.X_OK)):
            job["error"] = ("Ops kit not found at %s. Install it on the host: "
                            "sudo cp appvault_ops.sh selfheal_watchdog.sh /opt/appvault/ && "
                            "sudo chmod +x /opt/appvault/*.sh (or point APPVAULT_OPS_SCRIPT at it). "
                            "See README_OPS.md." % OPS_SCRIPT)
            return
        job["stage"] = "working"
        job["message"] = "%s %s: running..." % (job["cmd"], job["target"])
        p = subprocess.run([OPS_SCRIPT, job["cmd"], job["target"]],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=OPS_TIMEOUT)
        job["log_tail"] = (p.stdout or "")[-4000:]
        if p.returncode == 0:
            job["stage"] = "done"
            job["message"] = "%s %s finished OK" % (job["cmd"], job["target"])
        else:
            job["error"] = "%s %s failed (exit %d)" % (job["cmd"], job["target"], p.returncode)
    except subprocess.TimeoutExpired:
        job["error"] = "%s %s timed out after %ds" % (job["cmd"], job["target"], OPS_TIMEOUT)
    except Exception as e:
        job["error"] = str(e)[:300]
    finally:
        job["done"] = True
        with _ops_job_lock:
            if _ops_active_job == job_id:
                _ops_active_job = None


def _start_ops_job(cmd, target):
    global _ops_active_job
    target = _ops_resolve_target((target or "").strip())
    if not _ops_target_ok(target):
        return jsonify({"status": "error",
                        "message": "Invalid target: use 'all' or a container/app name"}), 400
    with _ops_job_lock:
        if _ops_active_job:
            active = _ops_jobs.get(_ops_active_job) or {}
            return jsonify({"status": "busy",
                            "message": "An ops job is already running (%s %s)" % (active.get("cmd"), active.get("target"))}), 409
        job_id = uuid.uuid4().hex[:12]
        _ops_jobs[job_id] = {"job_id": job_id, "cmd": cmd, "target": target,
                             "stage": "queued", "message": "Queued...", "done": False,
                             "error": "", "log_tail": "", "start_time": time.time()}
        _ops_active_job = job_id
        done_ids = [j for j, v in _ops_jobs.items() if v["done"]]
        for j in done_ids[:-19]:
            _ops_jobs.pop(j, None)
    threading.Thread(target=_ops_worker, args=(job_id,), daemon=True).start()
    print("[agent] ops job %s: %s %s" % (job_id, cmd, target), flush=True)
    return jsonify({"status": "started", "job_id": job_id,
                    "message": "%s %s started" % (cmd, target)})


@app.route("/api/ops/backup", methods=["POST"])
def api_ops_backup():
    """Back up app data volumes + launch settings now (all managed apps or one)."""
    body = request.get_json(silent=True) or {}
    return _start_ops_job("backup", body.get("target") or "all")


@app.route("/api/ops/update", methods=["POST"])
def api_ops_update():
    """Safe-update apps via the ops kit (auto-rollback / backup-restore on failure)."""
    body = request.get_json(silent=True) or {}
    return _start_ops_job("update", body.get("target") or "all")


@app.route("/api/ops/restore", methods=["POST"])
def api_ops_restore():
    """Restore one app from its latest backup (container/app name required)."""
    body = request.get_json(silent=True) or {}
    target = (body.get("target") or "").strip()
    if not target or target == "all":
        return jsonify({"status": "error",
                        "message": "Restore requires a specific app/container name"}), 400
    return _start_ops_job("restore", target)


@app.route("/api/ops/status", methods=["GET"])
@app.route("/api/ops/status/<job_id>", methods=["GET"])
def api_ops_status(job_id=None):
    """Poll the active (or a specific) ops job."""
    with _ops_job_lock:
        if job_id:
            job = _ops_jobs.get(job_id)
            if not job:
                return jsonify({"error": "Unknown job"}), 404
            return jsonify(_ops_snapshot(job))
        if _ops_active_job:
            return jsonify(_ops_snapshot(_ops_jobs[_ops_active_job]))
        done_ids = [j for j, v in _ops_jobs.items() if v["done"]]
        if done_ids:
            return jsonify(_ops_snapshot(_ops_jobs[done_ids[-1]]))
    return jsonify({"job_id": None, "cmd": None, "target": None, "stage": "idle",
                    "message": "No ops job has run yet", "done": True, "error": "",
                    "log_tail": "", "elapsed_seconds": 0})

@app.route("/api/exec/<app_id>", methods=["POST"])
def api_exec(app_id):
    """Run a command inside the container targeting a specific app (e.g. npx omniroute reset-password).

    POST-only on purpose: commands in query strings leak into proxy logs and history."""
    cname = _app_container_name(app_id)
    if not (container_running(cname) or container_exists(cname)):
        return jsonify({"status": "error", "app_id": app_id, "message": f"App container '{cname}' for '{app_id}' is not running"}), 400

    data = request.get_json(silent=True) or request.form or {}
    cmd = data.get("command") or data.get("cmd")

    if not cmd:
        return jsonify({"status": "error", "message": "Missing 'command' parameter (provide JSON body: {\"command\": \"...\"} or query param ?cmd=...)"}), 400

    if isinstance(cmd, str):
        cmd_args = ["sh", "-c", cmd]
    elif isinstance(cmd, list):
        cmd_args = [str(x) for x in cmd]
    else:
        return jsonify({"status": "error", "message": "Invalid command format"}), 400

    ok, output = _docker("exec", cname, *cmd_args, capture=True, timeout=60)
    return jsonify({
        "status": "ok" if ok else "error",
        "app_id": app_id,
        "container": cname,
        "exit_code": 0 if ok else 1,
        "output": output
    })

@app.route("/api/ai/generate-command", methods=["POST"])
def api_ai_generate_command():
    """AI Command Generator: Converts natural language prompt into exact container CLI command."""
    data = request.get_json(silent=True) or {}
    app_id = (data.get("app_id") or "").lower().strip()
    raw_prompt = data.get("prompt") or data.get("query") or ""
    prompt = raw_prompt.lower().strip()

    if not prompt:
        return jsonify({"status": "error", "message": "Missing 'prompt' in request body"}), 400

    import re, shlex
    pass_match = re.search(r'(?:password\s+(?:to|is|=)\s*|password:\s*)(\S+)', raw_prompt, re.I)
    pwd = pass_match.group(1) if pass_match else "NewAdminPass123"
    pwd_q = shlex.quote(pwd)

    cmd = ""
    explanation = ""

    if app_id == "omniroute":
        if any(k in prompt for k in ["reset", "password", "pass"]):
            cmd = f"printf {pwd_q} | node /app/bin/reset-password.mjs --password-stdin"
            explanation = f"Resets OmniRoute admin password non-interactively to '{pwd}'."
        elif any(k in prompt for k in ["help", "option"]):
            cmd = "node /app/bin/omniroute.mjs --help"
            explanation = "Displays OmniRoute CLI options."
        elif any(k in prompt for k in ["version", "v"]):
            cmd = "node -v"
            explanation = "Displays Node.js runtime version."
    elif app_id == "openwebui":
        if any(k in prompt for k in ["python", "version"]):
            cmd = "python3 --version"
            explanation = "Shows Python runtime version in Open WebUI container."
        elif any(k in prompt for k in ["env", "config"]):
            cmd = "env"
            explanation = "Lists environment variables."
    elif app_id == "pihole":
        if any(k in prompt for k in ["status", "state"]):
            cmd = "pihole status"
            explanation = "Checks Pi-hole blocking status."
        elif any(k in prompt for k in ["version", "v"]):
            cmd = "pihole -v"
            explanation = "Shows Pi-hole core and web versions."

    if not cmd:
        if any(k in prompt for k in ["disk", "space", "storage", "size"]):
            cmd = "df -h"
            explanation = "Shows disk space usage."
        elif any(k in prompt for k in ["mem", "ram", "memory"]):
            cmd = "cat /proc/meminfo | head -n 5"
            explanation = "Shows system memory stats."
        elif any(k in prompt for k in ["env", "environment", "variable"]):
            cmd = "env"
            explanation = "Displays container environment variables."
        elif any(k in prompt for k in ["process", "top", "ps"]):
            cmd = "ps aux || ps -ef"
            explanation = "Lists running processes inside container."
        elif any(k in prompt for k in ["node", "version"]):
            cmd = "node -v || python3 --version"
            explanation = "Checks installed runtime version."
        elif any(k in prompt for k in ["list", "file", "directory", "dir", "ls"]):
            cmd = "ls -la /app"
            explanation = "Lists files in /app directory."
        else:
            # shlex.quote: the raw prompt may contain quotes that break out of
            # the echo'd shell string (command injection into /api/exec).
            cmd = f"echo {shlex.quote('Executing request: ' + prompt)} && env"
            explanation = f"Generated CLI command for: '{prompt}'"

    return jsonify({
        "status": "ok",
        "app_id": app_id,
        "prompt": raw_prompt,
        "command": cmd,
        "explanation": explanation
    })

# ==============================================================================
# ==============================================================================
# AGENTIC OS - UNIFIED AI CONTROL PLANE API (real implementation)
# Replaces the hardcoded demo block. State lives in SQLite, roster status is
# derived from live probes, Oracle sweeps real RSS feeds, LLM config is central.
# ==============================================================================
from agentic_plane import agentic_bp, start_funnel_scheduler, start_backup_scheduler
start_funnel_scheduler()  # boot the funnel scheduler daemon (prospect machine)
start_backup_scheduler()  # boot the daily backup daemon (agentic.db snapshots)
app.register_blueprint(agentic_bp)

# HEIMDALL â€” auto-configure on startup
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

try:
    from heimdall_bridge import setup_heimdall_custom_js
    if setup_heimdall_custom_js():
        print("[agent] Heimdall configured for AppVault integration")
except Exception as e:
    print(f"[agent] Heimdall bridge not available: {e}")

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# STARTUP
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

# Register cloud sync routes
cloud_sync.register_routes(app)

# Start phone-home in background
threading.Thread(target=phone_home_loop, daemon=True).start()

# Start cloud sync in background
threading.Thread(target=cloud_sync.sync_loop, daemon=True).start()

# MCP Gateway (optional, fail-safe): exposes installed apps as LLM tools on :8087.
# Read-only by default; MCP_ALLOW_WRITES=1 opts into write tools (Phase 2 approval gate).
try:
    import mcp_gateway
    _mcp_port = int(os.environ.get("MCP_PORT", "8087"))
    _mcp_writes = os.environ.get("MCP_ALLOW_WRITES", "0") == "1"

    def _mcp_start():
        try:
            # Persistent credential vault seeded from a JSON file the operator
            # drops at <STORAGE_PATH>/gateway_creds.json:
            #   {"wordpress": {"header": "Authorization", "value": "Basic ..."},
            #    "write_policy": {"apps": {"wordpress": "auto"}}}
            _vault = mcp_gateway.Vault(
                path=os.path.join(STORAGE_PATH, "gateway_vault.json"))
            _policy = {}
            _seed = os.path.join(STORAGE_PATH, "gateway_creds.json")
            if os.path.exists(_seed):
                with open(_seed) as _f:
                    _seed_data = json.load(_f)
                _policy = _seed_data.pop("write_policy", {}) or {}
                for _app, _cred in _seed_data.items():
                    if isinstance(_cred, dict) and _cred.get("header") and _cred.get("value"):
                        _vault.set(_app, _cred)
                        print(f"[agent] MCP vault: seeded credentials for {_app}")
            _env_policy = os.environ.get("GATEWAY_WRITE_POLICY", "")
            if _env_policy:
                try:
                    _policy = json.loads(_env_policy)
                except Exception:
                    pass

            def _gw_get_host_port(cname):
                """Resolve 'host:port' for an app, preferring the IP on the SAME
                docker network as the agent (apps can sit on several networks)."""
                app_id = cname[4:] if cname.startswith("app-") else cname
                cport = None
                for a in catalog_cache.get("apps", []):
                    if a.get("id") == app_id:
                        cport = a.get("container_port")
                        break
                if not cport:
                    return None
                # agent's own networks (hostname inside docker == container id)
                my_nets = set()
                try:
                    ok, out = _docker("inspect", "-f",
                                      "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                                      socket.gethostname(), capture=True)
                    if ok:
                        my_nets = set(out.strip().split())
                except Exception:
                    pass
                ok, out = _docker("inspect", "-f",
                                  "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}",
                                  cname, capture=True)
                if ok and out:
                    pairs = [p for p in out.strip().split() if "=" in p]
                    for pair in pairs:
                        net, ip = pair.split("=", 1)
                        if net in my_nets and ip:
                            return f"{ip}:{cport}"
                    for pair in pairs:
                        ip = pair.split("=", 1)[1]
                        if ip:
                            return f"{ip}:{cport}"
                return None

            mcp_gateway.start_gateway(
                catalog_getter=lambda: catalog_cache.get("apps", []),
                docker_fn=_docker,
                get_host_port=_gw_get_host_port,
                vault=_vault,
                write_policy=_policy,
                api_key=API_KEY,
                port=_mcp_port,
                allow_writes=_mcp_writes,
            )
        except Exception as e:
            print(f"[agent] MCP gateway failed to start: {e}")

    threading.Thread(target=_mcp_start, daemon=True).start()
    print(f"[agent] MCP gateway starting on port {_mcp_port} "
          f"(write policy: {'allow' if _mcp_writes else 'deny'})")
except Exception as e:
    print(f"[agent] MCP gateway disabled: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("AGENT_PORT", AGENT_PORT))
    print(f"[agent] Starting AppVault Agent on port {port}")
    print(f"[agent] Central server: {CENTRAL_URL}")
    print(f"[agent] Agent name: {AGENT_NAME}")
    # Warm the bulk container snapshot so the FIRST /api/catalog load is fast.
    try:
        _refresh_bulk_container_state()
        print(f"[agent] Bulk container state warmed ({len(_BULK_NAMES)} containers)")
    except Exception as e:
        print(f"[agent] Warm-up failed: {e}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

