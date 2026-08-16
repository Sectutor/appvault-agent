#!/usr/bin/env python3
"""Mission Loop v3: metrics (revenue/leads) + goal bump, product template with
compile-verify, task delay endpoint, metrics in reports."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    edits = 0

    def rep(old, new):
        nonlocal src, edits
        assert old in src, 'ANCHOR MISSING: ' + old[:90]
        src = src.replace(old, new, 1)
        edits += 1

    # 1. product template
    rep("""    "outreach": {
        "name": "Outreach mission",
        "description": "Research prospects -> draft emails -> send (SMTP) -> follow up (+24h) -> report",
        "tasks": [
            {"title": "Research prospects + industry signals", "task_type": "research", "executor": "mission"},
            {"title": "Draft personalized outreach emails", "task_type": "draft_emails", "executor": "mission", "depends_on": 0},
            {"title": "Send emails via SMTP", "task_type": "send_emails", "executor": "mission", "depends_on": 1},
            {"title": "Follow up with non-repliers (+24h)", "task_type": "followup", "executor": "mission", "depends_on": 2, "wait_minutes": 1440},
            {"title": "Record + report + goal bump", "task_type": "report", "executor": "mission", "depends_on": 3},
        ],
    },
}""",
        """    "outreach": {
        "name": "Outreach mission",
        "description": "Research prospects -> draft emails -> send (SMTP) -> follow up (+24h) -> report",
        "tasks": [
            {"title": "Research prospects + industry signals", "task_type": "research", "executor": "mission"},
            {"title": "Draft personalized outreach emails", "task_type": "draft_emails", "executor": "mission", "depends_on": 0},
            {"title": "Send emails via SMTP", "task_type": "send_emails", "executor": "mission", "depends_on": 1},
            {"title": "Follow up with non-repliers (+24h)", "task_type": "followup", "executor": "mission", "depends_on": 2, "wait_minutes": 1440},
            {"title": "Record + report + goal bump", "task_type": "report", "executor": "mission", "depends_on": 3},
        ],
    },
    "product": {
        "name": "Product mission",
        "description": "Spec -> build -> verify (compile) -> ship (manifest + ledger) -> report",
        "tasks": [
            {"title": "Write the build spec (files + acceptance criteria)", "task_type": "spec", "executor": "mission"},
            {"title": "Build the artifact (Python code)", "task_type": "build", "executor": "mission", "depends_on": 0},
            {"title": "Verify the artifact (compile + gates)", "task_type": "verify_build", "executor": "mission", "depends_on": 1},
            {"title": "Ship the artifact (manifest + work record)", "task_type": "ship", "executor": "mission", "depends_on": 2},
            {"title": "Record + report + goal bump", "task_type": "report", "executor": "mission", "depends_on": 3},
        ],
    },
}""")

    # 2. metrics table in migrate
    rep("""    CREATE TABLE IF NOT EXISTS mission_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id INTEGER, seq INTEGER, title TEXT, task_type TEXT, executor TEXT,
        status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, last_error TEXT,
        wait_until TEXT, depends_on INTEGER, result_ref TEXT, verified INTEGER DEFAULT 0,
        created TEXT, updated TEXT
    );
    """,
        """    CREATE TABLE IF NOT EXISTS mission_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id INTEGER, seq INTEGER, title TEXT, task_type TEXT, executor TEXT,
        status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, last_error TEXT,
        wait_until TEXT, depends_on INTEGER, result_ref TEXT, verified INTEGER DEFAULT 0,
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_type TEXT, value REAL, note TEXT, ts TEXT DEFAULT (datetime('now'))
    );
    """)

    # 3. product executors (before _mission_review)
    rep("""def _mission_review(mission):""",
        """def _mission_spec(mission, task, ctx):
    topic = mission.get("title") or "Build a small tool"
    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\\n"
              "Write a build spec for: %s\\nSpec must include: purpose, 1-3 files (Python), "
              "key functions, and 3 acceptance criteria. Output ONLY the spec (markdown)."
              % (ctx["voice"], topic))
    out = _call_llm_with({}, prompt, agent="hermes", timeout=180)
    out = (out or "").strip()
    if len(out) < 150:
        return (False, None, "spec too short (%d chars)" % len(out))
    path = _write_vault_output("05_Build", "spec_%s.md" % _mission_slug(topic), out, tag="Spec", agent="Mission")
    return (True, path, None)


def _mission_build(mission, task, ctx):
    spec = _read_dep_result(task) or ""
    topic = mission.get("title") or "tool"
    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\\n"
              "Write the COMPLETE, syntactically valid Python 3 script implementing this spec. "
              "Output ONLY the code (no markdown fences, no explanation).\\n\\nSpec:\\n%s"
              % (ctx["voice"], spec[:3000]))
    out = _call_llm_with({}, prompt, agent="hermes", timeout=300)
    out = (out or "").strip()
    out = out.replace("```python", "").replace("```", "").strip()
    if len(out) < 60:
        return (False, None, "artifact too short (%d chars)" % len(out))
    path = _write_vault_output("05_Build", "%s.py" % _mission_slug(topic), out, tag="Build", agent="Mission")
    return (True, path, None)


def _mission_verify_build(mission, task, ctx):
    ref = _read_dep_result(task) or ""
    code = _read_ref(ref) or ""
    if not code:
        return (False, None, "no artifact to verify")
    try:
        compile(code, "<mission>", "exec")
    except SyntaxError as e:
        return (False, None, "SYNTAX ERROR line %s: %s" % (getattr(e, "lineno", "?"), str(e)[:120]))
    if len(code) < 60:
        return (False, None, "artifact too short (%d chars)" % len(code))
    lines = len(code.splitlines())
    return (True, "compile-ok %d lines" % lines, None)


def _mission_ship(mission, task, ctx):
    ref = _read_dep_result(task) or ""
    code = _read_ref(ref) or ""
    slug = _mission_slug(mission.get("title") or "artifact")
    shipped = []
    try:
        vault = _vault_path()
        d = os.path.join(vault, "05_Build", "shipped")
        os.makedirs(d, exist_ok=True)
        spath = os.path.join(d, "%s.py" % slug)
        with open(spath, "w", encoding="utf-8") as f:
            f.write(code)
        manifest = os.path.join(d, "SHIPPED.md")
        with open(manifest, "a", encoding="utf-8") as f:
            f.write("- %s | %s | %d lines | %s\\n" % (slug, mission.get("title", ""), len(code.splitlines()),
                                                      datetime.now().strftime("%Y-%m-%d %H:%M")))
        shipped.append(spath)
    except Exception as e:
        return (False, None, "ship failed: %s" % str(e)[:150])
    try:
        _work_record(category="product", title=mission.get("title") or "Product artifact",
                     content=("shipped artifact: %s" % slug)[:500], source="mission")
    except Exception:
        pass
    return (True, "shipped:%s" % slug, None)


def _mission_review(mission):""")

    # 4. dispatch new task types
    rep("""        if ttype == "followup":
            return _mission_followup(mission, task, ctx)
        return (False, None, "unknown task_type: %s" % ttype)""",
        """        if ttype == "followup":
            return _mission_followup(mission, task, ctx)
        if ttype == "spec":
            return _mission_spec(mission, task, ctx)
        if ttype == "build":
            return _mission_build(mission, task, ctx)
        if ttype == "verify_build":
            return _mission_verify_build(mission, task, ctx)
        if ttype == "ship":
            return _mission_ship(mission, task, ctx)
        return (False, None, "unknown task_type: %s" % ttype)""")

    # 5. report includes recent metrics
    rep("""    lines = ["# Mission report: %s" % mission.get("title", "")]
    lines.append("Objective: %s | Status: %s" % (mission.get("objective_type", ""), mission.get("status", "")))""",
        """    lines = ["# Mission report: %s" % mission.get("title", "")]
    lines.append("Objective: %s | Status: %s" % (mission.get("objective_type", ""), mission.get("status", "")))
    try:
        conn = _db()
        mrows = conn.execute("SELECT metric_type, value, note FROM metrics ORDER BY id DESC LIMIT 3").fetchall()
        conn.close()
        if mrows:
            lines.append("\\nRecent metrics:")
            for mr in mrows:
                lines.append("- %s: %s%s" % (mr["metric_type"], mr["value"], (" (" + mr["note"] + ")") if mr["note"] else ""))
    except Exception:
        pass""")

    # 6. API endpoints: metrics + task delay (before the templates route)
    rep("""@agentic_bp.route("/api/agentic/missions/templates", methods=["GET", "OPTIONS"])
def api_mission_templates():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok", "templates": MISSION_TEMPLATES})""",
        """@agentic_bp.route("/api/agentic/metrics", methods=["GET", "POST", "OPTIONS"])
def api_metrics():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        mtype = (data.get("type") or "").strip().lower()
        if mtype not in ("revenue", "leads", "traffic"):
            return jsonify({"error": "type must be revenue|leads|traffic"}), 400
        try:
            value = float(data.get("value") or 0)
        except Exception:
            return jsonify({"error": "value must be a number"}), 400
        note = (data.get("note") or "").strip()[:200]
        conn = _db()
        cur = conn.execute("INSERT INTO metrics (metric_type, value, note) VALUES (?,?,?)", (mtype, value, note))
        conn.commit()
        conn.close()
        # revenue metrics feed business goals
        if mtype == "revenue" and value > 0:
            _goal_bump("revenue", "revenue", 2, "metrics", note=("Revenue recorded: $%s" % value))
        return jsonify({"status": "ok", "metric_id": cur.lastrowid})
    conn = _db()
    rows = conn.execute("SELECT * FROM metrics ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"status": "ok", "metrics": [dict(r) for r in rows]})


@agentic_bp.route("/api/agentic/tasks/<int:tid>/delay", methods=["POST", "OPTIONS"])
def api_task_delay(tid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    try:
        minutes = int(data.get("minutes") or 0)
    except Exception:
        return jsonify({"error": "minutes required"}), 400
    if minutes < 1:
        return jsonify({"error": "minutes must be >= 1"}), 400
    wait = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    conn.execute("UPDATE mission_tasks SET wait_until=?, updated=? WHERE id=?", (wait, wait, tid))
    conn.commit()
    row = conn.execute("SELECT * FROM mission_tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "task": _mtask_to_dict(row) if row else None})


@agentic_bp.route("/api/agentic/missions/templates", methods=["GET", "OPTIONS"])
def api_mission_templates():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok", "templates": MISSION_TEMPLATES})""")

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    print('mission_loop_block.py v3 —', edits, 'edits')

if __name__ == '__main__':
    main()
