#!/usr/bin/env python3
"""
Apply certification results to the catalog.

Reads cert-report.json (produced by certify_app.py) and updates
central/static/catalog.json:
  - certified apps   -> entry["certified"] = {status ok, legs, date, url}
                        web_path corrected to the certified URL's path
  - failed apps      -> entry["certified"] = {status "beta"} (or left unset)
  - web_path fixes   -> derived from verified_url when the probe found the
                        real path (e.g. jellyfin '/' -> '/web/')

Usage: python apply_cert.py cert-report.json [--catalog central/static/catalog.json] [--leg windows]
"""
import json, os, sys, datetime

REPORT = sys.argv[1] if len(sys.argv) > 1 else "cert-report.json"
CATALOG = os.environ.get("CERT_CATALOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "central", "static", "catalog.json"))
LEG = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CERT_LEG", "windows")
TODAY = datetime.date.today().isoformat()


def main():
    report = json.load(open(REPORT, encoding="utf-8"))
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    apps = {a["id"]: a for a in catalog["apps"]}
    fixed, certified, failed = [], 0, 0
    for app_id, r in report.items():
        if app_id not in apps:
            print(f"skip {app_id}: not in catalog")
            continue
        a = apps[app_id]
        if r.get("certified") and r.get("verified_url"):
            # derive web_path from the certified URL
            from urllib.parse import urlparse
            wp = urlparse(r["verified_url"]).path or "/"
            old_wp = a.get("web_path") or "/"
            if wp != old_wp:
                a["web_path"] = wp
                fixed.append(f"{app_id}: web_path {old_wp!r} -> {wp!r}")
            cert = a.get("certified") or {}
            legs = list(cert.get("legs", []))
            if LEG not in legs:
                legs.append(LEG)
            a["certified"] = {"status": "ok", "legs": legs, "date": TODAY, "url": r["verified_url"]}
            certified += 1
            print(f"✅ {app_id}: certified ({','.join(legs)}) url={r['verified_url']}")
        else:
            a.pop("certified", None)
            failed += 1
            notes = "; ".join(r.get("notes", []))[:160]
            print(f"⚠️  {app_id}: NOT certified — {notes}")
    json.dump(catalog, open(CATALOG, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"\ncertified: {certified} | failed/unsure: {failed} | web_path fixes: {len(fixed)}")
    for f in fixed:
        print("  fix:", f)


if __name__ == "__main__":
    main()
