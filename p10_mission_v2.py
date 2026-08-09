#!/usr/bin/env python3
"""Mission Loop v2 additions: oracle signals in research, outreach template,
SMTP sender, post-mission review/lessons, wait_minutes in task creation."""
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

    # 1. outreach template (after research_brief template)
    rep("""    "research_brief": {
        "name": "Research mission",
        "description": "LLM research brief written to the vault",
        "tasks": [
            {"title": "Write the research brief", "task_type": "research", "executor": "mission"},
            {"title": "Record + report + goal bump", "task_type": "report", "executor": "mission", "depends_on": 0},
        ],
    },
}""",
        """    "research_brief": {
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
}""")

    # 2. research: prepend Oracle feed signals
    rep("""def _mission_research(mission, task, ctx):
    topic = mission.get("title") or "AI security tools"
    goal_txt = ""
    if ctx.get("goal"):
        goal_txt = "Business goal: %s (progress %s%%)." % (ctx["goal"].get("title", ""), ctx["goal"].get("progress", 0))
    prompt = ("You are the research arm of an autonomous business agent.\\nVoice: %s\\n%s\\n"
              "Topic: %s\\n\\nProduce a research brief (400-700 words, markdown): key facts, statistics, "
              "3-5 named sources with URLs, and a recommended article outline with a title. "
              "Output ONLY the brief.") % (ctx["voice"], goal_txt, topic)""",
        """def _mission_research(mission, task, ctx):
    topic = mission.get("title") or "AI security tools"
    goal_txt = ""
    if ctx.get("goal"):
        goal_txt = "Business goal: %s (progress %s%%)." % (ctx["goal"].get("title", ""), ctx["goal"].get("progress", 0))
    signals = ""
    try:
        top = _sweep_feeds(limit=5)
        if top:
            signals = "\\n".join("- %s (%s): %s" % (s.get("title", ""), s.get("source", ""), (s.get("summary") or "")[:160]) for s in top)
    except Exception:
        pass
    prompt = ("You are the research arm of an autonomous business agent.\\nVoice: %s\\n%s\\n"
              "Topic: %s\\n%s\\n\\nProduce a research brief (400-700 words, markdown): key facts, statistics, "
              "3-5 named sources with URLs, and a recommended article outline with a title. "
              "For an OUTREACH mission, also list 5-8 prospects as lines: `- Name | Company | email@example.com`. "
              "Output ONLY the brief.") % (ctx["voice"], goal_txt, topic,
              ("\\nRecent industry signals (from Oracle feeds):\\n" + signals) if signals else "")""")

    # 3. SMTP helper + new executors (before _mission_execute)
    rep("""def _mission_execute(mission, task):""",
        """def _send_email(to, subject, body):
    \"\"\"Send one email via configured SMTP. Returns (ok, err).\"\"\"
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
    prompt = ("You are the outreach arm of an autonomous business agent. Voice: %s\\n"
              "Write a short personalized cold email (80-140 words) for EACH prospect below. "
              "Format each email as:\\nTo: <email>\\nSubject: <line>\\n<body>\\n---\\n\\n"
              "Prospects:\\n%s") % (ctx["voice"], "\\n".join(prospects[:10]))
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
    blocks = re.split(r"\\n---\\n", emails)
    sent, failed = 0, []
    for b in blocks:
        mto = re.search(r"To:\\s*([^\\n]+)", b)
        if not mto:
            continue
        msub = re.search(r"Subject:\\s*([^\\n]+)", b)
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
    prompt = ("You are the follow-up arm of an autonomous business agent. Voice: %s\\n"
              "Write a polite follow-up email (40-80 words) to each recipient of these outreach emails. "
              "Same format (To:/Subject:/body, --- separated).\\n\\nEmails:\\n%s"
              % (ctx["voice"], draft[:2500]))
    out = _call_llm_with({}, prompt, agent="hermes", timeout=180)
    out = (out or "").strip()
    if len(out) < 100:
        return (False, None, "follow-up too short (%d chars)" % len(out))
    path = _write_vault_output("06_Outreach", "followup_%s.md" % _mission_slug(mission.get("title", "")),
                               out, tag="Outreach", agent="Mission")
    sent = 0
    for b in re.split(r"\\n---\\n", out):
        mto = re.search(r"To:\\s*([^\\n]+)", b)
        if not mto:
            continue
        msub = re.search(r"Subject:\\s*([^\\n]+)", b)
        ok, err = _send_email(mto.group(1).strip(), (msub.group(1).strip() if msub else "Follow-up"), b)
        if ok:
            sent += 1
    return (True, path + (" sent:%d" % sent if sent else ""), None)


def _mission_review(mission):
    \"\"\"Post-mission learning: review file + append lessons to MISSION_LESSONS.md.\"\"\"
    try:
        conn = _db()
        tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (mission["id"],)).fetchall()
        conn.close()
        lines = ["# Mission review: %s" % mission.get("title", "")]
        for t in tasks:
            mark = "✅" if t["verified"] else ("⚠️" if t["status"] == "blocked" else "⏳")
            lines.append("%s %s (%s)%s" % (mark, t["title"], t["status"],
                                           (" — " + str(t["last_error"])) if t.get("last_error") else ""))
        lessons = ""
        try:
            prompt = ("You are the learning arm of an autonomous business agent. Review this mission outcome and "
                      "write 3-5 concise lessons (what worked, what to avoid, how to improve the next mission). "
                      "Output ONLY markdown bullets.\\n\\n" + "\\n".join(lines))
            lessons = (_call_llm_with({}, prompt, agent="hermes", timeout=120) or "").strip()
        except Exception:
            pass
        if lessons:
            lines.append("\\n## Lessons learned\\n" + lessons)
        body = "\\n".join(lines)
        path = _write_vault_output("04_Projects/Outputs", "Mission_Review_%s.md" % _mission_slug(mission.get("title", "mission")),
                                   body, tag="Mission Review", agent="Mission")
        try:
            vault = _vault_path()
            d = os.path.join(vault, "04_Projects", "Outputs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "MISSION_LESSONS.md"), "a", encoding="utf-8") as f:
                f.write("\\n## %s (%s)\\n%s\\n" % (mission.get("title", ""), mission.get("objective_type", ""),
                                                  lessons or "\\n".join(lines[1:])))
        except Exception:
            pass
        return path
    except Exception:
        return None


def _mission_execute(mission, task):""")

    # 4. dispatch new task types
    rep("""        if ttype == "report":
            return _mission_report(mission, task, ctx)
        return (False, None, "unknown task_type: %s" % ttype)""",
        """        if ttype == "report":
            return _mission_report(mission, task, ctx)
        if ttype == "draft_emails":
            return _mission_draft_emails(mission, task, ctx)
        if ttype == "send_emails":
            return _mission_send_emails(mission, task, ctx)
        if ttype == "followup":
            return _mission_followup(mission, task, ctx)
        return (False, None, "unknown task_type: %s" % ttype)""")

    # 5. done branch -> post-mission review
    rep("""                if remaining and not remaining["c"]:
                    conn.execute("UPDATE missions SET status='done', progress=100, updated=? WHERE id=?", (now, m["id"]))
                    conn.commit()
                    out.append("M%d done" % m["id"])
                continue""",
        """                if remaining and not remaining["c"]:
                    conn.execute("UPDATE missions SET status='done', progress=100, updated=? WHERE id=?", (now, m["id"]))
                    conn.commit()
                    try:
                        rev = _mission_review(mdict)
                        out.append("M%d done%s" % (m["id"], (" review:" + rev) if rev else ""))
                    except Exception:
                        out.append("M%d done" % m["id"])
                continue""")

    # 6. wait_minutes support in task creation
    rep("""        ids = {}
        for i, t in enumerate(tdef["tasks"]):
            dep = None
            if t.get("depends_on") is not None:
                dep = ids.get(t["depends_on"])
            cur = conn.execute(
                "INSERT INTO mission_tasks (mission_id, seq, title, task_type, executor, status, attempts, depends_on, created, updated) "
                "VALUES (?,?,?,?,?,?,0,?,?,?)",
                (mid, i, t["title"], t["task_type"], t.get("executor") or "mission", "queued", dep, now, now))
            ids[i] = cur.lastrowid""",
        """        ids = {}
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
            ids[i] = cur.lastrowid""")

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    print('mission_loop_block.py v2 —', edits, 'edits')

if __name__ == '__main__':
    main()
