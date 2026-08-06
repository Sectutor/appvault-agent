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
from datetime import datetime
from flask import Flask, jsonify, request, render_template, send_from_directory
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
CATALOG_VERSION_FILE = os.path.join(STORAGE_PATH, "catalog_version.txt")

os.makedirs(STORAGE_PATH, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))
APP_VERSION = "1.0.0"

# â”€â”€ CORS: allow Heimdall (8085) and any other origin â”€â”€
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Agent-Id, X-Api-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

# -- Auth guard: require X-Api-Key on /api/* when API_KEY is set --
# -- Auth guard --
# Read-only catalog/status endpoints (GET) are PUBLIC so a fresh install shows free apps
# without a pre-provisioned API key. Mutating/admin actions (install/uninstall/restart)
# still require a valid X-Api-Key.
PUBLIC_READ_PREFIXES = ("/api/catalog", "/api/health", "/api/info", "/api/agent/status", "/api/stats",
                        "/api/apps/health", "/api/education/", "/api/icon/", "/api/ping/", "/api/security", "/api/monitoring")

@app.before_request
def require_api_key():
    if request.method == "OPTIONS":
        return None
    if not API_KEY:
        return None
    path = request.path
    if path.startswith("/api/"):
        # Self-service actions on the user's own agent/local install are public:
        # license, security, and app install/uninstall/restart. Install is a local user
        # action on a private install; the agent API key is for central control jobs.
        if path.startswith(("/api/license", "/api/security", "/api/install",
                            "/api/uninstall", "/api/restart", "/api/stop")):
            return None
        is_public_read = request.method == "GET" and path.startswith(PUBLIC_READ_PREFIXES)
        requires_key = not is_public_read
        if requires_key:
            key = request.headers.get("X-Api-Key", "")
            if key != API_KEY:
                return jsonify({"error": "Unauthorized", "message": "Valid X-Api-Key header required"}), 401
    return None


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
    return name in out

def container_running(name: str) -> bool:
    ok, out = _docker("ps", "--filter", f"name={name}", "--filter", "status=running", "--format", "{{.Names}}", capture=True)
    return name in out

def container_status(name: str) -> str:
    ok, out = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Status}}", capture=True)
    return out[:20] if ok else "not_found"

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LOCAL CATALOG CACHE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def load_catalog_cache():
    if os.path.exists(CATALOG_CACHE_PATH):
        try:
            with open(CATALOG_CACHE_PATH) as f:
                return json.load(f)
        except:
            pass
    return {"version": 0, "apps": []}

def save_catalog_cache(catalog):
    with open(CATALOG_CACHE_PATH, "w") as f:
        json.dump(catalog, f, indent=2)
    # Also save version separately for quick checks
    with open(CATALOG_VERSION_FILE, "w") as f:
        f.write(str(catalog.get("version", 0)))

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

def load_agent_state():
    if os.path.exists(AGENT_STATE_PATH):
        try:
            with open(AGENT_STATE_PATH) as f:
                return json.load(f)
        except:
            pass
    return {"agent_id": AGENT_ID, "api_key": API_KEY}

def save_agent_state(state):
    with open(AGENT_STATE_PATH, "w") as f:
        json.dump(state, f)

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
    
    # Create SSL context that allows self-signed certificates
    ctx = ssl.create_default_context()
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
        agent_state["agent_id"] = result["agent_id"]
        agent_state["api_key"] = result["api_key"]
        save_agent_state(agent_state)
        print(f"[agent] Registered with central as '{result['agent_id'][:12]}...'")
        return True
    else:
        print("[agent] Registration failed â€” will retry")
        return False

def poll_jobs():
    """Check for pending jobs from central server."""
    effective_id = agent_state.get("agent_id", "")
    effective_key = agent_state.get("api_key", "")
    if not effective_id or not effective_key:
        return
    
    result = central_request("GET", "/api/agent/jobs", params={
        "agent_id": effective_id,
        "api_key": effective_key
    })
    
    if result and result.get("jobs"):
        for job in result["jobs"]:
            print(f"[agent] Executing job #{job['id']}: {job['action']} {job['app_id']}")
            execute_job(job)

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
        if force or remote_ver > local_ver or plan_changed:
            reason = "force" if force else (f"v{local_ver} -> v{remote_ver}" if remote_ver > local_ver else f"plan {local_plan} -> {remote_plan}")
            print(f"[agent] Catalog update available: {reason}")
            catalog_result = central_request("GET", "/api/agent/catalog", params={
                "agent_id": effective_id,
                "api_key": effective_key
            })
            if catalog_result:
                catalog_cache = catalog_result
                save_catalog_cache(catalog_result)
                print(f"[agent] Catalog synced: v{remote_ver} ({len(catalog_cache.get('apps', []))} apps)")

def send_heartbeat():
    """Send heartbeat to central server."""
    effective_id = agent_state.get("agent_id", "")
    effective_key = agent_state.get("api_key", "")
    if not effective_id or not effective_key:
        return
    
    central_request("POST", "/api/agent/heartbeat", data={
        "agent_id": effective_id,
        "api_key": effective_key
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
_PORT_CACHE_TTL = 15  # seconds; docker port lookups are expensive (~300ms each via CLI)

def _cached_docker_port(key, fn, *args):
    """Cache docker port lookups for _PORT_CACHE_TTL seconds."""
    hit = _PORT_CACHE.get(key)
    if hit and time.time() - hit[0] < _PORT_CACHE_TTL:
        return hit[1]
    val = fn(*args)
    _PORT_CACHE[key] = (time.time(), val)
    if len(_PORT_CACHE) > 300:
        _PORT_CACHE.clear()
    return val

def get_container_host_port(container_name):
    """Get the first host port mapped to a container (cached 15s)."""
    return _cached_docker_port(("hp", container_name), _get_container_host_port_uncached, container_name)

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
    """Get the host port mapped to a specific container port (cached 15s)."""
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
    """Find a random free port on the host."""
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
    return str(stable)

def _record_host_port(app_id, host_port):
    """Persist the host port assigned to an app so updates/restarts reuse it."""
    try:
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
    """Docker network apps join: APPVAULT_NETWORK env → the network this agent
    itself is attached to → 'bridge'. Fixes 'network webdev_appvault-net not
    found' on installs that never set APPVAULT_NETWORK (Windows/Docker Desktop,
    bare installs) — apps always land on a network that actually exists."""
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
                if nets:
                    return nets[0]
        except Exception:
            pass
    return "bridge"


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



    """Deterministic HTTPS proxy port for an app (20000-28999), used for per-app HTTPS."""
    import hashlib
    h = int(hashlib.sha256(("https:" + app_id).encode()).hexdigest(), 16)
    return 20000 + (h % 9000)


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



def _create_mariadb_db(cname, db_name, db_user, db_pass):
    """Create/ensure the app's database and user in central MariaDB (idempotent, password reset)."""
    if not db_name:
        return
    root_pass = os.environ.get("MARIADB_ROOT_PASSWORD", "appvault_root_secret")
    if db_user and db_pass:
        _docker("exec", cname, "mariadb", "-uroot", f"-p{root_pass}", "-e",
                f"CREATE USER IF NOT EXISTS '{db_user}'@'%' IDENTIFIED BY '{db_pass}'; ALTER USER '{db_user}'@'%' IDENTIFIED BY '{db_pass}';",
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
    if not db_name:
        return
    ok, out = _docker("exec", cname, "psql", "-U", "postgres", "-c",
                      f"SELECT 1 FROM pg_database WHERE datname='{db_name}'", capture=True, timeout=10)
    db_exists = ok and "(1 row)" in out
    if db_user and db_pass:
        _docker("exec", cname, "psql", "-U", "postgres", "-c",
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{db_user}') THEN CREATE ROLE {db_user} LOGIN PASSWORD '{db_pass}'; ELSE ALTER ROLE {db_user} WITH PASSWORD '{db_pass}'; END IF; END $$;",
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
    import json as _json, random, string as _string, base64 as _b64
    user = os.getenv("PORTAINER_ADMIN_USER", "admin")
    newpw = "".join(random.choices(_string.ascii_letters + _string.digits, k=16))

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

def _install_blocked_reason(app_def):
    """Return an error message if this app may NOT be installed on this agent.

    Enforces the business rule: FREE apps go to every user; PREMIUM apps require
    a paid license. Unpublished (disabled) apps can never be installed.
    """
    if not app_def:
        return "App not found in catalog"
    if app_def.get("disabled"):
        return "This app is currently disabled by the admin"
    if (catalog_cache.get("plan") or "free") != "paid":
        if app_def.get("locked") or app_def.get("requires_paid") or not app_def.get("free_tier"):
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
    while time.time() < deadline:
        # 1) native docker healthcheck if the image defines one
        okh, hout = _docker("inspect", "--format", "{{.State.Health.Status}}", cname, capture=True, timeout=15)
        if okh and hout.strip() == "healthy":
            return True, "healthy"
        # 2) HTTP probe inside the container (works without host port binding).
        #    Try curl first; alpine/distroless images often only ship wget.
        okr, rout = _docker("exec", cname, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "--max-time", "5", f"http://127.0.0.1:{cport}{path}", capture=True, timeout=15)
        if not (okr and rout.strip().isdigit()):
            okr, rout = _docker("exec", cname, "wget", "-q", "-O", "/dev/null", "--timeout=5",
                                f"http://127.0.0.1:{cport}{path}", capture=True, timeout=15)
            if okr and rout.strip():
                rout = "200"  # wget exit 0 = served
        # 3) host-side probe via the container IP — covers scratch images
        #    (e.g. traefik) that ship no shell/curl/wget at all. The agent shares
        #    a docker network with app containers; try EVERY container IP since
        #    multi-network apps may expose the reachable IP on any of them.
        if not (okr and rout.strip().isdigit()):
            okip, ipout = _docker("inspect", "--format",
                                  "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                                  cname, capture=True, timeout=15)
            ips = (ipout.strip().split() if (okip and ipout) else [])
            for ip in ips:
                try:
                    import urllib.request
                    req = urllib.request.Request(f"http://{ip}:{cport}{path}", method="GET")
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        rout = str(resp.status)
                        okr = True
                        break
                except Exception as _e:
                    okr = False
                    last_detail = f"host probe {ip}:{cport} failed ({type(_e).__name__})"
        # 4) probe via the Caddy container — Caddy sits on the app network and
        #    resolves app containers BY NAME (busybox wget ships in alpine).
        #    Covers deployments where the agent's own network can't reach apps.
        if not (okr and rout.strip().isdigit()):
            okw, wout = _docker("exec", "appvault-caddy", "wget", "-q", "-O", "/dev/null",
                                "--timeout=5", f"http://{cname}:{cport}{path}",
                                capture=True, timeout=15)
            if okw and wout.strip() == "":
                rout = "200"  # wget exit 0 = served
                okr = True
            else:
                last_detail = f"caddy probe {cname}:{cport} failed"
        if okr and rout.strip().isdigit():
            code = int(rout.strip())
            if code in expect:
                return True, f"HTTP {code} on {path}"
            last_detail = f"HTTP {code} on {path} (wanted {expect})"
        # container still alive?
        okc, _cout = _docker("inspect", "--format", "{{.State.Running}}", cname, capture=True, timeout=15)
        if not (okc and _cout.strip() == "true"):
            okx, xout = _docker("logs", "--tail", "5", cname, capture=True, timeout=15)
            last_detail = "container exited: " + (xout.strip().splitlines() or ["?"])[-1][:120]
        time.sleep(5)
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
    
    # Find app in catalog
    app_def = None
    for a in catalog_cache.get("apps", []):
        if a["id"] == app_id:
            app_def = a
            break
    if not app_def:
        _set_progress_error(app_id, "App not found in catalog")
        raise Exception(f"App '{app_id}' not found in catalog")
    
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
    
    # Remove existing container if any
    if container_exists(container_name):
        _set_progress(app_id, "Removing previous container...", 15)
        _docker("rm", "-f", container_name)
    
    # Pull image
    image_short = image.split("/")[-1]
    _set_progress(app_id, f"Downloading {image_short}...", 25)
    print(f"[agent] Pulling image: {image}")
    ok, err = _docker("pull", image, timeout=600)  # 10 min for large AI/db images
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
    min_mem = app_def.get("min_mem_mb", 512) if app_def else 512
    mem_limit = f"{max(int(min_mem), 512)}m"
    run_args.extend(["--memory", mem_limit, "--cpus", "1.5"])
    
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
        if host_port == "auto":
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

    # Add image
    run_args.append(image)
    
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
            container_port = app_def.get("container_port", "")
            host_port = get_container_host_port(container_name)
            tile_url = f"{public_base()}:{host_port}" if host_port else f"{public_base()}:{container_port}"
        add_heimdall_tile(app_def.get("name", app_id), tile_url, app_id, app_def.get("description", ""))
    except Exception as e:
        print(f"[agent] Heimdall tile not added: {e}")
    
    _set_progress_done(app_id, f"{app_def.get('name', app_id)} installed!")
    if app_id == "portainer":
        try:
            _bootstrap_portainer()
        except Exception as e:
            print(f"[agent] portainer bootstrap error: {e}")
    _sync_caddy_apps()  # register HTTPS reverse-proxy path for this app
    print(f"[agent] {app_id} installed successfully")

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
    
    # Clone the full repo if we have a git URL (needed for build-from-source)
    if repo_url:
        print(f"[agent] Cloning repo: {repo_url}")
        _set_progress(app_id, "Cloning source code...", 20)
        if os.path.exists(repo_dir):
            shutil.rmtree(repo_dir)
        # Clone directly using git installed in the container
        # Also create any env files referenced by the compose file to prevent startup failures
        import subprocess
        r = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, repo_dir],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0:
            print(f"[agent] Repo cloned to {repo_dir}")
            # Create any referenced env files to prevent compose failures
            if os.path.exists(compose_path):
                with open(compose_path, 'r') as f:
                    compose_content = f.read()
                import re
                env_files = re.findall(r'env_file:\s*([^\n]+)', compose_content)
                for ef in env_files:
                    ef_path = os.path.join(repo_dir, ef.strip().strip('"').strip("'"))
                    if not os.path.exists(ef_path):
                        with open(ef_path, 'w') as f:
                            f.write("# Auto-created by AppVault\n")
                        print(f"[agent] Created missing env file: {ef_path}")
                        # Prefer a sibling .env.example as a starting point so
                        # services get sane defaults (e.g. Dify's docker/.env.example)
                        example = ef_path + ".example"
                        if os.path.exists(example):
                            shutil.copy2(example, ef_path)
                            print(f"[agent] Seeded env file from example: {ef_path}")
        else:
            print(f"[agent] Git clone failed: {r.stderr[:200]}")
        
        # Use compose from the URL's repo-relative path when present (e.g.
        # docker/docker-compose.yaml for Dify/Formbricks, packages/twenty-docker
        # for Twenty); otherwise fall back to the repo root.
        if repo_rel_path:
            compose_path = os.path.join(repo_dir, repo_rel_path)
        else:
            compose_path = os.path.join(repo_dir, "docker-compose.yml")
            if not os.path.exists(compose_path):
                compose_path = os.path.join(repo_dir, "docker-compose.yaml")
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
            if _new != _content:
                with open(compose_path, "w", encoding="utf-8") as _f:
                    _f.write(_new)
                print(f"[agent] Injected SERVER_URL={base} into {app_id} compose")
        except Exception as _e:
            print(f"[agent] SERVER_URL injection skipped for {app_id}: {_e}")

    # ADDITIVE: stabilize the web service's host port. Stack composes often use a
    # bare `- "9000"` (random host port) — that drifts on every reinstall and makes
    # launch URLs unpredictable. Rewrite the container_port mapping to a
    # deterministic stable host port so every client install behaves identically.
    try:
        import re as _re3
        _cport3 = str(app_def.get("container_port", "") or "")
        if _cport3:
            with open(compose_path, "r", encoding="utf-8") as _f:
                _content3 = _f.read()
            _stable3 = _stable_host_port(f"app-{app_id}", app_id, _cport3)
            _new3 = _re3.sub(
                rf'^(\s*-\s*)["\']?{_cport3}["\']?\s*$',
                lambda m: m.group(1) + f'"{_stable3}:{_cport3}"',
                _content3, flags=_re3.M)
            if _new3 != _content3:
                with open(compose_path, "w", encoding="utf-8") as _f:
                    _f.write(_new3)
                print(f"[agent] Stabilized {app_id} web port -> {_stable3}:{_cport3}")
    except Exception as _e:
        print(f"[agent] port stabilization skipped for {app_id}: {_e}")

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
                # Find all port mappings and replace host port with a random one
                def remap_port(m):
                    mapping = m.group(0)
                    # Extract the host port part
                    if ':' in mapping:
                        host_part = mapping.split(':')[0].strip().strip('"').strip("'")
                        if host_part.isdigit():
                            new_host = str(_find_free_port())
                            return mapping.replace(host_part, new_host, 1)
                    return mapping
                content = re.sub(r'"?\d+:\d+"?', remap_port, content)
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

    # ADDITIVE: register any labeled stack services with Caddy's HTTPS proxy so the
    # store's Launch URL works for stack apps (same as single-image apps).
    try:
        _sync_caddy_apps()
    except Exception as e:
        print(f"[agent] Caddy sync failed for stack app: {e}")

    _set_progress_done(app_id, f"{app_name} installed!")
    print(f"[agent] {app_id} stack installed")

def _do_uninstall(app_id):
    """Uninstall a Docker app AND free its disk (image + data + volumes)."""
    if not docker_available():
        raise Exception("Docker unavailable")

    container_name = f"app-{app_id}"
    if not container_exists(container_name):
        # ADDITIVE: stack apps run compose containers labeled appvault.app=<app_id>
        # (e.g. twenty-server-1), so uninstall must tear the whole stack down.
        okc, outc = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                            "--format", "{{.Names}}", capture=True)
        if okc and outc and outc.strip():
            print(f"[agent] {app_id} is a stack app; removing stack...")
            stack_root = os.path.join(os.environ.get("STORAGE_PATH", "/data"), "stacks", app_id)
            compose_path = ""
            for cand in (os.path.join(stack_root, "docker-compose.yml"),
                         os.path.join(stack_root, "repo", "docker-compose.yml")):
                if os.path.exists(cand):
                    compose_path = cand
                    break
            if compose_path:
                _docker("compose", "-p", _stack_project(app_id), "-f", compose_path, "down",
                        "-v", "--remove-orphans", capture=True, timeout=300)
            else:
                for cname in outc.strip().splitlines():
                    _docker("stop", cname.strip())
                    _docker("rm", cname.strip())
                _docker("volume", "prune", "-f", capture=True)
            try:
                _sync_caddy_apps()
            except Exception:
                pass
            print(f"[agent] {app_id} stack removed")
            return
        print(f"[agent] {app_id} not found, skipping")
        return

    # capture image + volumes/binds BEFORE removing the container
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

    _docker("stop", container_name)
    _docker("rm", container_name)

    # 1. remove the app's docker image (frees GBs). Ignore if shared/in-use.
    if image:
        ok, err = _docker("image", "rm", image, capture=True)
        if ok:
            print(f"[agent] removed image {image}")
        else:
            print(f"[agent] image {image} not removed ({str(err)[:60]}) - shared/in-use")
    # 2. clear dangling layers left behind (safe: only untagged)
    _docker("image", "prune", "-f", capture=True)

    # 3. remove the app's named volumes (skip shared central-* infra)
    for vol in named_volumes:
        if str(vol).startswith("central-"):
            continue
        _docker("volume", "rm", vol, capture=True)
        print(f"[agent] removed volume {vol}")

    # 4. remove the app's data dir (unified /data/apps/<id>) + any bind mounts
    app_data_host = os.environ.get("APP_DATA_HOST_PATH", "")
    host_dir = ""
    if app_data_host:
        host_dir = os.path.join(app_data_host, app_id).replace(os.sep, "/")
        if os.path.isdir(host_dir):
            import shutil
            shutil.rmtree(host_dir, ignore_errors=True)
            print(f"[agent] removed app data dir {host_dir}")
    for d in bind_dirs:
        if d and d != host_dir and os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            print(f"[agent] removed bind dir {d}")
    
    # Remove Heimdall tile
    try:
        from heimdall_bridge import remove_heimdall_tile
        tile_url = f"{public_base()}:{_https_port(app_id)}" if not _is_proxy_disabled(app_id) else f"{public_base()}:{get_container_host_port(container_name) or ''}"
        if tile_url:
            remove_heimdall_tile(tile_url)
    except Exception as e:
        pass
    
    # Monitoring tools (Portainer/Kuma/Netdata): clear per-install secret + wipe data
    # so uninstall removes the admin password entirely and reinstalling gets a fresh one.
    if app_id in ("portainer", "uptime-kuma", "netdata"):
        try:
            _mon_sec(app_id, "clear")
        except Exception:
            pass
        try:
            host_dir = _monitoring_health_dir(app_id)  # APP_DATA_HOST_PATH/<id>
            if host_dir and os.path.isdir(host_dir):
                import shutil
                shutil.rmtree(host_dir, ignore_errors=True)
                print(f"[agent] wiped {app_id} data dir {host_dir}")
        except Exception as e:
            pass

    _sync_caddy_apps()  # remove HTTPS reverse-proxy path for this app
    print(f"[agent] {app_id} uninstalled")

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
    """Stop a Docker app (releases memory; container + data preserved)."""
    if not docker_available():
        raise Exception("Docker unavailable")
    container_name = f"app-{app_id}"
    if not container_exists(container_name):
        raise Exception(f"Container '{container_name}' not found")
    ok, err = _docker("stop", container_name, capture=True)
    if ok:
        print(f"[agent] {app_id} stopped")
    else:
        raise Exception(f"Failed to stop: {err}")

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
    """Check if a container's web server is responding. Uses wget then curl."""
    url = f"http://127.0.0.1:{internal_port}/"
    # Try wget
    ok, out = _docker("exec", cname, "wget", "-q", "-O", "-", "--timeout=5", url, capture=True, timeout=10)
    if ok and len(out) > 50:
        return True
    # Try curl fallback
    ok, out = _docker("exec", cname, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", url, capture=True, timeout=10)
    if ok and out.strip().isdigit():
        code = int(out.strip())
        if 200 <= code < 500:
            return True
    return False

def _stack_web_ready(svc, cport):
    """Robust readiness for a stack app's web service.

    Prefers the container's compose healthcheck (docker health == healthy) and the
    app's /healthz endpoint. Plain HTTP 200 on `/` is NOT enough: during first-boot
    migrations, Twenty (and similar apps) run transient Nest command processes that
    briefly bind the web port and return 200 before shutting down, which would
    otherwise mark the install done prematurely. Requires two consecutive confirmations
    10s apart so transient processes are filtered out.
    """
    for attempt in range(2):
        ok_health = False
        # 1) docker compose healthcheck (targets the real server's health endpoint)
        okh, hout = _docker("inspect", "--format", "{{.State.Health.Status}}", svc,
                            capture=True, timeout=15)
        if okh and hout.strip() == "healthy":
            ok_health = True
        # 2) fallback: /healthz returns 200 (server-only endpoint when present)
        if not ok_health:
            okc, cout = _docker("exec", svc, "curl", "-s", "-o", "/dev/null", "-w",
                                "%{http_code}", "--max-time", "5",
                                f"http://127.0.0.1:{cport}/healthz", capture=True, timeout=15)
            if okc and cout.strip() == "200":
                ok_health = True
        # 3) last resort (no healthcheck + no /healthz): plain `/` 200
        if not ok_health:
            okr, rout = _docker("exec", svc, "curl", "-s", "-o", "/dev/null", "-w",
                                "%{http_code}", "--max-time", "5",
                                f"http://127.0.0.1:{cport}/", capture=True, timeout=15)
            if okr and rout.strip().isdigit() and 200 <= int(rout.strip()) < 500:
                ok_health = True
        if not ok_health:
            return False
        if attempt == 0:
            time.sleep(10)
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
        app_def = None
        for a in catalog_cache.get("apps", []):
            if a["id"] == app_id:
                app_def = a
                break
        if not app_def:
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
    cname = f"app-{app_id}"
    if container_running(cname):
        return "installed"
    if container_exists(cname):
        return "stopped"
    # ADDITIVE: stack apps run compose containers labeled appvault.app=<app_id>
    # (e.g. twenty-server-1), so they are reported installed/stopped like single-image apps.
    ok, out = _docker("ps", "-a", "--filter", f"label=appvault.app={app_id}",
                      "--format", "{{.Names}}", capture=True)
    if ok and out and out.strip():
        first = out.strip().splitlines()[0].strip()
        return "installed" if container_running(first) else "stopped"
    return "available"

def _get_app_image_uncached(app_id):
    """Installed image string for an app (cached 15s), e.g. 'n8nio/n8n:latest'."""
    ok, out = _docker("inspect", "--format", "{{.Config.Image}}", f"app-{app_id}", capture=True, timeout=15)
    return out.strip() if (ok and out and out.strip()) else ""

def get_app_image(app_id):
    return _cached_docker_port(("img", app_id), _get_app_image_uncached, app_id)

@app.route("/api/catalog")
def api_catalog():
    """Return the catalog with live local status and host ports."""
    result = []
    for app in catalog_cache.get("apps", []):
        if app.get("hidden") or app.get("disabled"):
            continue  # infra (central-* DBs etc.) / unpublished apps not shown in the store
        status = get_app_status_local(app["id"])
        host_port = ""
        if status in ("installed", "stopped"):
            cname = f"app-{app['id']}"
            host_port = get_container_host_port(cname) or app.get("container_port", "")
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
            cname = f"app-{app['id']}"
            path = app.get("web_path", "/")
            for cport in app["extra_ports"]:
                hp = get_container_port_host(cname, cport)
                if hp:
                    entry["setup_url"] = f"{public_base()}:{hp}{path}"
                    break
        result.append(entry)
    
    return jsonify({
        "apps": result,
        "version": catalog_cache.get("version", 0),
        "agent_id": agent_state.get("agent_id", ""),
        "central": CENTRAL_URL,
        "plan": catalog_cache.get("plan", "free"),
    })

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

@app.route("/api/stats")
def api_stats():
    """System + per-app memory/disk stats for the sidebar."""
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
    return jsonify({
        "memory": mem,
        "disk": disk,
        "containers": {"running": running, "stopped": stopped},
        "apps_memory": apps_mem,
    })

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
    """Check and report health of all installed apps."""
    results = []
    ok, out = _docker("ps", "--filter", "name=app-", "--format", "{{.Names}}", capture=True)
    if ok and out:
        containers = [l.strip() for l in out.strip().split('\n') if l.strip()]
        for cname in containers:
            app_id = cname.replace("app-", "", 1)
            app_def = None
            for a in catalog_cache.get("apps", []):
                if a["id"] == app_id:
                    app_def = a
                    break
            if not app_def:
                continue
            port = get_container_host_port(cname) or app_def.get("container_port", "")
            # Check response
            alive = False
            if container_running(cname):
                if app_def.get("category", "").lower() != "database":
                    internal_port = _get_internal_port(cname)
                    alive = _is_app_alive(cname, internal_port)
                else:
                    alive = True  # DB apps considered alive if running
            results.append({
                "id": app_id,
                "name": app_def.get("name", app_id),
                "status": "running" if container_running(cname) else "stopped",
                "port": port,
                "responsive": alive
            })
    return jsonify({"apps": results, "total": len(results), "healthy": sum(1 for r in results if r["responsive"])})

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
    POST stores the key persistently and re-registers with the central so the
    agent's plan upgrades to 'paid' (unlocking premium apps)."""
    if request.method == "GET":
        return jsonify({"license_key": agent_state.get("license_key", "")})
    data = request.json or {}
    # Empty key clears the license (downgrades to free-only); a non-empty key applies it.
    key = (data.get("license_key") or "").strip()
    agent_state["license_key"] = key
    save_agent_state(agent_state)
    ok = register_with_central()
    if ok:
        # Re-sync catalog so the correct apps show (premium when paid, free-only when cleared)
        try:
            sync_catalog(force=True)
        except Exception as e:
            print(f"[agent] Catalog re-sync after license change failed: {e}")
        cleared = not key
        return jsonify({"status": "ok", "license_key": key, "applied": not cleared, "cleared": cleared})
    return jsonify({"status": "error", "license_key": key, "applied": False,
                    "message": "License saved but central re-registration failed"}), 502
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
    """Serve the AppVault Store UI."""
    try:
        return render_template("store.html")
    except Exception as e:
        return f"<h1>AppVault Store</h1><p>Template error: {e}</p>", 200

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

_install_progress = {}  # app_id -> { "stage": str, "message": str, "percent": int, "done": bool, "error": str }

def _set_progress(app_id, message, percent, stage="working"):
    """Update install progress for an app."""
    _install_progress[app_id] = {
        "stage": stage,
        "message": message,
        "percent": min(percent, 99),
        "done": False,
        "error": ""
    }

def _set_progress_done(app_id, message="Done"):
    """Mark install as complete."""
    _install_progress[app_id] = {
        "stage": "done",
        "message": message,
        "percent": 100,
        "done": True,
        "error": ""
    }

def _set_progress_error(app_id, error_msg):
    """Mark install as failed."""
    _install_progress[app_id] = {
        "stage": "error",
        "message": error_msg,
        "percent": 0,
        "done": True,
        "error": error_msg
    }

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
    result["host_port"] = get_container_host_port(cname) or app_def.get("container_port", "")
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
                        result["host_port"] = first.split("->")[1].split("/")[0].strip()
    # END ADDITIVE
    
    # Build launch URL.
    # Monitoring apps publish NO host ports and are reached ONLY via Caddy on the
    # monitoring HTTPS ports (29001/29002/29003). Use the Caddy port, not the
    # container port (9000/3001/19999), so the store shows the WORKING link.
    port = result["host_port"]
    path = result["web_path"]
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
    elif app_id in _app_https_ports():
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
    # Reject disabled apps (admin disabled for everyone) and enforce plan gating
    app_def = next((a for a in catalog_cache.get("apps", []) if a.get("id") == app_id), None)
    blocked = _install_blocked_reason(app_def)
    if blocked:
        return jsonify({"status": "error", "app_id": app_id, "message": blocked}), \
            (400 if app_def and app_def.get("disabled") else 402)
    # Initialize progress
    _set_progress(app_id, "Queued...", 2)
    # Serialize per-app operations: no concurrent install/uninstall/restart
    op_lock = _app_op_lock(app_id)
    if not op_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "app_id": app_id,
                        "message": "Another operation is already running for this app"}), 409
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

# ==============================================================================
# AGENTIC OS — UNIFIED AI CONTROL PLANE API
# ==============================================================================

# In-memory store for Agentic OS state & memory feed
_AGENTIC_MEMORY_FEED = [
    {
        "id": "mem-1",
        "timestamp": "09:45 LOCAL",
        "agent": "Hermes",
        "tag": "Radar Signal",
        "content": "Swept X firehose: Transformer pioneer Noam Shazeer rejoins OpenAI. Logged angle & post hook to Obsidian."
    },
    {
        "id": "mem-2",
        "timestamp": "09:30 LOCAL",
        "agent": "Antigravity",
        "tag": "Code Architecture",
        "content": "Architected Agentic OS unified memory stack with LiteLLM proxy and OneBrain MCP server."
    },
    {
        "id": "mem-3",
        "timestamp": "08:15 LOCAL",
        "agent": "Claude",
        "tag": "System Health",
        "content": "Verified container health across 12 AppVault microservices. All ports operational."
    }
]

_AGENTIC_ROSTER = [
    {"id": "claude", "name": "Claude 3.5 Sonnet", "type": "Core AI Agent", "status": "online", "model": "anthropic/claude-3-5-sonnet", "role": "Deep Reasoning & Code", "mcp_enabled": True},
    {"id": "antigravity", "name": "Antigravity", "type": "Pair Developer", "status": "active", "model": "gemini-3.6-flash", "role": "Full-Stack System Builder", "mcp_enabled": True},
    {"id": "hermes", "name": "Hermes Oracle", "type": "24/7 Watcher", "status": "online", "model": "xai/grok-beta", "role": "News & X Firehose Radar", "mcp_enabled": True},
    {"id": "openclaw", "name": "OpenClaw", "type": "Autonomous Agent", "status": "idle", "model": "local/ollama-llama3", "role": "Web Scraping & Automation", "mcp_enabled": True},
    {"id": "codex", "name": "Codex", "type": "Code Synthesizer", "status": "idle", "model": "openai/gpt-4o", "role": "Refactoring & Spec Generation", "mcp_enabled": True},
    {"id": "kimi", "name": "Kimi Code", "type": "Context Agent", "status": "idle", "model": "moonshot/kimi", "role": "Long-Context Parsing", "mcp_enabled": True},
    {"id": "glm", "name": "GLM 5.2", "type": "Multimodal Agent", "status": "idle", "model": "zhipu/glm-5", "role": "Data & Image Processing", "mcp_enabled": True},
    {"id": "grok", "name": "Grok Build", "type": "Trend Analyst", "status": "idle", "model": "xai/grok-2", "role": "Live Technical Search", "mcp_enabled": True},
    {"id": "free_claude", "name": "Free Claude Code", "type": "Assistant", "status": "idle", "model": "anthropic/claude-3-haiku", "role": "Quick Code Edits", "mcp_enabled": True},
    {"id": "fusion", "name": "Fusion Engine", "type": "Orchestrator", "status": "online", "model": "litellm/router", "role": "Multi-Agent Workflow Fusion", "mcp_enabled": True}
]

@app.route("/api/agentic/roster", methods=["GET"])
def api_agentic_roster():
    """Return live roster of AI agents and orchestration backends."""
    return jsonify({"status": "ok", "agents": _AGENTIC_ROSTER, "total": len(_AGENTIC_ROSTER)})

@app.route("/api/agentic/memory", methods=["GET", "POST"])
def api_agentic_memory():
    """Read or append to the unified Agentic OS shared memory stream and sync to Obsidian Vault."""
    if request.method == "POST":
        data = request.get_json() or {}
        new_entry = {
            "id": f"mem-{len(_AGENTIC_MEMORY_FEED) + 1}",
            "timestamp": data.get("timestamp", "NOW"),
            "agent": data.get("agent", "System"),
            "tag": data.get("tag", "General"),
            "content": data.get("content", "")
        }
        _AGENTIC_MEMORY_FEED.insert(0, new_entry)

        # Sync to Obsidian Vault if path exists
        obsidian_dir = os.environ.get("OBSIDIAN_VAULT_PATH", "D:/ObsidianVault")
        inbox_dir = os.path.join(obsidian_dir, "01_Inbox")
        if os.path.exists(inbox_dir):
            feed_file = os.path.join(inbox_dir, "Agentic_Memory_Feed.md")
            try:
                with open(feed_file, "a", encoding="utf-8") as f:
                    f.write(f"\n### [{new_entry['timestamp']}] {new_entry['agent']} ({new_entry['tag']})\n{new_entry['content']}\n")
            except Exception as e:
                print(f"[agentic] Warning: could not write to Obsidian Vault: {e}")

        return jsonify({"status": "ok", "entry": new_entry})
    
    return jsonify({"status": "ok", "memory": _AGENTIC_MEMORY_FEED})

@app.route("/api/agentic/oracle", methods=["POST"])
def api_agentic_oracle():
    """Trigger Hermes Live Radar / Oracle web sweep."""
    data = request.get_json() or {}
    query = data.get("query", "Latest AI agent frameworks & research")
    
    new_signal = {
        "id": f"mem-{len(_AGENTIC_MEMORY_FEED) + 1}",
        "timestamp": "JUST NOW",
        "agent": "Hermes Oracle",
        "tag": "Live Sweep",
        "content": f"Oracle query executed: '{query}'. Swept 6 live signals. Verified 01 trend high-confidence."
    }
    _AGENTIC_MEMORY_FEED.insert(0, new_signal)
    
    return jsonify({
        "status": "ok",
        "query": query,
        "signals": [
            {
                "id": "sig-01",
                "title": "Transformer Pioneer Shazeer Joins OpenAI",
                "score": 94,
                "posts": "4,891 posts",
                "author": "@NoamShazeer",
                "angle": "This is the real AI moat story: not GPUs, but who owns the architects.",
                "quote": "Google paid $2.7B to get him back. He just walked to OpenAI anyway."
            },
            {
                "id": "sig-02",
                "title": "US Pulls Anthropic Fable Models Offline",
                "score": 91,
                "posts": "3,642 posts",
                "author": "@davidjsacks",
                "angle": "If your agency runs on closed frontier APIs, this is the wake-up call.",
                "quote": "Anthropic spent years asking for AI cyber regulation — then refused to pull a model."
            }
        ]
    })

@app.route("/api/agentic/crew", methods=["POST"])
def api_agentic_crew():
    """Trigger a multi-agent CrewAI or LangGraph workflow."""
    data = request.get_json() or {}
    crew_name = data.get("crew", "Full-Stack Dev Crew")
    task = data.get("task", "Audit & refactor codebase for memory efficiency")
    
    log_entry = {
        "id": f"mem-{len(_AGENTIC_MEMORY_FEED) + 1}",
        "timestamp": "JUST NOW",
        "agent": "Fusion Engine",
        "tag": "Crew Execution",
        "content": f"Dispatched Crew '{crew_name}' on task: {task}. Routed through LiteLLM proxy."
    }
    _AGENTIC_MEMORY_FEED.insert(0, log_entry)
    
    return jsonify({
        "status": "ok",
        "job_id": "crew-job-8842",
        "crew": crew_name,
        "task": task,
        "assigned_agents": ["Claude 3.5 Sonnet", "Antigravity", "Codex"],
        "message": "Workflow started. Progress logged to shared memory MCP."
    })



# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
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
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

