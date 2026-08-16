"""Synthetic test for the prospect-seeds layer (agentic_plane.py).

Serves fake pages from a local HTTP server:
- /customers : a "customer list" page linking to company sites (connector source)
- /c/acme, /c/globex, /c/initech : company homepages with GRC-ish keywords
- /c/noise : a homepage with NO signal keywords

Flow (NO LLM): configure tenant connectors+signals -> run connector -> import
CSV -> enrich -> assert scores/facts. Proves NOTHING is hardcoded: signals
are tenant config and scoring is generic.

Usage: uv run --isolated --no-project --with flask python test_funnel_seeds.py
"""
import http.server
import json
import os
import sqlite3
import threading

DB = os.environ.get("AGENTIC_DB_PATH", "C:/tmp/funnel-seeds-test.db")
if os.path.exists(DB):
    os.remove(DB)

import agentic_plane as ap  # noqa: E402
ap._pipeline_ensure()  # create the projects table (agent does this at boot)

BID = "995"

PAGES = {
    "/customers": ("<html><body>Trusted by "
                   "<a href='http://127.0.0.1:8931/c/acme'>Acme Corp</a>, "
                   "<a href='http://127.0.0.1:8931/c/globex'>Globex</a>, "
                   "<a href='http://127.0.0.1:8931/c/initech'>Initech</a>, "
                   "<a href='http://127.0.0.1:8931/c/noise'>Noise LLC</a></body></html>"),
    "/c/acme": "<html><head><title>Acme Corp — SOC 2 & ISO 27001 compliance</title></head>"
               "<body>We maintain a trust center and privacy policy. <a href='https://acme.local'>home</a></body></html>",
    "/c/globex": "<html><head><title>Globex Logistics</title></head><body>Fleet tracking.</body></html>",
    "/c/initech": "<html><head><title>Initech — GDPR privacy</title></head><body>Privacy policy here.</body></html>",
    "/c/noise": "<html><head><title>Noise LLC</title></head><body>Coffee beans.</body></html>",
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path)
        if page is None:
            self.send_response(404); self.end_headers(); return
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 8931), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

from flask import Flask  # noqa: E402
app = Flask(__name__)
app.register_blueprint(ap.agentic_bp)
client = app.test_client()

# seed a business
conn = sqlite3.connect(DB)
conn.execute("INSERT OR IGNORE INTO businesses (id, name, website, created, updated) "
             "VALUES (995, 'Seed Test Co', 'https://seedtest.example', datetime('now'), datetime('now'))")
conn.commit()
conn.close()


def ok(r, what):
    b = r.get_json()
    assert r.status_code == 200, f"{what} failed: {r.status_code} {b}"
    return b


# ---- 1. TENANT CONFIG (nothing hardcoded): signals + connector for THIS business ----
r = client.put("/api/agentic/funnel/connectors", json={
    "business_id": BID,
    "signals": {"soc 2": 3, "iso 27001": 3, "trust center": 2, "privacy": 1, "gdpr": 2},
    "connectors": [{"name": "customer-list", "url": "http://127.0.0.1:8931/customers",
                    "link_regex": r"http://127\.0\.0\.1:8931/c/"}]})
cfg = ok(r, "connectors config")
print("CONFIG OK:", cfg)
assert cfg["signals"].get("soc 2") == 3 and cfg["connectors"][0]["name"] == "customer-list"

# ---- 2. RUN CONNECTOR -> seeds from the real list page ----
r = client.post("/api/agentic/funnel/connector/run", json={"business_id": BID, "name": "customer-list"})
b = ok(r, "connector run")
print("CONNECTOR OK:", b)
assert b["added"] == 4, f"expected 4 candidates added, got {b}"

# ---- 3. CSV IMPORT (header names matched generically) ----
r = client.post("/api/agentic/funnel/seeds/import", json={
    "business_id": BID,
    "csv": "Company,URL\nUmbrella Corp,http://127.0.0.1:8931/c/acme\n"})
b = ok(r, "csv import")
print("IMPORT OK:", b)
assert b["added"] == 1

# ---- 4. ENRICH: fetch real pages, score by tenant signals ----
r = client.post("/api/agentic/funnel/seeds/enrich", json={"business_id": BID, "limit": 50})
b = ok(r, "enrich")
print("ENRICH OK:", b)
assert b["scored"] >= 5 and b["with_signal"] >= 2, b

# ---- 5. ASSERT SCORES from the DB ----
conn = sqlite3.connect(DB)
rows = conn.execute("SELECT company, signal_score, site_title FROM prospect_seeds WHERE business_id=? "
                    "ORDER BY signal_score DESC", (BID,)).fetchall()
conn.close()
print("SCORES:", [(r[0], r[1], r[2][:30]) for r in rows])
scores = {r[0]: r[1] for r in rows}
assert scores.get("Acme Corp") == 9, f"Acme (soc2+iso+trust+privacy) should be 9: {scores}"
assert scores.get("Initech") == 3, f"Initech (gdpr+privacy) should be 3: {scores}"
assert scores.get("Globex") == 0, f"Globex should be 0: {scores}"
assert scores.get("Noise LLC") == 0, f"Noise should be 0: {scores}"
assert scores.get("Umbrella Corp") == 9, f"Umbrella should be 9: {scores}"
# site titles captured
titles = {r[0]: r[2] for r in rows}
assert "SOC 2" in titles.get("Acme Corp", "")

# ---- 6. SEED-MODE RUN plumbing: _funnel_run_for with seed_ids picks them up ----
seeds = ap._funnel_seeds_ensure()
conn = sqlite3.connect(DB)
seed_ids = [r[0] for r in conn.execute("SELECT id FROM prospect_seeds WHERE business_id=? ORDER BY signal_score DESC LIMIT 3", (BID,)).fetchall()]
conn.close()
assert len(seed_ids) == 3
# call the internal runner in seed mode; stage 1 must SKIP the LLM and use seeds.
# We simulate the LLM calls by monkeypatching _funnel_stage to echo the prompt.
calls = []
orig = ap._funnel_stage
orig_guarded = ap._funnel_stage_guarded
ap._funnel_stage = lambda prompt, agent, timeout=90: (calls.append(agent), prompt)[1]
ap._funnel_stage_guarded = lambda prompt, agent, min_len=300, timeout=150: (calls.append(agent), "PROFILE OUTPUT")[1]
try:
    code, payload = ap._funnel_run_for(BID, research_n=2, seed_ids=seed_ids)
    ap._funnel_stage = orig
    ap._funnel_stage_guarded = orig_guarded
except Exception:
    ap._funnel_stage = orig
    ap._funnel_stage_guarded = orig_guarded
    raise
print("SEED-MODE RUN:", code, payload.get("status"))
assert code == 200 and len(payload["research"]) == 2 and len(payload["outreach"]) == 2
assert "crew-prospector" not in calls, f"prospector must be skipped in seed mode, calls={calls}"
assert calls.count("crew-researcher") == 2 and calls.count("crew-sdr") == 2

print("SEEDS TESTS PASSED — scores=%s" % {r[0]: r[1] for r in rows})
