"""Live pilot runner for the Client Acquisition Funnel.

Runs the full sequential chain (lead -> research -> outreach -> proposal ->
delivery) against a REAL business on the LIVE agentic.db. ADDS work_items
rows only — never modifies existing rows or config. Stages 4-5 (replied /
accepted) are human gates; the pilot flags them as SIMULATED in tags.

Usage:
  python run_funnel_pilot.py --list            # list businesses (read-only)
  python run_funnel_pilot.py --bid 5           # full chain for business id
  python run_funnel_pilot.py --bid 5 --stages 1-3   # lead+research+outreach only
Env: AGENTIC_DB_PATH (default /data/agentic.db)
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentic_plane as ap  # noqa: E402

DB = os.environ.get("AGENTIC_DB_PATH", "/data/agentic.db")
ap._DB_PATH = DB  # force the live DB (import may have cached an env value)


def q(sql, args=()):
    conn = sqlite3.connect(DB, timeout=30)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def main():
    apg = argparse.ArgumentParser()
    apg.add_argument("--list", action="store_true")
    apg.add_argument("--bid", type=int)
    apg.add_argument("--stages", default="1-5")
    apg.add_argument("--leads", type=int, default=5, help="lead_count for the run (1-15)")
    apg.add_argument("--research", type=int, default=3, help="research_n for the run (1-5)")
    a = apg.parse_args()

    if a.list:
        for r in q("SELECT id, name, website FROM businesses ORDER BY id"):
            print(f"{r[0]}\t{r[1]}\t{r[2] or ''}")
        return

    if not a.bid:
        print("--bid required (or --list)")
        sys.exit(2)

    lo, hi = (int(x) for x in a.stages.split("-"))
    biz = dict(ap._get_business(a.bid) or {})
    if not biz:
        print(f"business {a.bid} not found")
        sys.exit(1)
    print(f"=== PILOT FUNNEL for business #{a.bid}: {biz.get('name')} ===")

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ap.agentic_bp)
    client = app.test_client()

    out = {}
    if lo <= 1:
        r = client.post("/api/agentic/funnel/run", json={"business_id": a.bid,
                                                         "lead_count": a.leads, "research_n": a.research})
        b = r.get_json()
        assert r.status_code == 200, f"run failed: {r.status_code} {b}"
        out.update(b)
        out["leads_wids"] = b["leads"]
        out["research_wids"] = b.get("research") or []
        out["outreach_wids"] = b.get("outreach") or []
        print(f"[1-3] RUN OK -> lead={b['leads']} research={out['research_wids']} outreach={out['outreach_wids']}")

    if lo <= 4 and hi >= 4 and out.get("outreach_wids"):
        r = client.post("/api/agentic/funnel/replied", json={"wid": out["outreach_wids"][0]})
        b = r.get_json()
        assert r.status_code == 200, f"replied failed: {r.status_code} {b}"
        out["proposal"] = b["proposal"]
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE work_items SET tags=tags || ',pilot:simulated-reply' WHERE id=?", (out["outreach_wids"][0],))
        conn.commit(); conn.close()
        print(f"[4] REPLIED (SIMULATED) -> proposal={out['proposal']}")

    if lo <= 5 and hi >= 5 and out.get("proposal"):
        r = client.post("/api/agentic/funnel/accepted", json={"wid": out["proposal"]})
        b = r.get_json()
        assert r.status_code == 200, f"accepted failed: {r.status_code} {b}"
        out["delivery"] = b["delivery"]
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE work_items SET tags=tags || ',pilot:simulated-accept' WHERE id=?", (out["proposal"],))
        conn.commit(); conn.close()
        print(f"[5] ACCEPTED (SIMULATED) -> delivery={out['delivery']}")

    # ---- metrics ----
    print("\n=== PILOT METRICS ===")
    ids = (out.get("leads_wids") or []) + (out.get("research_wids") or []) + \
          (out.get("outreach_wids") or []) + [out.get("proposal"), out.get("delivery")]
    total_chars = 0
    for wid in ids:
        if not wid:
            continue
        r = q("SELECT category, status, length(content), substr(content,1,120) FROM work_items WHERE id=?", (wid,))[0]
        total_chars += r[2]
        print(f"{r[0]:<16} {r[1]:<20} {r[2]:>6} chars | {r[3]}...")
    calls = len([x for x in ids if x])
    print(f"\nLLM calls (real): {calls} | total output chars: {total_chars}")
    print(f"Est. output tokens ~ {total_chars // 4} (chars/4); input tokens extra (context grows per stage)")
    print("\nStages: lead -> research -> outreach (GATE 1) -> proposal (GATE 2) -> delivery")
    print("NOTE: gates 4-5 flagged pilot:simulated in tags; C1 applies — drafts are NOT verified facts.")


if __name__ == "__main__":
    main()
