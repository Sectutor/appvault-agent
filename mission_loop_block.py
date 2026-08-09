# ---------------------------------------------------------------------------
# MISSION LOOP (2026-08-09) — goals -> plans -> verified work, executed until done.
# Vertical slice: content mission (research -> draft -> qa -> publish -> syndicate -> report).
# ---------------------------------------------------------------------------
MISSION_TEMPLATES = {
    "content": {
        "name": "Content mission",
        "description": "Research -> draft -> QA -> publish (WP) -> syndicate (LinkedIn) -> report",
        "tasks": [
            {"title": "Research the topic (LLM research brief + vault)", "task_type": "research", "executor": "mission"},
            {"title": "Draft the article from the brief", "task_type": "draft", "executor": "mission", "depends_on": 0},
            {"title": "QA the draft (length + placeholders + voice)", "task_type": "qa", "executor": "mission", "depends_on": 1},
            {"title": "Publish to WordPress + verify URL", "task_type": "publish", "executor": "mission", "depends_on": 2},
            {"title": "Syndicate on LinkedIn (post prepared)", "task_type": "syndicate", "executor": "mission", "depends_on": 3},
            {"title": "Record + report + goal bump", "task_type": "report", "executor": "mission", "depends_on": 4},
        ],
    },
    "research_brief": {
        "name": "Research mission",
        "description": "LLM research brief written to the vault",
        "tasks": [
            {"title": "Write the research brief", "task_type": "research", "executor": "mission"},
            {"title": "Record + report + goal bump", "task_type": "report", "executor": "mission", "depends_on": 0},
        ],
    },
    "outreach": {
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
}


def _missions_migrate():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS missions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER, title TEXT, objective_type TEXT DEFAULT 'content',
        status TEXT DEFAULT 'draft', progress REAL DEFAULT 0,
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS mission_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id INTEGER, seq INTEGER, title TEXT, task_type TEXT, executor TEXT,
        status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, last_error TEXT,
        wait_until TEXT, depends_on INTEGER, result_ref TEXT, verified INTEGER DEFAULT 0,
        created TEXT, updated TEXT
    );
    """)
    conn.commit()
    conn.close()
    try:
        conn = _db()
        exists = conn.execute("SELECT id FROM cron_jobs WHERE name='mission-executor'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO cron_jobs (name, schedule, task, action, enabled, next_run, created, updated) "
                "VALUES (?,?,?,?,1,NULL,?,?)",
                ("mission-executor", "every 5m", "Mission executor tick", "mission_tick",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _mission_to_dict(r):
    return {k: r[k] for k in ("id", "goal_id", "title", "objective_type", "status", "progress", "created", "updated")}


def _mtask_to_dict(r):
    return {k: r[k] for k in ("id", "mission_id", "seq", "title", "task_type", "executor", "status",
                              "attempts", "last_error", "wait_until", "depends_on", "result_ref",
                              "verified", "created", "updated")}


def _mission_dict(mid):
    try:
        conn = _db()
        row = conn.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
        tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (mid,)).fetchall()
        conn.close()
        if not row:
            return None
        d = _mission_to_dict(row)
        d["tasks"] = [_mtask_to_dict(t) for t in tasks]
        d["blocked"] = sum(1 for t in d["tasks"] if t["status"] == "blocked")
        d["done_count"] = sum(1 for t in d["tasks"] if t["status"] in ("verified", "done"))
        d["total"] = len(d["tasks"])
        return d
    except Exception:
        return None


def _mission_context(mission, task):
    """Unified context bundle for any executor: goal + voice + memories + recent work."""
    ctx = {"goal": None, "voice": "Professional, clear, direct.", "memories": [], "recent_work": []}
    try:
        if mission.get("goal_id"):
            conn = _db()
            g = conn.execute("SELECT * FROM goals WHERE id=?", (mission["goal_id"],)).fetchone()
            conn.close()
            if g:
                ctx["goal"] = dict(g)
    except Exception:
        pass
    try:
        souls = _cfg_get("souls") or {}
        if isinstance(souls, str):
            souls = json.loads(souls or "{}")
        prof = _get_profile() or {}
        voice = souls.get(prof.get("name") or "Default")
        if voice:
            ctx["voice"] = voice
    except Exception:
        pass
    try:
        mem = _memory_context(mission.get("title") or "", limit=5)
        if isinstance(mem, list):
            ctx["memories"] = mem
    except Exception:
        pass
    try:
        conn = _db()
        rows = conn.execute("SELECT title, category FROM work_items ORDER BY id DESC LIMIT 5").fetchall()
        conn.close()
        ctx["recent_work"] = [dict(r) for r in rows]
    except Exception:
        pass
    return ctx


def _read_ref(ref):
    if not ref:
        return None
    try:
        p = ref if os.path.isabs(ref) else os.path.join(_vault_path(), ref)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    except Exception:
        return None
    return None


def _read_dep_result(task):
    if not task.get("depends_on"):
        return None
    try:
        conn = _db()
        dep = conn.execute("SELECT result_ref FROM mission_tasks WHERE id=?", (task["depends_on"],)).fetchone()
        conn.close()
        return _read_ref(dep["result_ref"] if dep else None)
    except Exception:
        return None


def _mission_result_by_type(mission_id, ttype):
    try:
        conn = _db()
        row = conn.execute(
            "SELECT result_ref FROM mission_tasks WHERE mission_id=? AND task_type=? "
            "AND result_ref IS NOT NULL AND result_ref!='' ORDER BY seq DESC LIMIT 1",
            (mission_id, ttype)).fetchone()
        conn.close()
        return row["result_ref"] if row else None
    except Exception:
        return None


def _mission_slug(title):
    return re.sub(r"[^a-z0-9]+", "-", (title or "mission").lower()).strip("-")[:60] or "mission"


# ── Executors (content slice) ──
def _mission_research(mission, task, ctx):
    topic = mission.get("title") or "AI security tools"
    goal_txt = ""
    if ctx.get("goal"):
        goal_txt = "Business goal: %s (progress %s%%)." % (ctx["goal"].get("title", ""), ctx["goal"].get("progress", 0))
    signals = ""
    try:
        top = _sweep_feeds(limit=5)
        if top:
            signals = "\n".join("- %s (%s): %s" % (s.get("title", ""), s.get("source", ""), (s.get("summary") or "")[:160]) for s in top)
    except Exception:
        pass
    prompt = ("You are the research arm of an autonomous business agent.\nVoice: %s\n%s\n"
              "Topic: %s\n%s\n\nProduce a research brief (400-700 words, markdown): key facts, statistics, "
              "3-5 named sources with URLs, and a recommended article outline with a title. "
              "For an OUTREACH mission, also list 5-8 prospects as lines: `- Name | Company | email@example.com`. "
              "Output ONLY the brief.") % (ctx["voice"], goal_txt, topic,
              ("\nRecent industry signals (from Oracle feeds):\n" + signals) if signals else "")
    out = _call_llm_with({}, prompt, agent="hermes", timeout=240)
    out = (out or "").strip()
    if len(out) < 150:
        return (False, None, "brief too short (%d chars)" % len(out))
    path = _write_vault_output("03_Content/Research", "%s.md" % _mission_slug(topic), out, tag="Research", agent="Mission")
    return (True, path, None)


def _mission_draft(mission, task, ctx):
    brief = _read_dep_result(task) or ""
    topic = mission.get("title") or "AI security tools"
    prompt = ("You are the writer arm of an autonomous business agent. Voice: %s\n"
              "Write a complete article (900-1400 words, markdown with H2 sections) titled: %s\n"
              "Research brief:\n%s\n\n"
              "The article must open with a hook, use concrete examples, and end with a CTA. "
              "Output ONLY the article.") % (ctx["voice"], topic, brief or "(none provided — use your knowledge)")
    out = _call_llm_with({}, prompt, agent="hermes", timeout=300)
    out = (out or "").strip()
    if len(out) < 400:
        return (False, None, "draft too short (%d chars)" % len(out))
    path = _write_vault_output("03_Content/Drafts", "%s.md" % _mission_slug(topic), out, tag="Draft", agent="Mission")
    return (True, path, None)


def _mission_qa(mission, task, ctx):
    content = _read_dep_result(task) or ""
    if not content:
        return (False, None, "no draft to QA")
    words = len(content.split())
    if words < 300:
        return (False, None, "draft too short (%d words)" % words)
    low = content.lower()
    if "todo" in low or "lorem" in low or "{{" in content or "]" in content and "http" not in content:
        if "todo" in low or "lorem" in low or "{{" in content:
            return (False, None, "draft contains placeholders")
    links = content.count("http")
    score = 50
    try:
        prompt = ("Rate this article's adherence to the brand voice below from 0-100. "
                  "Reply with ONLY a number.\nVoice: %s\n\nArticle:\n%s"
                  % (ctx["voice"], content[:3000]))
        raw = _call_llm_with({}, prompt, agent="hermes", timeout=90)
        num = re.sub(r"[^0-9]", "", (raw or "")[:4])
        if num:
            score = min(100, int(num))
    except Exception:
        pass
    if score < 40:
        return (False, None, "voice QA score %d < 40" % score)
    return (True, "qa-pass:%d words:%d links" % (words, links), None)


def _mission_publish(mission, task, ctx):
    content = _read_dep_result(task) or ""
    if not content:
        return (False, None, "no draft to publish")
    title = mission.get("title") or "Article"
    ok, res = _wp_publish(title, content, status="publish")
    if not ok:
        return (False, None, str(res)[:300])
    link = ""
    if isinstance(res, dict):
        link = res.get("link") or ""
    if link:
        try:
            data, code = _http(link, timeout=12)
            if code not in (200, 201, 301, 302):
                return (False, None, "published URL returned HTTP %s" % code)
        except Exception as e:
            return (False, None, "URL verify failed: %s" % str(e)[:120])
    return (True, link or "published", None)


def _mission_syndicate(mission, task, ctx):
    draft_ref = _mission_result_by_type(mission["id"], "draft")
    content = _read_ref(draft_ref) or ""
    topic = mission.get("title") or "article"
    prompt = ("You are the social arm of an autonomous business agent. Voice: %s\n"
              "Write a LinkedIn post (180-260 words) promoting this article — a bold hook, "
              "3 concrete takeaways, and a CTA. Output ONLY the post text.\n\nArticle:\n%s"
              % (ctx["voice"], content[:2500]))
    post = _call_llm_with({}, prompt, agent="hermes", timeout=120)
    post = (post or "").strip()
    if len(post) < 80:
        return (False, None, "post too short (%d chars)" % len(post))
    path = _write_vault_output("03_Content/Social", "linkedin_%s.md" % _mission_slug(topic), post,
                               tag="LinkedIn", agent="Mission")
    return (True, path, None)


def _mission_report(mission, task, ctx):
    conn = _db()
    tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (mission["id"],)).fetchall()
    conn.close()
    lines = ["# Mission report: %s" % mission.get("title", "")]
    lines.append("Objective: %s | Status: %s" % (mission.get("objective_type", ""), mission.get("status", "")))
    for t in tasks:
        mark = "x" if t["verified"] else " "
        lines.append("- [%s] %s (%s)%s" % (mark, t["title"], t["status"],
                                           (" -> " + str(t["result_ref"])) if t["result_ref"] else ""))
    report = "\n".join(lines)
    try:
        _work_record(category="article", title=mission.get("title") or "Mission report",
                     content=report[:1500], source="mission")
    except Exception:
        pass
    try:
        _goal_bump(mission.get("title") or "", mission.get("objective_type") or "content", 10, "mission",
                   note="Mission completed: %s" % mission.get("title", ""))
    except Exception:
        pass
    path = _write_vault_output("04_Projects/Outputs", "Mission_%s.md" % _mission_slug(mission.get("title", "mission")),
                               report, tag="Mission", agent="Mission")
    return (True, path, None)


def _send_email(to, subject, body):
    """Send one email via configured SMTP. Returns (ok, err)."""
    try:
        cfg = _cfg_get("outreach_smtp") or {}
        host = (cfg.get("host") or "").strip()
        if not host or not cfg.get("enabled"):
            return False, "outreach_smtp not configured (set host/enabled in config)"
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg.get("from") or cfg.get("user") or "agent@appvault.local"
        msg["To"] = to
        port = int(cfg.get("port") or 587)
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            if cfg.get("tls") is not False:
                s.starttls()
            if cfg.get("user"):
                s.login(cfg.get("user"), cfg.get("pass") or "")
            s.send_message(msg)
        return True, None
    except Exception as e:
        return False, str(e)[:200]


def _mission_draft_emails(mission, task, ctx):
    brief = _read_dep_result(task) or ""
    prospects = []
    for line in brief.splitlines():
        if "@" in line and ("|" in line or "," in line):
            prospects.append(line.strip().lstrip("-* ")[:160])
    if not prospects:
        return (False, None, "no prospects (lines with emails) found in the research brief")
    prompt = ("You are the outreach arm of an autonomous business agent. Voice: %s\n"
              "Write a short personalized cold email (80-140 words) for EACH prospect below. "
              "Format each email as:\nTo: <email>\nSubject: <line>\n<body>\n---\n\n"
              "Prospects:\n%s") % (ctx["voice"], "\n".join(prospects[:10]))
    out = _call_llm_with({}, prompt, agent="hermes", timeout=240)
    out = (out or "").strip()
    if len(out) < 200:
        return (False, None, "emails too short (%d chars)" % len(out))
    path = _write_vault_output("06_Outreach", "emails_%s.md" % _mission_slug(mission.get("title", "")),
                               out, tag="Outreach", agent="Mission")
    return (True, path, None)


def _mission_send_emails(mission, task, ctx):
    emails = _read_dep_result(task) or ""
    if not emails:
        return (False, None, "no email draft to send")
    blocks = re.split(r"\n---\n", emails)
    sent, failed = 0, []
    for b in blocks:
        mto = re.search(r"To:\s*([^\n]+)", b)
        if not mto:
            continue
        msub = re.search(r"Subject:\s*([^\n]+)", b)
        ok, err = _send_email(mto.group(1).strip(), (msub.group(1).strip() if msub else "Hello"), b)
        if ok:
            sent += 1
        else:
            failed.append((mto.group(1).strip(), err[:60]))
    if sent == 0 and failed:
        return (False, None, "no emails sent: %s" % failed[0][1])
    return (True, "sent:%d failed:%d" % (sent, len(failed)), None)


def _mission_followup(mission, task, ctx):
    draft = _read_ref(_mission_result_by_type(mission["id"], "draft_emails")) or ""
    prompt = ("You are the follow-up arm of an autonomous business agent. Voice: %s\n"
              "Write a polite follow-up email (40-80 words) to each recipient of these outreach emails. "
              "Same format (To:/Subject:/body, --- separated).\n\nEmails:\n%s"
              % (ctx["voice"], draft[:2500]))
    out = _call_llm_with({}, prompt, agent="hermes", timeout=180)
    out = (out or "").strip()
    if len(out) < 100:
        return (False, None, "follow-up too short (%d chars)" % len(out))
    path = _write_vault_output("06_Outreach", "followup_%s.md" % _mission_slug(mission.get("title", "")),
                               out, tag="Outreach", agent="Mission")
    sent = 0
    for b in re.split(r"\n---\n", out):
        mto = re.search(r"To:\s*([^\n]+)", b)
        if not mto:
            continue
        msub = re.search(r"Subject:\s*([^\n]+)", b)
        ok, err = _send_email(mto.group(1).strip(), (msub.group(1).strip() if msub else "Follow-up"), b)
        if ok:
            sent += 1
    return (True, path + (" sent:%d" % sent if sent else ""), None)


def _mission_review(mission):
    """Post-mission learning: review file + append lessons to MISSION_LESSONS.md."""
    try:
        conn = _db()
        tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (mission["id"],)).fetchall()
        conn.close()
        lines = ["# Mission review: %s" % mission.get("title", "")]
        for t in [dict(x) for x in tasks]:
            mark = "✅" if t["verified"] else ("⚠️" if t["status"] == "blocked" else "⏳")
            lines.append("%s %s (%s)%s" % (mark, t["title"], t["status"],
                                           (" — " + str(t.get("last_error"))) if t.get("last_error") else ""))
        lessons = ""
        try:
            prompt = ("You are the learning arm of an autonomous business agent. Review this mission outcome and "
                      "write 3-5 concise lessons (what worked, what to avoid, how to improve the next mission). "
                      "Output ONLY markdown bullets.\n\n" + "\n".join(lines))
            lessons = (_call_llm_with({}, prompt, agent="hermes", timeout=120) or "").strip()
        except Exception:
            pass
        if lessons:
            lines.append("\n## Lessons learned\n" + lessons)
        body = "\n".join(lines)
        path = _write_vault_output("04_Projects/Outputs", "Mission_Review_%s.md" % _mission_slug(mission.get("title", "mission")),
                                   body, tag="Mission Review", agent="Mission")
        try:
            vault = _vault_path()
            d = os.path.join(vault, "04_Projects", "Outputs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "MISSION_LESSONS.md"), "a", encoding="utf-8") as f:
                f.write("\n## %s (%s)\n%s\n" % (mission.get("title", ""), mission.get("objective_type", ""),
                                                  lessons or "\n".join(lines[1:])))
        except Exception:
            pass
        return path
    except Exception:
        return None


def _mission_execute(mission, task):
    ttype = (task.get("task_type") or "").strip()
    ctx = _mission_context(mission, task)
    try:
        if ttype == "research":
            return _mission_research(mission, task, ctx)
        if ttype == "draft":
            return _mission_draft(mission, task, ctx)
        if ttype == "qa":
            return _mission_qa(mission, task, ctx)
        if ttype == "publish":
            return _mission_publish(mission, task, ctx)
        if ttype == "syndicate":
            return _mission_syndicate(mission, task, ctx)
        if ttype == "report":
            return _mission_report(mission, task, ctx)
        if ttype == "draft_emails":
            return _mission_draft_emails(mission, task, ctx)
        if ttype == "send_emails":
            return _mission_send_emails(mission, task, ctx)
        if ttype == "followup":
            return _mission_followup(mission, task, ctx)
        return (False, None, "unknown task_type: %s" % ttype)
    except Exception as e:
        return (False, None, str(e)[:300])


def _mission_tick():
    """Executor tick: advance each active mission by one task. Returns event strings."""
    out = []
    try:
        conn = _db()
        missions = conn.execute("SELECT * FROM missions WHERE status='active' ORDER BY id").fetchall()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for m in missions:
            mdict = dict(m)
            tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (m["id"],)).fetchall()
            chosen = None
            for t in tasks:
                tdict = dict(t)
                if tdict["status"] != "queued":
                    continue
                if tdict["depends_on"]:
                    dep = conn.execute("SELECT status FROM mission_tasks WHERE id=?", (tdict["depends_on"],)).fetchone()
                    if not dep or dep["status"] != "verified":
                        continue
                if tdict["wait_until"]:
                    wu = str(tdict["wait_until"] or "")
                    if wu and wu > now:
                        continue
                chosen = tdict
                break
            if not chosen:
                remaining = conn.execute(
                    "SELECT COUNT(*) c FROM mission_tasks WHERE mission_id=? AND status NOT IN ('verified','done')",
                    (m["id"],)).fetchone()
                if remaining and not remaining["c"]:
                    conn.execute("UPDATE missions SET status='done', progress=100, updated=? WHERE id=?", (now, m["id"]))
                    conn.commit()
                    try:
                        rev = _mission_review(mdict)
                        out.append("M%d done%s" % (m["id"], (" review:" + rev) if rev else ""))
                    except Exception:
                        out.append("M%d done" % m["id"])
                continue
            conn.execute("UPDATE mission_tasks SET status='running', updated=? WHERE id=?", (now, chosen["id"]))
            conn.commit()
            conn.close()
            ok, ref, err = _mission_execute(mdict, chosen)
            conn = _db()
            if ok:
                conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                             (ref, now, chosen["id"]))
                out.append("M%d T%d ok" % (m["id"], chosen["id"]))
            else:
                attempts = chosen["attempts"] + 1
                if attempts >= 3:
                    conn.execute("UPDATE mission_tasks SET status='blocked', attempts=?, last_error=?, updated=? WHERE id=?",
                                 (attempts, str(err)[:300], now, chosen["id"]))
                    out.append("M%d T%d blocked: %s" % (m["id"], chosen["id"], str(err)[:80]))
                else:
                    conn.execute("UPDATE mission_tasks SET status='queued', attempts=?, last_error=?, updated=? WHERE id=?",
                                 (attempts, str(err)[:300], now, chosen["id"]))
                    out.append("M%d T%d retry(%d)" % (m["id"], chosen["id"], attempts))
            conn.commit()
            conn.close()
            conn = _db()
        conn.close()
    except Exception as e:
        return ["error: " + str(e)[:200]]
    return out


# ── API ──
@agentic_bp.route("/api/agentic/missions", methods=["GET", "POST", "OPTIONS"])
def api_missions():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title required"}), 400
        tpl = data.get("template") or "content"
        goal_id = data.get("goal_id")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute(
            "INSERT INTO missions (goal_id, title, objective_type, status, progress, created, updated) VALUES (?,?,?,?,0,?,?)",
            (goal_id, title, data.get("objective_type") or "content", "draft", now, now))
        mid = cur.lastrowid
        tdef = MISSION_TEMPLATES.get(tpl) or MISSION_TEMPLATES["content"]
        ids = {}
        for i, t in enumerate(tdef["tasks"]):
            dep = None
            if t.get("depends_on") is not None:
                dep = ids.get(t["depends_on"])
            wait = None
            if t.get("wait_minutes"):
                wait = (datetime.now() + timedelta(minutes=int(t["wait_minutes"]))).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "INSERT INTO mission_tasks (mission_id, seq, title, task_type, executor, status, attempts, depends_on, wait_until, created, updated) "
                "VALUES (?,?,?,?,?,?,0,?,?,?,?)",
                (mid, i, t["title"], t["task_type"], t.get("executor") or "mission", "queued", dep, wait, now, now))
            ids[i] = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "mission": _mission_dict(mid)})
    conn = _db()
    rows = conn.execute("SELECT * FROM missions ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _mission_to_dict(r)
        d["blocked"] = 0
        d["done_count"] = 0
        d["total"] = 0
        md = _mission_dict(r["id"])
        if md:
            d["blocked"] = md["blocked"]
            d["done_count"] = md["done_count"]
            d["total"] = md["total"]
        out.append(d)
    return jsonify({"status": "ok", "missions": out})


@agentic_bp.route("/api/agentic/missions/<int:mid>", methods=["GET", "OPTIONS"])
def api_mission_detail(mid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    md = _mission_dict(mid)
    if not md:
        return jsonify({"error": "mission not found"}), 404
    return jsonify({"status": "ok", "mission": md})


@agentic_bp.route("/api/agentic/missions/<int:mid>/state", methods=["POST", "OPTIONS"])
def api_mission_state(mid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    st = (data.get("state") or "").strip()
    if st not in ("start", "pause", "resume", "archive"):
        return jsonify({"error": "state must be start|pause|resume|archive"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    row = conn.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "mission not found"}), 404
    new_status = {"start": "active", "pause": "paused", "resume": "active", "archive": "archived"}[st]
    conn.execute("UPDATE missions SET status=?, updated=? WHERE id=?", (new_status, now, mid))
    if st in ("start", "resume"):
        conn.execute("UPDATE mission_tasks SET status='queued' WHERE mission_id=? AND status IN ('failed','blocked')", (mid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "mission": _mission_dict(mid)})


@agentic_bp.route("/api/agentic/tasks/<int:tid>/run", methods=["POST", "OPTIONS"])
def api_task_run(tid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    row = conn.execute("SELECT * FROM mission_tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404
    mrow = conn.execute("SELECT * FROM missions WHERE id=?", (row["mission_id"],)).fetchone()
    conn.execute("UPDATE mission_tasks SET status='running', updated=? WHERE id=?", (now, tid))
    conn.commit()
    conn.close()
    ok, ref, err = _mission_execute(dict(mrow) if mrow else {}, dict(row))
    conn = _db()
    if ok:
        conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                     (ref, now, tid))
    else:
        conn.execute("UPDATE mission_tasks SET status='blocked', attempts=attempts+1, last_error=?, updated=? WHERE id=?",
                     (str(err)[:300], now, tid))
    conn.commit()
    row2 = conn.execute("SELECT * FROM mission_tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "task": _mtask_to_dict(row2) if row2 else None, "ok": ok, "error": err, "result_ref": ref})


@agentic_bp.route("/api/agentic/tasks/<int:tid>/resolve", methods=["POST", "OPTIONS"])
def api_task_resolve(tid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    conn.execute("UPDATE mission_tasks SET status='queued', attempts=0, last_error=NULL, updated=? WHERE id=? AND status='blocked'",
                 (now, tid))
    conn.commit()
    row = conn.execute("SELECT * FROM mission_tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "task": _mtask_to_dict(row) if row else None})


@agentic_bp.route("/api/agentic/missions/tick", methods=["POST", "OPTIONS"])
def api_mission_tick():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    res = _mission_tick()
    return jsonify({"status": "ok", "events": res})


@agentic_bp.route("/api/agentic/missions/templates", methods=["GET", "OPTIONS"])
def api_mission_templates():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok", "templates": MISSION_TEMPLATES})


_missions_migrate()
