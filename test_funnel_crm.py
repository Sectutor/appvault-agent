"""Synthetic test for the light CRM derivation (_funnel_prospects).

Seeds 4 chains directly in a throwaway DB (NO LLM calls):
A won (proposal accepted + delivery done, $149 value), B draft, C lost,
D followup (follow-up draft pending). Asserts stage mapping, name/DM/value
parsing, counts, and won revenue.

Usage: uv run --isolated --no-project --with flask python test_funnel_crm.py
"""
import os
import sqlite3

DB = os.environ.get("AGENTIC_DB_PATH", "C:/tmp/funnel-crm-test.db")
if os.path.exists(DB):
    os.remove(DB)

import agentic_plane as ap  # noqa: E402

PROJ = "crmtest"


_IDS = iter(["aa01", "aa02", "aa03", "aa04", "aa05", "bb01", "bb02", "bb03",
             "cc01", "cc02", "cc03", "dd01", "dd02", "dd03", "dd04"])


def seed(cat, status, content, tags, project=PROJ):
    wid = next(_IDS)  # work_items.id is TEXT (hex) in the real system
    conn = sqlite3.connect(DB)
    cur = conn.execute(
        "INSERT INTO work_items (id, category, status, content, title, source, tags, project, created_at, updated_at) "
        "VALUES (?,?,?,?,?, 'pipeline:funnel', ?, ?, datetime('now'), datetime('now'))",
        (wid, cat, status, content, f"{cat} {project}", tags, project))
    conn.commit()
    conn.close()
    return wid


# ---- chain A: won (outreach approved -> proposal accepted -> delivery done) ----
lA = seed("lead", "new", "Candidates:\n1. TRACETAC — security automation\n2. OtherCo", "funnel,stage:lead,biz:9")
rA = seed("lead_research", "enriched", "TRACETAC — VERIFIED CANDIDATE PROFILE\nCOMPANY FACTS\n- Open-source security", f"funnel,stage:research,prev:{lA}")
oA = seed("outreach_draft", "approved", "Subject: Tracecat — verified listing\n\nHey Andrew,\n\nYou've got momentum...", f"funnel,stage:outreach,prev:{rA}")
pA = seed("proposal", "accepted", "PROPOSAL: Tracecat Verified Listing\nPRICING:\n- Verified Listing: $49/mo\n- Featured: $149/mo", f"funnel,stage:proposal,prev:{oA}")
dA = seed("delivery", "done", "# Delivery Plan\n1. Listing live within 10 min", f"funnel,stage:delivery,prev:{pA}")

# ---- chain B: draft (outreach ready_for_approval) ----
lB = seed("lead", "new", "1. Acme Widgets — manufacturing", "funnel,stage:lead,biz:9")
rB = seed("lead_research", "enriched", "Acme Widgets Inc\n- 200 employees", f"funnel,stage:research,prev:{lB}")
oB = seed("outreach_draft", "ready_for_approval", "Hi Sarah,\n\nAcme is growing fast...", f"funnel,stage:outreach,prev:{rB}")

# ---- chain C: lost (outreach rejected) ----
lC = seed("lead", "new", "1. Globex — logistics", "funnel,stage:lead,biz:9")
rC = seed("lead_research", "enriched", "Globex Logistics\n- fleet 500", f"funnel,stage:research,prev:{lC}")
oC = seed("outreach_draft", "rejected", "Hi Bob,\n\n...", f"funnel,stage:outreach,prev:{rC}")

# ---- chain D: followup (outreach approved + follow-up draft pending) ----
lD = seed("lead", "new", "1. Initech — payroll", "funnel,stage:lead,biz:9")
rD = seed("lead_research", "enriched", "Initech Payroll Systems\n- 120 staff", f"funnel,stage:research,prev:{lD}")
oD = seed("outreach_draft", "approved", "Hello Nina,\n\n...", f"funnel,stage:outreach,prev:{rD}")
fD = seed("outreach_draft", "ready_for_approval", "CONTEXT — original outreach\nDRAFT NUDGE: Hi Nina, just following up...",
          f"funnel,stage:followup,prev:{oD},funnel:followup")

# ---- assertions ----
data = ap._funnel_prospects(PROJ)
prospects = data["prospects"]
print("prospects:", [(p["prospect"], p["stage"], p["value"], p["decision_maker"]) for p in prospects])

assert len(prospects) == 4, f"expected 4 prospects, got {len(prospects)}"

def find(name):
    return next((p for p in prospects if p["prospect"].lower().startswith(name.lower())), None)

a = find("tracetac")
assert a and a["stage"] == "won", f"chain A should be won: {a and a['stage']}"
assert a["value"] == 149, f"chain A value should be 149: {a['value']}"
assert a["decision_maker"] == "Andrew", f"chain A dm: {a['decision_maker']}"

b = find("acme")
assert b and b["stage"] == "draft", f"chain B should be draft: {b and b['stage']}"
assert b["decision_maker"] == "Sarah"

c = find("globex")
assert c and c["stage"] == "lost", f"chain C should be lost: {c and c['stage']}"

d = find("initech")
assert d and d["stage"] == "followup", f"chain D should be followup: {d and d['stage']}"

assert data["counts"] == {"new": 0, "draft": 1, "sent": 0, "followup": 1, "proposal": 0, "won": 1, "lost": 1}, data["counts"]
assert data["revenue_won"] == 149, data["revenue_won"]

# anchors point at the deepest item
assert a["anchor"]["category"] == "delivery" and a["anchor"]["status"] == "done"
assert d["anchor"]["category"] == "outreach_draft"

print("CRM DERIVATION TESTS PASSED: counts=%s revenue=%s" % (data["counts"], data["revenue_won"]))
