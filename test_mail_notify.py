#!/usr/bin/env python3
"""P0-1 mail notify functional test — temp DB, real Flask test client."""
import os, sys, json, tempfile

TMP = os.path.join(tempfile.gettempdir(), "av_mail_test.db")
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


# a) default config
r = c.get("/api/agentic/mail/config")
j = r.get_json()
check("cfg-default", r.status_code == 200 and j["mail"]["enabled"] is False, json.dumps(j.get("mail")))

# b) PUT config with dummy smtp; password must be masked on read
r = c.put("/api/agentic/mail/config", json={"enabled": True, "host": "127.0.0.1", "port": 1,
                                            "user": "test@x.com", "password": "secret",
                                            "to_addr": "me@x.com", "tls": False})
j = r.get_json()
check("cfg-put-masked", r.status_code == 200 and j["mail"]["password"] == "****", json.dumps(j.get("mail")))
r = c.get("/api/agentic/mail/config")
check("cfg-get-masked", r.get_json()["mail"]["password"] == "****")
stored = ap._cfg_get("mail")
check("cfg-stored-clear", stored.get("password") == "secret", "stored pw must stay clear for SMTP")

# c) test send fails gracefully (port 1 -> conn refused), no crash
r = c.post("/api/agentic/mail/test", json={})
j = r.get_json()
check("test-502", r.status_code == 502 and j.get("ok") is False and j.get("error"), j.get("error"))

# d) notify endpoint queues + attempts flush
r = c.post("/api/agentic/mail/notify", json={"subject": "Swarm report", "body": "Deploy finished."})
j = r.get_json()
check("notify", r.status_code == 200 and j.get("queued") is True and j.get("sent") == 0
      and len(j.get("failed", [])) == 1, json.dumps(j))

# e) queue lists the mail as failed (send attempted, SMTP unreachable)
r = c.get("/api/agentic/mail/queue")
j = r.get_json()
check("queue-listed", r.status_code == 200 and len(j["mails"]) == 1
      and j["mails"][0]["status"] == "failed" and "Swarm report" in j["mails"][0]["subject"])

# f) GOAL COMPLETION HOOK: PUT status -> done must queue a mail
r = c.post("/api/agentic/goals", json={"title": "Deploy OmniRoute"})
gid = r.get_json()["goal"]["id"]
r = c.put("/api/agentic/goals/%d" % gid, json={"status": "done"})
r = c.get("/api/agentic/mail/queue")
subjects = [m["subject"] for m in r.get_json()["mails"]]
check("goal-hook", any("Goal complete" in s for s in subjects), json.dumps(subjects))
# no duplicate when PUT twice with same status
r = c.put("/api/agentic/goals/%d" % gid, json={"status": "done"})
r = c.get("/api/agentic/mail/queue")
subjects = [m["subject"] for m in r.get_json()["mails"]]
n_goal = sum(1 for s in subjects if "Goal complete" in s)
check("goal-hook-dedup", n_goal == 1, json.dumps(subjects))

# g) MISSION COMPLETION HOOK: active mission + all tasks verified -> tick -> mail
conn = ap._db()
now = "2026-08-10 00:00:00"
cur = conn.execute("INSERT INTO missions (title, status, progress, created, updated) VALUES (?,?,?,?,?)",
                   ("Test Mission", "active", 0, now, now))
mid = cur.lastrowid
conn.execute("INSERT INTO mission_tasks (mission_id, seq, title, task_type, executor, status, verified, created, updated) "
             "VALUES (?,?,?,?,?,?,?,?,?)",
             (mid, 1, "do something", "build", "mission", "verified", 1, now, now))
conn.commit()
conn.close()
events = ap._mission_tick()
print("tick events:", events)
check("mission-marked-done", "M%d done" % mid in " ".join(events) or any("done" in e and "M%d" % mid in e for e in events), json.dumps(events))
r = c.get("/api/agentic/mail/queue")
subjects = [m["subject"] for m in r.get_json()["mails"]]
check("mission-hook", any("Mission complete" in s for s in subjects), json.dumps(subjects))

# h) DB rows survive a "restart" (fresh connection, table + rows intact)
conn = ap._db()
n = conn.execute("SELECT COUNT(*) c FROM mail_queue").fetchone()["c"]
conn.close()
check("persist", n >= 3, "mail_queue rows: %d" % n)

fails = [x for x in results if not x[1]]
print("\n%d/%d passed" % (len(results) - len(fails), len(results)))
for f in fails:
    print("FAILED:", f[0], f[2])
sys.exit(1 if fails else 0)
