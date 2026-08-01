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
PUBLIC_READ_PREFIXES = ("/api/catalog", "/api/health", "/api/info", "/api/agent/status",
                        "/api/apps/health", "/api/education/", "/api/icon/", "/api/ping/", "/api/security")

@app.before_request
def require_api_key():
    if request.method == "OPTIONS":
        return None
    if not API_KEY:
        return None
    path = request.path
    if path.startswith("/api/"):
        # Self-service license + security apply are public (user's own agent/local install)
        if path.startswith("/api/license") or path.startswith("/api/security"):
            return None
        is_public_read = request.method == "GET" and path.startswith(PUBLIC_READ_PREFIXES)
        requires_key = (not is_public_read) or path.startswith("/api/install/")
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
        result = subprocess.run(
            [DOCKER_CMD] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")}
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()[:200]
            return False, err
        if capture:
            return True, result.stdout.strip()
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
    
    try:
        with urllib.request.urlopen(req, data=body, timeout=10) as resp:
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

def get_container_host_port(container_name):
    """Get the first host port mapped to a container."""
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
    """Get the host port mapped to a specific container port (e.g. setup wizard port)."""
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

def _provision_database(app_id, app_def):
    """Auto-start central DB if needed and create the app's database."""
    env_vars = {e.split("=")[0]: e.split("=", 1)[1] for e in app_def.get("env", []) if "=" in e}
    
    # Check which central DB is needed
    central_db = None
    db_name = None
    db_user = None
    db_pass = None
    
    for key, val in env_vars.items():
        if val == "app-central-mariadb" or val == "app-central-mariadb:3306":
            central_db = "central-mariadb"
        elif val == "app-central-postgres":
            central_db = "central-postgres"
    
    # Extract DB credentials from env vars
    for key, val in env_vars.items():
        if "DB_NAME" in key.upper() or "DB_DATABASE" in key.upper() or "MYSQL_DATABASE" in key.upper() or "DATABASE_NAME" in key.upper():
            db_name = val
        if "DB_USER" in key.upper() or "MYSQL_USER" in key.upper() or "DATABASE_USER" in key.upper():
            db_user = val
        if "DB_PASS" in key.upper() or "MYSQL_PASSWORD" in key.upper() or "DB_PASSWORD" in key.upper() or "DATABASE_PASSWORD" in key.upper():
            db_pass = val
    
    if not central_db:
        return  # No central DB needed
    
    cname = f"app-{central_db}"
    
    # Start central DB if not running
    if not container_running(cname):
        # Find central DB in catalog
        db_def = None
        for a in catalog_cache.get("apps", []):
            if a["id"] == central_db:
                db_def = a
                break
        if db_def:
            print(f"[agent] Starting central DB: {central_db}")
            # Build minimal run args for central DB
            image = db_def.get("image", "mariadb:10.11")
            net_name = os.environ.get("APPVAULT_NETWORK", "webdev_appvault-net")
            run_args = [
                "run", "-d",
                "--name", cname,
                "--network", net_name,
                "--restart", "unless-stopped",
                "-p", "3306" if central_db == "central-mariadb" else "5432",
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
                time.sleep(5)  # Give it a moment
            else:
                print(f"[agent] Failed to start central DB: {err}")
                return
    
    # Create database and user if needed
    if central_db == "central-mariadb":
        _create_mariadb_db(cname, db_name, db_user, db_pass)
    elif central_db == "central-postgres":
        _create_postgres_db(cname, db_name, db_user, db_pass)

def _create_mariadb_db(cname, db_name, db_user, db_pass):
    """Create a database and user in MariaDB."""
    if not db_name:
        return
    root_pass = "appvault_root_secret"
    # Check if DB already exists
    ok, out = _docker("exec", cname, "mysql", "-uroot", f"-p{root_pass}", "-e", 
                      f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='{db_name}'", capture=True, timeout=10)
    if ok and db_name in out:
        print(f"[agent] DB '{db_name}' already exists")
        return
    # Create DB
    _docker("exec", cname, "mysql", "-uroot", f"-p{root_pass}", "-e", 
            f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", timeout=10)
    if db_user and db_pass:
        _docker("exec", cname, "mysql", "-uroot", f"-p{root_pass}", "-e",
                f"CREATE USER IF NOT EXISTS '{db_user}'@'%' IDENTIFIED BY '{db_pass}'", timeout=10)
        _docker("exec", cname, "mysql", "-uroot", f"-p{root_pass}", "-e",
                f"GRANT ALL PRIVILEGES ON {db_name}.* TO '{db_user}'@'%'", timeout=10)
        _docker("exec", cname, "mysql", "-uroot", f"-p{root_pass}", "-e", "FLUSH PRIVILEGES", timeout=10)
    print(f"[agent] MariaDB: created DB '{db_name}', user '{db_user}'")

def _create_postgres_db(cname, db_name, db_user, db_pass):
    """Create a database and user in PostgreSQL."""
    if not db_name:
        return
    root_pass = "appvault_root_secret"
    # Check if DB exists
    ok, out = _docker("exec", cname, "psql", "-U", "postgres", "-c", 
                      f"SELECT 1 FROM pg_database WHERE datname='{db_name}'", capture=True, timeout=10)
    if ok and "(1 row)" in out:
        print(f"[agent] DB '{db_name}' already exists")
        return
    # Create user
    if db_user and db_pass:
        _docker("exec", cname, "psql", "-U", "postgres", "-c",
                f"CREATE USER {db_user} WITH PASSWORD '{db_pass}'", timeout=10)
    # Create DB
    _docker("exec", cname, "psql", "-U", "postgres", "-c",
            f"CREATE DATABASE {db_name} OWNER {db_user or 'postgres'}", timeout=10)
    if db_user:
        _docker("exec", cname, "psql", "-U", "postgres", "-c",
                f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user}", timeout=10)
    print(f"[agent] PostgreSQL: created DB '{db_name}', user '{db_user}'")

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
    net_name = os.environ.get("APPVAULT_NETWORK", "webdev_appvault-net")
    run_args = [
        "run", "-d",
        "--name", container_name,
        "--network", net_name,
        "--restart", "unless-stopped",
        "--label", f"appvault.app={app_id}",
        "--label", "appvault.managed=true",
    ]
    
    # Port mappings
    container_port = app_def.get("container_port")
    if container_port:
        run_args.extend(["-p", str(container_port)])  # random host port
    
    extra_ports = app_def.get("extra_ports", {})
    # extra_ports format: "container_port": "${ENV_VAR:-host_port}"
    for container_port_str, host_port_str in extra_ports.items():
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
            print(f"[agent] Data dir: {dir_path}")
            # Use the host path for Docker bind mount
            run_args.extend(["-v", f"{host_path}:{container_path}"])
        else:
            run_args.extend(["-v", vol])
    
    # Environment variables
    for e in app_def.get("env", []):
        if "=" in e:
            key, val = e.split("=", 1)
            expanded = os.path.expandvars(val)
            # Only add if not referencing an unset variable
            if not expanded.startswith("${") or ":-" in expanded:
                run_args.extend(["-e", f"{key}={expanded}"])
    
    # Add image
    run_args.append(image)
    
    # Provision database in central DB if needed
    _set_progress(app_id, "Configuring database...", 70)
    _provision_database(app_id, app_def)
    
    # Run container
    _set_progress(app_id, "Starting container...", 80)
    print(f"[agent] Starting container: {container_name}")
    ok, err = _docker(*run_args, capture=True)
    if not ok:
        _set_progress_error(app_id, f"Failed to start: {err[:150]}")
        print(f"[agent] Docker run failed: {err}")
        raise Exception(f"Failed to start container: {err}")
    
    _set_progress(app_id, "Finalizing...", 90)
    
    # Add Heimdall tile
    try:
        from heimdall_bridge import add_heimdall_tile
        container_port = app_def.get("container_port", "")
        host_port = get_container_host_port(container_name)
        tile_url = f"{public_base()}:{host_port}" if host_port else f"{public_base()}:{container_port}"
        add_heimdall_tile(app_def.get("name", app_id), tile_url, app_id, app_def.get("description", ""))
    except Exception as e:
        print(f"[agent] Heimdall tile not added: {e}")
    
    _set_progress_done(app_id, f"{app_def.get('name', app_id)} installed!")
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
    if "raw.githubusercontent.com" in compose_url:
        # Extract GitHub repo URL: https://raw.githubusercontent.com/user/repo/branch/file
        parts = compose_url.replace("https://raw.githubusercontent.com/", "").split("/")
        if len(parts) >= 3:
            user, repo, branch = parts[0], parts[1], parts[2]
            repo_url = f"https://github.com/{user}/{repo}.git"
            repo_dir = os.path.join(stack_dir, "repo")
    
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
        else:
            print(f"[agent] Git clone failed: {r.stderr[:200]}")
        
        # Use compose from repo root
        compose_path = os.path.join(repo_dir, "docker-compose.yml")
        if not os.path.exists(compose_path):
            compose_path = os.path.join(repo_dir, "docker-compose.yaml")
        print(f"[agent] Using compose file: {compose_path}")
    
    _set_progress(app_id, "Pulling images...", 40)
    ok, pull_out = _docker("compose", "-f", compose_path, "pull", capture=True, timeout=600)
    if not ok:
        print(f"[agent] Pull warning: {pull_out[:200]}")
    
    _set_progress(app_id, "Building services...", 55)
    ok, build_out = _docker("compose", "-f", compose_path, "build", capture=True, timeout=600)
    if ok:
        print(f"[agent] Build complete")
    else:
        print(f"[agent] Build output: {build_out[:200]}")
    
    _set_progress(app_id, "Starting services...", 70)
    
    ok, services_out = _docker("compose", "-f", compose_path, "config", "--services", capture=True, timeout=30)
    services = services_out.strip().split('\n') if ok else []
    
    for i, svc in enumerate(services):
        pct = 70 + int((i / max(len(services), 1)) * 25)
        _set_progress(app_id, f"Starting {svc}...", pct)
        ok, err = _docker("compose", "-f", compose_path, "up", "-d", svc, capture=True, timeout=300)
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
                        # Retry
                        ok, err = _docker("compose", "-f", compose_path, "up", "-d", svc, capture=True, timeout=300)
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
                ok, err = _docker("compose", "-f", compose_path, "up", "-d", svc, capture=True, timeout=300)
                if ok:
                    print(f"[agent] {svc} started with remapped ports")
                else:
                    print(f"[agent] {svc} still failed after remap: {err[:200]}")
            else:
                print(f"[agent] {svc} failed: {err[:200]}")
    
    _set_progress(app_id, "Finalizing...", 95)
    
    try:
        from heimdall_bridge import add_heimdall_tile
        tile_url = f"{public_base()}:{app_def.get('container_port','3000')}"
        add_heimdall_tile(app_name, tile_url, app_id, app_def.get("description", ""))
    except Exception as e:
        print(f"[agent] Tile not added: {e}")
    
    _set_progress_done(app_id, f"{app_name} installed!")
    print(f"[agent] {app_id} stack installed")

def _do_uninstall(app_id):
    """Uninstall a Docker app."""
    if not docker_available():
        raise Exception("Docker unavailable")
    
    container_name = f"app-{app_id}"
    if not container_exists(container_name):
        print(f"[agent] {app_id} not found, skipping")
        return
    
    _docker("stop", container_name)
    _docker("rm", container_name)
    
    # Remove Heimdall tile
    try:
        from heimdall_bridge import remove_heimdall_tile
        tile_url = f"{public_base()}:{get_container_host_port(container_name) or ''}"
        if tile_url:
            remove_heimdall_tile(tile_url)
    except Exception as e:
        pass
    
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
    else:
        raise Exception(f"Failed to restart: {err}")

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
    """Get the container's internal port from Docker port mapping."""
    ok, out = _docker("port", cname, capture=True)
    if ok and out:
        for line in out.strip().split('\n'):
            if '/tcp' in line:
                parts = line.split('->')
                return parts[0].split('/')[0].strip()
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
    """Check if a Docker app is installed locally."""
    cname = f"app-{app_id}"
    if container_running(cname):
        return "installed"
    if container_exists(cname):
        return "stopped"
    return "available"

@app.route("/api/catalog")
def api_catalog():
    """Return the catalog with live local status and host ports."""
    result = []
    for app in catalog_cache.get("apps", []):
        status = get_app_status_local(app["id"])
        host_port = ""
        if status in ("installed", "stopped"):
            cname = f"app-{app['id']}"
            host_port = get_container_host_port(cname) or app.get("container_port", "")
        entry = {**app, "status": status, "host_port": host_port}
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
    })

@app.route("/api/health")
def api_health():
    """Health check."""
    d = docker_info()
    return jsonify({
        "status": "ok",
        "agent_id": agent_state.get("agent_id", ""),
        "docker": "connected" if d["available"] else "disconnected",
        "docker_version": d["version"],
        "central": CENTRAL_URL,
        "central_status": "unknown",
        "catalog_version": catalog_cache.get("version", 0),
        "catalog_apps": len(catalog_cache.get("apps", [])),
        "version": APP_VERSION,
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
    installed = os.path.exists("/usr/bin/tailscale") or os.path.exists("/usr/local/bin/tailscale")
    if not installed:
        return {"installed": False, "running": False, "ip": None}
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            selfip = d.get("Self", {})
            return {"installed": True, "running": d.get("BackendState") in ("Running", "Starting"),
                    "ip": selfip.get("TailscaleIPs", [None])[0], "hostname": selfip.get("HostName", "")}
        return {"installed": True, "running": False}
    except Exception:
        return {"installed": True, "running": False}

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
    result["web_path"] = app_def.get("web_path", "/")
    
    # Build launch URL
    port = result["host_port"]
    path = result["web_path"]
    if port:
        result["launch_url"] = f"{public_base()}:{port}{path}"
    else:
        result["launch_url"] = ""

    # Extra ports (setup/secondary) -> host URLs so clients show the RIGHT link
    extra_urls = {}
    for cport in (app_def.get("extra_ports") or {}):
        hp = get_container_port_host(cname, cport)
        if hp:
            extra_urls[cport] = f"{public_base()}:{hp}{path}"
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

@app.route("/api/install/<app_id>", methods=["POST"])
def api_install(app_id):
    """Start installing an app in the background. Returns immediately."""
    # Reject disabled apps (admin disabled for everyone)
    for a in catalog_cache.get("apps", []):
        if a["id"] == app_id and a.get("disabled"):
            return jsonify({"status": "error", "app_id": app_id,
                            "message": "This app is currently disabled by the admin"}), 400
    # Initialize progress
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

@app.route("/api/uninstall/<app_id>", methods=["POST"])
def api_uninstall(app_id):
    """Uninstall an app locally."""
    try:
        _do_uninstall(app_id)
        return jsonify({"status": "ok", "app_id": app_id, "message": f"{app_id} uninstalled"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/restart/<app_id>", methods=["POST"])
def api_restart(app_id):
    """Restart an app locally."""
    try:
        _do_restart(app_id)
        return jsonify({"status": "ok", "app_id": app_id, "message": f"{app_id} restarted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

if __name__ == "__main__":
    port = int(os.environ.get("AGENT_PORT", AGENT_PORT))
    print(f"[agent] Starting AppVault Agent on port {port}")
    print(f"[agent] Central server: {CENTRAL_URL}")
    print(f"[agent] Agent name: {AGENT_NAME}")
    app.run(host="0.0.0.0", port=port, debug=False)

