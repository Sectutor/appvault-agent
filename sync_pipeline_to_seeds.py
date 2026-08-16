"""Sync pipeline leads -> AppVault funnel seeds (NO CSV round-trip).

Reads the outbound pipeline's lead store (pipeline_leads.csv or any CSV with
company/domain/contact_email columns) and pushes rows straight into the
funnel's prospect seeds via the agent API. Dedupes against existing seeds.

Usage:
    python sync_pipeline_to_seeds.py --dry-run
    python sync_pipeline_to_seeds.py --business 5 --limit 100
    python sync_pipeline_to_seeds.py --source "C:/path/leads.csv" --business 5
"""
import argparse
import csv
import json
import sys
import urllib.request

API = "http://localhost:8086"
DEFAULT_SOURCE = r"C:\Users\emman\.hermes\profiles\cisovault\scripts\vulnflow\pipeline_leads.csv"
BIG_BRANDS = {"microsoft.com", "google.com", "amazon.com", "apple.com", "facebook.com",
              "meta.com", "netflix.com", "cisco.com", "ibm.com", "oracle.com", "salesforce.com"}


def api_post(path, payload):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def existing_websites(business):
    try:
        req = urllib.request.Request(API + f"/api/agentic/funnel/seeds?business_id={business}&limit=500")
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return {s.get("website") or "" for s in d.get("seeds", [])}
    except Exception:
        return set()


def load_leads(path, limit, min_score):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dom = (r.get("domain") or r.get("root_domain") or "").strip().lower()
            company = (r.get("company") or "").strip()
            email = (r.get("contact_email") or r.get("preferred_email") or "").strip()
            if not dom or dom in BIG_BRANDS or dom.startswith(("test.", "example.")):
                continue
            try:
                score = float(r.get("lead_score") or 0)
            except Exception:
                score = 0
            if score < min_score:
                continue
            if not company:
                company = dom.split(".")[0].capitalize()
            note = f"email: {email}" if email else ""
            sig = (r.get("vuln_signals") or "").strip()
            if sig:
                note = (note + " | signals: " + sig[:120]).strip(" |")
            rows.append({"company": company[:200], "website": "https://" + dom, "note": note[:500]})
            if len(rows) >= limit:
                break
    return rows


def main():
    apg = argparse.ArgumentParser()
    apg.add_argument("--business", type=int, default=5)
    apg.add_argument("--source", default=DEFAULT_SOURCE)
    apg.add_argument("--limit", type=int, default=0, help="0 = all")
    apg.add_argument("--min-score", type=float, default=0)
    apg.add_argument("--dry-run", action="store_true")
    a = apg.parse_args()

    rows = load_leads(a.source, a.limit or 10**9, a.min_score)
    print(f"loaded {len(rows)} leads from {a.source}")

    seen = existing_websites(a.business)
    fresh = [r for r in rows if r["website"] not in seen]
    dupes = len(rows) - len(fresh)
    print(f"already in funnel: {dupes} | new: {len(fresh)}")

    if a.dry_run:
        print("\nDRY RUN — first 10 to import:")
        for r in fresh[:10]:
            print(f"  {r['company']:<28} {r['website']:<40} {r['note'][:50]}")
        return

    if not fresh:
        print("nothing to import")
        return
    # import in chunks of 100
    for i in range(0, len(fresh), 100):
        chunk = fresh[i:i + 100]
        resp = api_post("/api/agentic/funnel/seeds/import",
                        {"business_id": a.business, "rows": chunk})
        print(f"imported chunk {i // 100 + 1}: {resp}")
    print("done — next: ✨ Enrich & score in the store (📊 Prospects tab)")


if __name__ == "__main__":
    main()
