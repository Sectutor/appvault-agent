#!/usr/bin/env python3
"""P0-2 message bus functional test — temp DB, real Flask test client + live SSE."""
import os, sys, json, tempfile, threading, queue as qmod

TMP = os.path.join(tempfile.gettempdir(), "av_bus_test.db")
for suf in ("", "-wal", "-shm"):
    p = TMP + suf
    if os.path.exists(p):
        os.remove(p)
os.environ["AGENTIC_DB_PATH"] = TMP
os.chdir(r"D:\DATA_INTELLFENCE\WebDev\AppVault\agent")
sys.path.insert(0, r"D:\DATA_INTELLFENCE\WebDev\AppVault\agent")

import agentic_plane as ap
from flask import Flask

app = Flask(__name__)
app.register_blueprint(ap.agentic_bp)
c = app.test_client()
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


# 1) publish via path route
r = c.post("/api/agentic/bus/deploy", json={"app": "omniroute", "status": "ok"})
j = r.get_json()
check("publish-path", r.status_code == 200 and j["event"]["topic"] == "deploy"
      and j["event"]["payload"]["app"] == "omniroute", json.dumps(j))
eid1 = j["event"]["id"]

# 2) publish via body route
r = c.post("/api/agentic/bus/publish", json={"topic": "security.scan", "payload": {"ok": True, "ports": [22, 80]}})
j = r.get_json()
check("publish-body", r.status_code == 200 and j["event"]["topic"] == "security.scan"
      and j["event"]["payload"]["ports"] == [22, 80], json.dumps(j))
eid2 = j["event"]["id"]

# 3) invalid topic -> 400
r = c.post("/api/agentic/bus/bad%20topic", json={"x": 1})
check("publish-invalid", r.status_code == 400, "status=%d" % r.status_code)

# 4) topics listing
r = c.get("/api/agentic/bus/topics")
tops = {t["topic"]: t["c"] for t in r.get_json()["topics"]}
check("topics", tops.get("deploy") == 1 and tops.get("security.scan") == 1, json.dumps(tops))

# 5) replay filtered
r = c.get("/api/agentic/bus/replay?topics=deploy")
evs = r.get_json()["events"]
check("replay-filter", len(evs) == 1 and evs[0]["id"] == eid1 and evs[0]["payload"]["status"] == "ok", json.dumps(evs))

# 6) replay since
r = c.get("/api/agentic/bus/replay?since=%d" % eid2)
check("replay-since", r.get_json()["events"] == [], json.dumps(r.get_json()["events"]))

# 7) replay all (no topic filter)
r = c.get("/api/agentic/bus/replay")
check("replay-all", len(r.get_json()["events"]) == 2, str(len(r.get_json()["events"])))

# 8) LIVE SSE round-trip: open stream, get historical replay, then live push
chunks = qmod.Queue()
stream_resp = c.get("/api/agentic/bus/stream?topics=deploy", buffered=False)


def _read_stream():
    try:
        for chunk in stream_resp.response:
            chunks.put(chunk)
    except Exception as e:
        chunks.put(b"ERR:" + str(e).encode())


th = threading.Thread(target=_read_stream, daemon=True)
th.start()
first = chunks.get(timeout=6)
text = first.decode() if isinstance(first, bytes) else str(first)
check("sse-historical", 'event: message' in text and '"topic": "deploy"' in text and '"app": "omniroute"' in text, text[:200])

r = c.post("/api/agentic/bus/deploy", json={"msg": "second"})
eid3 = r.get_json()["event"]["id"]
second = chunks.get(timeout=6)
text2 = second.decode() if isinstance(second, bytes) else str(second)
check("sse-live", '"msg": "second"' in text2 and '"id": %d' % eid3 in text2, text2[:200])

# 8b) subscribe/unsubscribe cleanup (unit level — deterministic)
q1 = ap._bus_subscribe(["unit.topic"])
ev = ap._bus_publish("unit.topic", {"u": 1})
got = q1.get(timeout=2)
check("unit-subscribe", got["payload"]["u"] == 1 and got["topic"] == "unit.topic", json.dumps(got))
ap._bus_unsubscribe(["unit.topic"], q1)
check("unit-unsubscribe", "unit.topic" not in ap._BUS_SUBSCRIBERS,
      json.dumps({k: len(v) for k, v in ap._BUS_SUBSCRIBERS.items()}))

# 9) TTL: ttl=0 event is purged by the NEXT publish and absent from replay
r = c.post("/api/agentic/bus/ttl-test", json={"x": 1}, query_string="ttl=0")
r = c.post("/api/agentic/bus/deploy", json={"msg": "purge-trigger"})
r = c.get("/api/agentic/bus/replay?topics=ttl-test")
check("ttl-expired", r.get_json()["events"] == [], json.dumps(r.get_json()["events"]))
conn = ap._db()
n = conn.execute("SELECT COUNT(*) c FROM bus_events WHERE topic='ttl-test'").fetchone()["c"]
conn.close()
check("ttl-row-gone", n == 0, "rows=%d" % n)

# 10) persistence across "restart" (fresh connection)
conn = ap._db()
n = conn.execute("SELECT COUNT(*) c FROM bus_events").fetchone()["c"]
conn.close()
check("persist", n >= 3, "bus_events rows=%d" % n)

# 11) vector-vault feed: memory rows tagged bus:<topic>
conn = ap._db()
rows = conn.execute("SELECT tag FROM memory WHERE source='bus'").fetchall()
conn.close()
tags = [r["tag"] for r in rows]
check("memory-feed", any(t == "bus:deploy" for t in tags) and any(t == "bus:security.scan" for t in tags), json.dumps(tags))

fails = [x for x in results if not x[1]]
print("\n%d/%d passed" % (len(results) - len(fails), len(results)))
for f in fails:
    print("FAILED:", f[0], f[2])
sys.exit(1 if fails else 0)
