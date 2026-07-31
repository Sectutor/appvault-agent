"""
Cloud storage sync module for AppVault Agent.
Provides rclone-based sync to S3, Google Drive, OneDrive.
"""
import os, json, threading, time, subprocess
from datetime import datetime
from flask import jsonify, request

# Config paths (shared with agent)
STORAGE_PATH = os.environ.get("STORAGE_PATH", "/data")
CLOUD_CONFIG_PATH = os.path.join(STORAGE_PATH, "cloud_config.json")
CLOUD_RCLONE_CONFIG = os.path.join(STORAGE_PATH, "rclone.conf")
CLOUD_SYNC_INTERVAL = int(os.environ.get("CLOUD_SYNC_INTERVAL", "300"))
RCLONE_CMD = "/usr/local/bin/rclone"

def load_config():
    if os.path.exists(CLOUD_CONFIG_PATH):
        with open(CLOUD_CONFIG_PATH) as f:
            return json.load(f)
    return {"enabled": False, "provider": "", "remote_path": "", "last_sync": "", "status": "not_configured"}

def save_config(config):
    os.makedirs(os.path.dirname(CLOUD_CONFIG_PATH), exist_ok=True)
    with open(CLOUD_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

def _rclone(cmd, source, dest, extra=None):
    args = [RCLONE_CMD, cmd, source, dest]
    if extra: args.extend(extra)
    args.extend(["--config", CLOUD_RCLONE_CONFIG])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        s = r.stdout.strip() or r.stderr.strip()[:200]
        return (r.returncode == 0), s
    except Exception as e:
        return False, str(e)

def get_version():
    try:
        r = subprocess.run([RCLONE_CMD, "version"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip().split("\n")[0] if r.returncode == 0 else "not_installed"
    except:
        return "not_installed"

def sync_loop():
    """Background sync thread."""
    time.sleep(10)
    while True:
        try:
            cfg = load_config()
            if cfg.get("enabled") and cfg.get("remote_path"):
                ddir = os.environ.get("APP_DATA_DIR", "/data/apps")
                print(f"[cloud] Syncing {ddir} -> {cfg['remote_path']}")
                ok, out = _rclone("sync", ddir, cfg["remote_path"], ["--checksum"])
                cfg["last_sync"] = datetime.now().isoformat()
                cfg["status"] = "ok" if ok else "error"
                cfg["last_message"] = out[:200] if not ok else f"Synced at {cfg['last_sync'][:19]}"
                save_config(cfg)
        except Exception as e:
            print(f"[cloud] Sync error: {e}")
        time.sleep(CLOUD_SYNC_INTERVAL)

def register_routes(app):
    """Register cloud sync API routes on the Flask app."""
    
    @app.route("/api/cloud/status")
    def cloud_status():
        c = load_config()
        c["rclone_version"] = get_version()
        return jsonify(c)

    @app.route("/api/cloud/configure", methods=["POST"])
    def cloud_configure():
        d = request.json
        if not d: return jsonify({"error": "No data"}), 400
        
        provider = d.get("provider", "")
        remote_path = d.get("remote_path", "")
        if not provider or not remote_path:
            return jsonify({"error": "Provider and path required"}), 400
        
        rclone_cfg = ""
        if provider == "s3":
            ak = d.get("access_key", "")
            sk = d.get("secret_key", "")
            bu = d.get("bucket", "")
            if not ak or not sk or not bu:
                return jsonify({"error": "Keys and bucket required for S3"}), 400
            rclone_cfg = (
                f"[appvault]\n"
                f"type = s3\nprovider = AWS\n"
                f"access_key_id = {ak}\nsecret_access_key = {sk}\n"
                f"region = {d.get('region', 'us-east-1')}\nbucket = {bu}\n"
            )
            remote_path = f"appvault:{remote_path}"
        
        elif provider == "google-drive":
            cid = d.get("client_id", "")
            cs = d.get("client_secret", "")
            if not cid or not cs:
                return jsonify({"error": "Client ID and Secret required"}), 400
            rclone_cfg = (
                f"[appvault]\ntype = drive\n"
                f"client_id = {cid}\nclient_secret = {cs}\n"
                f"scope = drive.file\n"
            )
            remote_path = f"appvault:{remote_path}"
        
        elif provider == "onedrive":
            cid = d.get("client_id", "")
            cs = d.get("client_secret", "")
            if not cid or not cs:
                return jsonify({"error": "Client ID and Secret required"}), 400
            rclone_cfg = (
                f"[appvault]\ntype = onedrive\n"
                f"client_id = {cid}\nclient_secret = {cs}\n"
                f"drive_type = personal\n"
            )
            remote_path = f"appvault:{remote_path}"
        
        else:
            return jsonify({"error": f"Unsupported provider: {provider}"}), 400
        
        with open(CLOUD_RCLONE_CONFIG, "w") as f:
            f.write(rclone_cfg)
        
        cfg = load_config()
        cfg.update({
            "enabled": True, "provider": provider,
            "remote_path": remote_path, "status": "configured"
        })
        save_config(cfg)
        return jsonify({"status": "configured", "provider": provider, "remote_path": remote_path})

    @app.route("/api/cloud/sync", methods=["POST"])
    def cloud_sync_now():
        cfg = load_config()
        if not cfg.get("enabled"):
            return jsonify({"error": "Not configured"}), 400
        
        def _do_sync():
            ddir = os.environ.get("APP_DATA_DIR", "/data/apps")
            ok, out = _rclone("sync", ddir, cfg["remote_path"], ["--checksum"])
            c = load_config()
            c["last_sync"] = datetime.now().isoformat()
            c["status"] = "ok" if ok else "error"
            c["last_message"] = (
                out[:200] if not ok
                else f"Synced at {c['last_sync'][:19]}"
            )
            save_config(c)
        
        threading.Thread(target=_do_sync, daemon=True).start()
        return jsonify({"status": "syncing", "message": "Cloud sync started"})

    @app.route("/api/cloud/disable", methods=["POST"])
    def cloud_disable():
        cfg = load_config()
        cfg["enabled"] = False
        cfg["status"] = "disabled"
        save_config(cfg)
        if os.path.exists(CLOUD_RCLONE_CONFIG):
            os.remove(CLOUD_RCLONE_CONFIG)
        return jsonify({"status": "disabled"})
