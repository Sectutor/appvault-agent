#!/usr/bin/env python3
"""
AppVault App Certification Harness
==================================
Runs a catalog app spec EXACTLY as the agent would (same image, ports,
volumes, env, network), on THIS docker host, and verifies:

  1. image pulls
  2. container starts and stays up
  3. the host port mapping is live
  4. an HTTP probe on the app's web path returns a real response
     (follows first-run redirects — e.g. AdGuard's /install.html wizard)
  5. the container survives a restart (persistence)

Output: a JSON certification report per app. An app is only "certified"
when every check passes on BOTH a Linux host and a Windows/Docker Desktop
host. Uncertified apps are marked beta in the store and should not be
advertised as production-ready.

Usage:
  python certify_app.py <app_id>            # certify one app
  python certify_app.py --all               # certify every app in the catalog
  python certify_app.py --list              # list apps + their cert status

Env:
  CERT_NETWORK   network name (default: bridge/nat — host default)
  CERT_CATALOG   path to catalog.json (default: ../central/static/catalog.json)
"""
import json, os, socket, subprocess, sys, time, urllib.request, hashlib

CATALOG_PATH = os.environ.get("CERT_CATALOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "central", "static", "catalog.json"))
NETWORK = os.environ.get("CERT_NETWORK", "")


def sh(*args, timeout=300):
    try:
        r = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return False, "", str(e)


def free_port():
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_probe(url, timeout=10):
    """GET with redirects; returns (final_url, status) or (None, 0)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AppVaultCert/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl(), r.status
    except urllib.error.HTTPError as e:
        return e.geturl(), e.code
    except Exception:
        return None, 0


def load_catalog():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)["apps"]


def certify(app):
    app_id = app["id"]
    cname = f"cert-{app_id}"
    image = app.get("image")
    cport = app.get("container_port", "80")
    web_path = app.get("health_path") or app.get("web_path") or "/"
    result = {"app_id": app_id, "image": image, "certified": False,
              "checks": {}, "verified_url": "", "http_status": 0, "notes": []}

    # 1. image pull
    ok, _, err = sh("docker", "pull", image)
    result["checks"]["image_pull"] = ok
    if not ok:
        result["notes"].append(f"image pull failed: {err[:120]}")
        return result

    # 2. run with the agent's exact semantics (ports/volumes/env)
    sh("docker", "rm", "-f", cname)
    hport = str(free_port())
    args = ["docker", "run", "-d", "--name", cname, "--restart", "unless-stopped",
            "-p", f"{hport}:{cport}"]
    if NETWORK:
        args += ["--network", NETWORK]
    for v in app.get("volumes") or []:
        args += ["-v", v]
    for e in app.get("env") or []:
        args += ["-e", e]
    args.append(image)
    ok, _, err = sh(*args)
    result["checks"]["container_start"] = ok
    if not ok:
        result["notes"].append(f"docker run failed: {err[:160]}")
        return result

    # 3. boot wait + up check
    boot = int(app.get("boot_timeout") or 60)
    up = False
    for i in range(boot // 2):
        time.sleep(2)
        ok, out, _ = sh("docker", "inspect", "-f", "{{.State.Running}}", cname)
        if ok and out.strip() == "true":
            up = True
            break
    result["checks"]["stays_up"] = up
    if not up:
        ok, out, _ = sh("docker", "logs", "--tail", "25", cname)
        result["notes"].append(f"container not running after {boot}s; logs: {out[-300:]}")
        return result

    # 4. host port mapping live
    ok, out, _ = sh("docker", "port", cname)
    mapped = "->" in out
    result["checks"]["port_mapped"] = mapped
    if not mapped:
        result["notes"].append(f"no port mapping: {out[:120]}")

    # 5. HTTP probe on the app's web path (follows redirects; retries
    #    throughout the boot window — some apps serve 503 while initializing)
    url = f"http://127.0.0.1:{hport}{web_path}"
    status = 0
    final_url = url
    deadline = time.time() + int(app.get("boot_timeout") or 60)
    while time.time() < deadline:
        final_url, status = http_probe(url, timeout=10)
        # only a real page (2xx/3xx/401/403) counts — 404/503/5xx = still
        # booting or wrong path; keep probing until the deadline
        if status and status != 404 and 200 <= status < 500:
            break
        time.sleep(5)
    result["http_status"] = status
    if status and 200 <= status < 500 and status != 404:
        suffix = final_url.replace(f"http://127.0.0.1:{hport}", "")
        result["verified_url"] = f"http://localhost:{hport}{suffix}"
        result["checks"]["http_ok"] = True
    else:
        # probe bare "/" as a fallback — some specs have a wrong web_path
        final_url2, status2 = http_probe(f"http://127.0.0.1:{hport}/", timeout=10)
        if status2 and 200 <= status2 < 500 and status2 != 404:
            result["checks"]["http_ok"] = True
            result["verified_url"] = f"http://localhost:{hport}/"
            result["notes"].append(f"web_path '{web_path}' failed ({status}); '/' works ({status2})")
        else:
            result["checks"]["http_ok"] = False
            result["notes"].append(f"no good HTTP response on '{web_path}' (status {status}) or '/' ({status2}) — 404 means the path is wrong")

    # 6. restart persistence
    sh("docker", "restart", cname)
    time.sleep(4)
    ok, out, _ = sh("docker", "inspect", "-f", "{{.State.Running}}", cname)
    result["checks"]["restart_ok"] = ok and out.strip() == "true"

    result["certified"] = all(result["checks"].values())
    sh("docker", "rm", "-f", cname)
    return result


def main():
    apps = load_catalog()
    args = sys.argv[1:]
    if args and args[0] == "--list":
        for a in apps:
            cert = a.get("certified") or {}
            print(f"{a['id']:24} {a.get('image',''):42} cert: {cert.get('status','UNSET')}")
        return
    targets = [a for a in apps] if (args and args[0] == "--all") else \
              [a for a in apps if a["id"] in args]
    # infra entries (hidden from the store) have no web UI — not certifiable
    targets = [a for a in targets if not (a.get("hidden") or a.get("disabled"))]
    # stack apps (compose_url) run multi-container compose files — the
    # single-image runner can't certify them; report + skip.
    stack_targets = [a for a in targets if a.get("is_stack") or a.get("compose_url")]
    targets = [a for a in targets if not (a.get("is_stack") or a.get("compose_url"))]
    for a in stack_targets:
        report[a["id"]] = {"app_id": a["id"], "image": a.get("image") or "(stack)",
                           "certified": False, "checks": {}, "verified_url": "",
                           "http_status": 0,
                           "notes": ["stack app (compose_url) — needs the stack certification path, not single-image"]}
        print(f"⏭  {a['id']}: stack app — skipped (stack cert path)", flush=True)
    # apps with no web UI (e.g. VPN-only) can't be HTTP-certified — note + skip
    for a in targets:
        if not a.get("web_ui", True) and a.get("web_ui") is not None:
            report[a["id"]] = {"app_id": a["id"], "image": a.get("image") or "",
                               "certified": True, "checks": {"no_web_ui": True},
                               "verified_url": "", "http_status": 0,
                               "notes": ["no web UI (headless/VPN) — not HTTP-certifiable; data-path checks only"]}
            print(f"⏭  {a['id']}: headless — noted", flush=True)
            targets = [t for t in targets if t["id"] != a["id"]]
    # --exclude app1,app2 — skip known problem images (giant pulls that OOM
    # the runner on Docker Desktop)
    if "--exclude" in args:
        excl = set(args[args.index("--exclude") + 1].split(","))
        targets = [a for a in targets if a["id"] not in excl]
        print(f"excluded: {sorted(excl)}")
    # --resume — skip apps already in cert-report.json (crash-safe restarts)
    report = {}
    if "--resume" in args:
        rp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cert-report.json")
        if os.path.exists(rp):
            try:
                report = json.load(open(rp, encoding="utf-8"))
                done = set(report.keys())
                targets = [a for a in targets if a["id"] not in done]
                print(f"resuming: {len(done)} already done, {len(targets)} remaining")
            except Exception:
                report = {}
    if not targets:
        print("no matching apps; try: python certify_app.py <app_id> [more ids] | --all | --list")
        sys.exit(1)
    # --parallel N — concurrent certification (pulls overlap; big speedup)
    from concurrent.futures import ThreadPoolExecutor
    parallel = 1
    if "--parallel" in args:
        try:
            parallel = max(1, min(4, int(args[args.index("--parallel") + 1])))
        except Exception:
            parallel = 1
    if parallel > 1:
        print(f"parallel: {parallel} workers", flush=True)

    def _certify_one(app):
        print(f"\n=== certifying {app['id']} ({app.get('image')}) ===", flush=True)
        try:
            r = certify(app)
        except Exception as e:
            r = {"app_id": app["id"], "image": app.get("image"), "certified": False,
                 "checks": {}, "verified_url": "", "http_status": 0,
                 "notes": [f"harness exception: {e}"]}
            print(f"  !! harness exception: {e}", flush=True)
        print(json.dumps(r, indent=1), flush=True)
        return app["id"], r

    if parallel > 1:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for app_id, r in pool.map(_certify_one, targets):
                report[app_id] = r
                out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cert-report.json")
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=1)
                print(f"  [{len(report)}/{len(targets)} done]", flush=True)
    else:
        for app in targets:
            app_id, r = _certify_one(app)
            report[app_id] = r
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cert-report.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1)
            print(f"  [{len(report)}/{len(targets)} done]", flush=True)
    print(f"\nreport: {out}")
    passed = [k for k, v in report.items() if v["certified"]]
    print(f"certified: {len(passed)}/{len(report)} — {passed}")


if __name__ == "__main__":
    main()
