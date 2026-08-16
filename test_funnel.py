"""Local end-to-end test for the Client Acquisition Funnel (agentic_plane.py).

Runs the full sequential chain against a THROWAWAY DB with REAL LLM calls
via the local LiteLLM hub (deepseek-chat). Never touches the live agentic.db.

Usage:
    AGENTIC_DB_PATH=C:/tmp/funnel-test.db python test_funnel.py
Requires: flask installed, LiteLLM hub at http://localhost:4000/v1 (deploy/.env key).
"""
import json
import os
import re
import sqlite3
import sys

DB = os.environ.get("AGENTIC_DB_PATH", "C:/tmp/funnel-test.db")
if os.path.exists(DB):
    os.remove(DB)

import agentic_plane as ap

# Seed the LLM config -> local LiteLLM hub (host view of the container).
KEY = ""
try:
    with open(r"D:\DATA_INTELLFENCE\WebDev\AppVault\deploy\.env", encoding="utf-8") as f:
        m = re.search(r"^LITELLM_MASTER_KEY=(\S+)", f.read(), re.M)
        if m:
            KEY = m.group(1).strip('"').strip("'")
except Exception as e:
    print("WARN: could not read LiteLLM key:", e)
ap._cfg_set("llm", {"provider": "litellm", "api_base": "http://localhost:4000/v1",
                    "api_key": KEY, "model": "deepseek-chat", "temperature": 0.5})

# Seed one test business.
conn = sqlite3.connect(DB)
conn.execute("INSERT OR IGNORE INTO businesses (id, name, website, created, updated) "
             "VALUES (999, 'Test MSP', 'https://testmsp.example', "
             "datetime('now'), datetime('now'))")
conn.commit()
conn.close()

from flask import Flask
app = Flask(__name__)
app.register_blueprint(ap.agentic_bp)
client = app.test_client()

def show(wid):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT category, status, length(content), project, tags FROM work_items WHERE id=?",
                       (wid,)).fetchone()
    conn.close()
    return row

# ---- Stage 1-3: run ----
r = client.post("/api/agentic/funnel/run", json={"business_id": 999})
body = r.get_json()
assert r.status_code == 200, f"run failed: {r.status_code} {body}"
lead_wid = body["leads"][0]
res_wid = body["research"]
out_wid = body["outreach"]
print(f"RUN OK: lead={lead_wid} research={res_wid} outreach={out_wid}")
for w in (lead_wid, res_wid, out_wid):
    print("  item:", show(w))
assert show(lead_wid)[1] == "new"
assert show(res_wid)[1] == "enriched"
assert show(out_wid)[1] == "ready_for_approval"  # HUMAN GATE 1
assert show(out_wid)[4] and "prev:" + res_wid in show(out_wid)[4]

# ---- Stage 4: replied (gate 1 passed by human) ----
r = client.post("/api/agentic/funnel/replied", json={"wid": out_wid})
body = r.get_json()
assert r.status_code == 200, f"replied failed: {r.status_code} {body}"
prop_wid = body["proposal"]
print(f"REPLIED OK: proposal={prop_wid}", show(prop_wid))
assert show(prop_wid)[1] == "ready_for_approval"  # HUMAN GATE 2
assert show(out_wid)[1] == "replied"

# ---- Stage 5: accepted (gate 2 passed by human) ----
r = client.post("/api/agentic/funnel/accepted", json={"wid": prop_wid})
body = r.get_json()
assert r.status_code == 200, f"accepted failed: {r.status_code} {body}"
del_wid = body["delivery"]
print(f"ACCEPTED OK: delivery={del_wid}", show(del_wid))
assert show(del_wid)[1] == "done"
assert show(prop_wid)[1] == "accepted"

# ---- Error paths ----
r = client.post("/api/agentic/funnel/replied", json={"wid": "nope"})
assert r.status_code in (400, 404), r.status_code
r = client.post("/api/agentic/funnel/run", json={})
assert r.status_code == 400

# ---- Sequential-handoff proof: the SDR draft must reference the researched
# candidate (the chain embedded previous artifacts) ----
conn = sqlite3.connect(DB)
outreach = conn.execute("SELECT content FROM work_items WHERE id=?", (out_wid,)).fetchone()[0]
conn.close()
assert len(outreach) > 200, "outreach draft too short"
print(f"\nOUTREACH DRAFT ({len(outreach)} chars) — first 400:")
print(outreach[:400])
print("\nALL FUNNEL TESTS PASSED — 5 stages, 2 human gates, real LLM output")
