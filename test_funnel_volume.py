"""Volume + reply-loop test for the funnel (agentic_plane.py).

Throwaway DB, REAL LLM calls via the local LiteLLM hub (deepseek-chat).
Covers: lead_count/research_n volume run, schedule API, reply triage
(positive -> proposal, question -> followup, negative -> closed), and the
auto follow-up nudge. Never touches the live agentic.db.

Usage: uv run --isolated --no-project --with flask python test_funnel_volume.py
"""
import json
import os
import re
import sqlite3

DB = os.environ.get("AGENTIC_DB_PATH", "C:/tmp/funnel-vol-test.db")
if os.path.exists(DB):
    os.remove(DB)

import agentic_plane as ap  # noqa: E402

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

conn = sqlite3.connect(DB)
conn.execute("INSERT OR IGNORE INTO businesses (id, name, website, created, updated) "
             "VALUES (997, 'Volume Test Co', 'https://volumetest.example', "
             "datetime('now'), datetime('now'))")
conn.commit()
conn.close()

from flask import Flask  # noqa: E402
app = Flask(__name__)
app.register_blueprint(ap.agentic_bp)
client = app.test_client()


def show(wid):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT category, status, length(content), project, tags FROM work_items WHERE id=?",
                       (wid,)).fetchone()
    conn.close()
    return row


def run_ok(r, what):
    body = r.get_json()
    assert r.status_code == 200, f"{what} failed: {r.status_code} {body}"
    return body


# ---- 1. VOLUME RUN: 6 leads, research top 2 ----
r = client.post("/api/agentic/funnel/run", json={"business_id": 997, "lead_count": 6, "research_n": 2})
body = run_ok(r, "volume run")
lead_wid = body["leads"][0]
res_wids = body["research"]
out_wids = body["outreach"]
print("VOLUME RUN OK:", {"leads": len(body["leads"]), "research": len(res_wids), "outreach": len(out_wids)})
assert len(res_wids) == 2 and len(out_wids) == 2, "expected 2 research + 2 outreach"
for i, (rw, ow) in enumerate(zip(res_wids, out_wids)):
    rs, os_ = show(rw), show(ow)
    print(f"  chain#{i+1}: {rs[:3]} | {os_[:3]}")
    assert rs[1] == "enriched" and os_[1] == "ready_for_approval"
    assert "prev:" + rw in os_[4] and "prev:" + lead_wid in rs[4]
    assert "cand:" + str(i + 1) in os_[4]
# funnel items registered under the project
p = show(lead_wid)[3]
assert p == "volumetestco", f"unexpected project {p}"

# ---- 2. SCHEDULE API ----
r = client.post("/api/agentic/funnel/schedule", json={"business_id": 997, "interval": "daily", "weekly_cap": 7})
s = run_ok(r, "schedule set")
print("SCHEDULE OK:", s)
assert s["schedule"]["interval"] == "daily" and s["schedule"]["weekly_cap"] == 7
r = client.get("/api/agentic/funnel/schedule?business_id=997")
assert run_ok(r, "schedule get")["schedule"]["interval"] == "daily"
# bad interval rejected
r = client.post("/api/agentic/funnel/schedule", json={"business_id": 997, "interval": "hourly"})
assert r.status_code == 400, "hourly should be rejected"

# ---- 3. REPLY TRIAGE ----
# 3a. positive -> proposal
r = client.post("/api/agentic/funnel/reply", json={"wid": out_wids[0],
                 "text": "Thanks for reaching out — we'd love to talk. When can we set up a call?"})
b = run_ok(r, "positive reply")
print("POSITIVE OK:", b["action"], b["class"])
assert b["action"] == "proposal" and b.get("proposal")
assert show(out_wids[0])[1] == "replied"
assert show(b["proposal"])[1] == "ready_for_approval"

# 3b. question -> followup draft
r = client.post("/api/agentic/funnel/reply", json={"wid": out_wids[1],
                 "text": "Interesting. What exactly does this cost and how long does it take?"})
b = run_ok(r, "question reply")
print("QUESTION OK:", b["action"], b["class"])
assert b["action"] == "followup" and b.get("followup")
fu = show(b["followup"])
assert fu[1] == "ready_for_approval" and "funnel:followup" in fu[4]
assert "prev:" + out_wids[1] in fu[4]

# 3c. negative -> closed
r = client.post("/api/agentic/funnel/reply", json={"wid": out_wids[0],
                 "text": "Thanks but we're not interested at this time. Please remove us from your list."})
b = run_ok(r, "negative reply")
print("NEGATIVE OK:", b["action"], b["class"])
assert b["action"] == "closed"
assert show(out_wids[0])[1] == "rejected"

# ---- 4. AUTO FOLLOW-UP NUDGE ----
# Mark the remaining outreach (out_wids[1] already has a followup) — use the
# proposal's chain: backdate an APPROVED outreach with no followup.
conn = sqlite3.connect(DB)
conn.execute("UPDATE work_items SET status='approved', created_at=datetime('now','-6 days') WHERE id=?", (out_wids[0],))
conn.commit()
conn.close()
ap._funnel_followup_nudge(days=5, max_n=2)
conn = sqlite3.connect(DB)
nudges = conn.execute("SELECT count(*) FROM work_items WHERE tags LIKE '%funnel:followup%' AND tags LIKE ?",
                      ("%prev:" + out_wids[0] + "%",)).fetchone()[0]
conn.close()
print("NUDGE OK: followups for out#1 =", nudges)
assert nudges >= 1, "nudge should have created a follow-up for the approved, un-replied outreach"

print("\nALL VOLUME + REPLY-LOOP TESTS PASSED")
