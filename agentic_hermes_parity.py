# =============================================================================
# HERMES PARITY LAYER (2026-08-08) — slash commands, user cron job manager,
# and multi-profile switching. Spliced into agentic_plane.py. stdlib-only.
# =============================================================================

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def _init_parity_tables():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cron_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, schedule TEXT, task TEXT, action TEXT,
        enabled INTEGER DEFAULT 1,
        next_run REAL, last_run TEXT, last_status TEXT, last_output TEXT,
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE, identity TEXT, created TEXT, updated TEXT
    );
    """)
    conn.commit()
    # seed Default profile from the legacy identity config (idempotent)
    n = conn.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
    if n == 0:
        legacy = _cfg_get("identity") or ""
        conn.execute("INSERT INTO profiles (name, identity, created, updated) VALUES (?,?,?,?)",
                     ("Default", legacy, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        _cfg_set("active_profile", "Default")
        conn.commit()
    conn.close()

_init_parity_tables()


# ---------------------------------------------------------------------------
# PROFILES — Hermes-style profile switching. Identity is per-profile.
# ---------------------------------------------------------------------------
def _active_profile_name():
    return (_cfg_get("active_profile") or "Default")


def _get_profile():
    """Profile-aware: reads the ACTIVE profile's identity (fallback legacy)."""
    conn = _db()
    row = conn.execute("SELECT identity FROM profiles WHERE name=?", (_active_profile_name(),)).fetchone()
    conn.close()
    raw = (row["identity"] if row else "") or (_cfg_get("identity") or "")
    try:
        prof = json.loads(raw) if raw else {}
    except Exception:
        prof = {}
    return {**IDENTITY_DEFAULTS, **{k: v for k, v in (prof or {}).items() if v is not None}}


def _set_profile(patch):
    """Update the ACTIVE profile's identity."""
    prof = _get_profile()
    for k, v in (patch or {}).items():
        if k in prof and v is not None:
            prof[k] = str(v).strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    row = conn.execute("SELECT id FROM profiles WHERE name=?", (_active_profile_name(),)).fetchone()
    if row:
        conn.execute("UPDATE profiles SET identity=?, updated=? WHERE id=?",
                     (json.dumps(prof), now, row["id"]))
    else:
        conn.execute("INSERT INTO profiles (name, identity, created, updated) VALUES (?,?,?,?)",
                     (_active_profile_name(), json.dumps(prof), now, now))
    conn.commit()
    conn.close()
    _mirror_profile_to_vault(prof)
    return prof


@agentic_bp.route("/api/agentic/profiles", methods=["GET", "POST", "OPTIONS"])
def api_profiles():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        try:
            cur = conn.execute("INSERT INTO profiles (name, identity, created, updated) VALUES (?,?,?,?)",
                               (name, json.dumps(_get_profile()), now, now))
        except Exception:
            conn.close()
            return jsonify({"error": f"profile '{name}' already exists"}), 400
        conn.commit()
        conn.close()
        _cfg_set("active_profile", name)
        _audit("store", "profile.create", f"profile '{name}' created + activated")
        return jsonify({"status": "ok", "id": cur.lastrowid, "active": name})
    conn = _db()
    rows = conn.execute("SELECT id, name, created, updated FROM profiles ORDER BY id").fetchall()
    conn.close()
    active = _active_profile_name()
    return jsonify({"status": "ok", "profiles": [dict(r) for r in rows], "active": active})


@agentic_bp.route("/api/agentic/profiles/<int:pid>/switch", methods=["POST", "OPTIONS"])
def api_profile_switch(pid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT name FROM profiles WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "profile not found"}), 404
    _cfg_set("active_profile", row["name"])
    _audit("store", "profile.switch", f"active -> '{row['name']}'")
    return jsonify({"status": "ok", "active": row["name"]})


@agentic_bp.route("/api/agentic/profiles/<int:pid>", methods=["PUT", "DELETE", "OPTIONS"])
def api_profile(pid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "profile not found"}), 404
    if request.method == "DELETE":
        if row["name"] == _active_profile_name():
            conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
            conn.execute("UPDATE profiles SET name='Default' WHERE name='Default'")
            other = conn.execute("SELECT name FROM profiles ORDER BY id LIMIT 1").fetchone()
            target = other["name"] if other else "Default"
            conn.commit()
            conn.close()
            _cfg_set("active_profile", target)
            _audit("store", "profile.delete", f"deleted '{row['name']}' -> active '{target}'")
            return jsonify({"status": "ok", "deleted": row["name"], "active": target})
        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        _audit("store", "profile.delete", f"deleted '{row['name']}'")
        return jsonify({"status": "ok", "deleted": row["name"]})
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if name and name != row["name"]:
        try:
            conn.execute("UPDATE profiles SET name=?, updated=? WHERE id=?",
                         (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), pid))
            conn.commit()
        except Exception:
            conn.close()
            return jsonify({"error": "name taken"}), 400
        if _active_profile_name() == row["name"]:
            _cfg_set("active_profile", name)
    conn.close()
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# CRON JOB MANAGER — user-created scheduled jobs (LLM tasks or built-in
# actions). Scheduler thread ticks every 30s.
# ---------------------------------------------------------------------------
def _parse_schedule(schedule):
    """Parse a schedule string -> next-run epoch (or None if invalid).
    Accepts: hourly | daily | weekly | every <N>s|m|h | HH:MM | mon:HH:MM …"""
    s = (schedule or "").strip().lower()
    now = time.time()
    if s == "hourly":
        return now + 3600
    if s == "daily":
        return now + 86400
    if s == "weekly":
        return now + 604800
    m = re.match(r"^every\s+(\d+)\s*(s|m|h)$", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        return now + int(m.group(1)) * mult
    m = re.match(r"^(mon|tue|wed|thu|fri|sat|sun):(\d{1,2}):(\d{2})$", s)
    if m:
        import calendar as _cal
        days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        target = days[m.group(1)]
        hh, mm = int(m.group(2)), int(m.group(3))
        t = time.localtime(now)
        cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))
        delta = (target - t.tm_wday) % 7
        if delta == 0 and cand <= now:
            delta = 7
        return cand + delta * 86400
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        t = time.localtime(now)
        cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))
        if cand <= now:
            cand += 86400
        return cand
    return None


CRON_ACTIONS = {
    "digest": "📬 Generate today's intelligence digest (sweep-all + write)",
    "capture": "📔 Write today's daily log (OMI-style capture)",
    "self_improve": "🔁 Run the self-improvement pass (from negative feedback)",
}


def _run_cron_job(job):
    """Execute one job. Returns (status, output)."""
    action = (job.get("action") or "").strip()
    name = job.get("name") or "Cron Job"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if action == "digest":
        try:
            _run_digest_now()
            return "ok", "digest written to 00_Intelligence"
        except Exception as e:
            return "error", str(e)[:200]
    if action == "capture":
        try:
            res = _run_daily_capture(force=True)
            return "ok", str(res.get("file", "capture done"))
        except Exception as e:
            return "error", str(e)[:200]
    if action == "self_improve":
        try:
            res = _run_self_improvement(force=True)
            return "ok", f"{res.get('proposals', 0)} proposals"
        except Exception as e:
            return "error", str(e)[:200]
    # default: LLM prompt task
    task = (job.get("task") or "").strip()
    if not task:
        return "error", "no task (set a prompt or pick an action)"
    try:
        out = _call_llm_with({}, task, agent="hermes", timeout=180)
        return "ok", (out or "")[:500]
    except Exception as e:
        return "error", str(e)[:200]


def _complete_cron_job(job, status, output):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    conn.execute("UPDATE cron_jobs SET last_run=?, last_status=?, last_output=?, next_run=?, updated=? WHERE id=?",
                 (now, status, (output or "")[:800], _parse_schedule(job.get("schedule")) or (time.time() + 86400),
                  now, job["id"]))
    conn.commit()
    conn.close()
    # compounding loop: every cron result lands in memory + vault
    try:
        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) VALUES (?,?,?,?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), f"Cron: {job.get('name')}", "Cron Job",
                      f"Cron '{job.get('name')}' -> {status}: {(output or '')[:250]}", "auto", "cron", now))
        conn.commit()
        conn.close()
    except Exception:
        pass
    vault = _vault_path()
    d = os.path.join(vault, "02_Agent_Logs", "Cron")
    try:
        os.makedirs(d, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", (job.get("name") or "job").lower()).strip("-")[:40]
        with open(os.path.join(d, f"{slug}_{job['id']}_{datetime.now().strftime('%Y%m%d')}.md"),
                  "w", encoding="utf-8") as f:
            f.write(f"# Cron: {job.get('name')}\n\n**Schedule:** {job.get('schedule')} · **Ran:** {now} · "
                    f"**Status:** {status}\n\n{output}\n")
    except Exception:
        pass


_CRON_RUNNING = {}
_CRON_LOCK = threading.Lock()


def _cron_tick():
    conn = _db()
    rows = conn.execute("SELECT * FROM cron_jobs WHERE enabled=1 AND (next_run IS NULL OR next_run <= ?)",
                        (time.time(),)).fetchall()
    conn.close()
    for r in rows:
        jid = r["id"]
        with _CRON_LOCK:
            if _CRON_RUNNING.get(jid):
                continue
            _CRON_RUNNING[jid] = True
        job = dict(r)
        def _one(j=job):
            try:
                status, output = _run_cron_job(j)
                _complete_cron_job(j, status, output)
            except Exception:
                try:
                    _complete_cron_job(j, "error", "internal error")
                except Exception:
                    pass
            finally:
                with _CRON_LOCK:
                    _CRON_RUNNING.pop(j["id"], None)
        threading.Thread(target=_one, daemon=True).start()


def _cron_loop():
    while True:
        try:
            _cron_tick()
        except Exception:
            pass
        time.sleep(30)


def _start_cron():
    threading.Thread(target=_cron_loop, daemon=True).start()


@agentic_bp.route("/api/agentic/cron", methods=["GET", "POST", "OPTIONS"])
def api_cron():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        schedule = (data.get("schedule") or "").strip()
        if not name or not schedule:
            return jsonify({"error": "name + schedule required (hourly | daily | weekly | every 30m | HH:MM | mon:09:00)"}), 400
        nxt = _parse_schedule(schedule)
        if not nxt:
            return jsonify({"error": f"cannot parse schedule '{schedule}'"}), 400
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute(
            "INSERT INTO cron_jobs (name, schedule, task, action, enabled, next_run, created, updated) "
            "VALUES (?,?,?,?,1,?,?,?)",
            (name, schedule, (data.get("task") or ""), (data.get("action") or ""), nxt, now, now))
        conn.commit()
        conn.close()
        _audit("store", "cron.create", f"job '{name}' every '{schedule}'")
        return jsonify({"status": "ok", "id": cur.lastrowid, "next_run": nxt})
    conn = _db()
    rows = conn.execute("SELECT * FROM cron_jobs ORDER BY id DESC").fetchall()
    conn.close()
    jobs = []
    for r in rows:
        j = dict(r)
        j["next_run_human"] = datetime.fromtimestamp(j["next_run"]).strftime("%Y-%m-%d %H:%M") if j["next_run"] else None
        jobs.append(j)
    return jsonify({"status": "ok", "jobs": jobs, "actions": CRON_ACTIONS})


@agentic_bp.route("/api/agentic/cron/<int:cid>", methods=["PUT", "DELETE", "OPTIONS"])
def api_cron_job(cid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM cron_jobs WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "job not found"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM cron_jobs WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        _audit("store", "cron.delete", f"job #{cid} deleted")
        return jsonify({"status": "ok", "deleted": cid})
    data = request.get_json() or {}
    merged = dict(row)
    for k in ("name", "schedule", "task", "action"):
        if data.get(k) is not None:
            merged[k] = data[k]
    if data.get("enabled") is not None:
        merged["enabled"] = 1 if data["enabled"] else 0
    nxt = _parse_schedule(merged["schedule"]) or (row["next_run"] or time.time() + 86400)
    conn.execute("UPDATE cron_jobs SET name=?, schedule=?, task=?, action=?, enabled=?, next_run=?, updated=? WHERE id=?",
                 (merged["name"], merged["schedule"], merged["task"], merged["action"],
                  merged["enabled"], nxt, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "next_run": nxt})


@agentic_bp.route("/api/agentic/cron/<int:cid>/run", methods=["POST", "OPTIONS"])
def api_cron_run(cid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM cron_jobs WHERE id=?", (cid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "job not found"}), 404
    status, output = _run_cron_job(dict(row))
    _complete_cron_job(dict(row), status, output)
    _audit("store", "cron.run", f"job #{cid} '{row['name']}' -> {status}")
    return jsonify({"status": "ok" if status == "ok" else "error", "status": status, "output": (output or "")[:500]})


# ---------------------------------------------------------------------------
# SLASH COMMANDS — Hermes-style / commands in every chat box.
# ---------------------------------------------------------------------------
def _slash_help():
    return ("**Hermes-style slash commands**\n"
            "- `/help` — this list\n"
            "- `/new [title]` — start a new session\n"
            "- `/sessions` — list sessions\n"
            "- `/skills [filter]` — list your skills (use `@name` to apply one)\n"
            "- `/cron` — list cron jobs · `/cron add <name>|<schedule>|<task>` · `/cron del <id>` · `/cron run <id>`\n"
            "- `/profile` — list profiles · `/profile new <name>` · `/profile switch <name>`\n"
            "- `/memory [n]` — recent shared memory\n"
            "- `/status` — agent fleet status\n"
            "- `/goals` — active goals\n"
            "Schedules: `hourly` `daily` `weekly` `every 30m` `HH:MM` `mon:09:00`")


def _slash_list_skills(filt=""):
    conn = _db()
    rows = conn.execute("SELECT * FROM skills ORDER BY uses DESC, updated DESC").fetchall()
    conn.close()
    f = filt.lower()
    rows = [r for r in rows if not f or f in (r["name"] or "").lower() or f in (r["description"] or "").lower()]
    if not rows:
        return "No skills yet — import some via 🔌 Import on the Agentic OS page."
    lines = [f"- **@{r['name']}** · {r['description'] or ''} · used {r['uses']}x"
             f"{' · ⚡ action' if r['kind'] == 'action' else ''}" for r in rows[:20]]
    return "**Your skills (" + str(len(rows)) + "):**\n" + "\n".join(lines)


def _slash_cron(args):
    parts = [p.strip() for p in args.split("|")]
    if parts[0] == "add" and len(parts) >= 3:
        name, schedule, task = parts[1], parts[2], parts[3] if len(parts) > 3 else ""
        nxt = _parse_schedule(schedule)
        if not nxt:
            return f"⚠️ Cannot parse schedule '{schedule}' — try hourly, daily, weekly, every 30m, 09:00, mon:09:00"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute("INSERT INTO cron_jobs (name, schedule, task, action, enabled, next_run, created, updated) "
                           "VALUES (?,?,?,?,1,?,?,?)", (name, schedule, task, "", nxt, now, now))
        conn.commit()
        conn.close()
        return f"✅ Cron job '{name}' created — next run {datetime.fromtimestamp(nxt).strftime('%Y-%m-%d %H:%M')} (see ⛅ Cron on the Gov page)"
    if parts[0] == "del" and len(parts) >= 2:
        try:
            cid = int(parts[1])
        except ValueError:
            return "⚠️ Usage: /cron del <id>"
        conn = _db()
        conn.execute("DELETE FROM cron_jobs WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        return f"🗑 Cron job #{cid} deleted"
    if parts[0] == "run" and len(parts) >= 2:
        try:
            cid = int(parts[1])
        except ValueError:
            return "⚠️ Usage: /cron run <id>"
        conn = _db()
        row = conn.execute("SELECT * FROM cron_jobs WHERE id=?", (cid,)).fetchone()
        conn.close()
        if not row:
            return "⚠️ Job not found"
        status, output = _run_cron_job(dict(row))
        _complete_cron_job(dict(row), status, output)
        return f"⛅ Job #{cid} '{row['name']}' → **{status}**: {(output or '')[:200]}"
    conn = _db()
    rows = conn.execute("SELECT * FROM cron_jobs ORDER BY id DESC LIMIT 12").fetchall()
    conn.close()
    if not rows:
        return ("No cron jobs yet. Add one in ⛅ Cron on the Gov page, or inline: "
                "`/cron add MyJob|daily|Summarize the vault notes`")
    lines = []
    for r in rows:
        lines.append(f"- #{r['id']} **{r['name']}** · {r['schedule']} · {'✅' if r['enabled'] else '⏸'} · "
                     f"last: {r['last_status'] or 'never'} ({r['last_run'] or '—'})")
    return "**Cron jobs:**\n" + "\n".join(lines)


def _slash_profiles(args):
    conn = _db()
    rows = conn.execute("SELECT id, name, created FROM profiles ORDER BY id").fetchall()
    active = _active_profile_name()
    if args.startswith("new "):
        name = args[4:].strip()
        if not name:
            return "⚠️ Usage: /profile new <name>"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.execute("INSERT INTO profiles (name, identity, created, updated) VALUES (?,?,?,?)",
                         (name, json.dumps(_get_profile()), now, now))
            conn.commit()
        except Exception:
            conn.close()
            return f"⚠️ Profile '{name}' already exists"
        conn.close()
        _cfg_set("active_profile", name)
        return f"✅ Profile '{name}' created + activated — the identity block now reflects it."
    if args.startswith("switch "):
        name = args[7:].strip()
        row = conn.execute("SELECT id FROM profiles WHERE name=?", (name,)).fetchone()
        conn.close()
        if not row:
            return f"⚠️ No profile named '{name}' — try /profile"
        _cfg_set("active_profile", name)
        return f"🔄 Switched to profile **'{name}'** — agents now know you as this identity."
    conn.close()
    lines = [f"- {'⭐' if r['name'] == active else '  '} **{r['name']}**" for r in rows]
    return ("**Profiles** (active marked ⭐):\n" + "\n".join(lines) +
            "\nUse `/profile new <name>` or `/profile switch <name>`. Edit identity on the 🧑 Identity page.")


def _handle_slash_command(msg, agent_id="hermes"):
    """Handle Hermes-style slash commands. Returns a reply string or None if
    the message is not a slash command."""
    msg = (msg or "").strip()
    if not msg.startswith("/"):
        return None
    parts = msg.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    if cmd == "/help":
        return _slash_help()
    if cmd == "/new":
        title = args.strip() or f"Hermes Session {datetime.now().strftime('%H:%M:%S')}"
        sid = f"session-{int(time.time()*1000)}"
        _save_session(sid, title, [])
        _audit("chat", "session.new", f"'{title}' ({sid})")
        return f"✅ New session **'{title}'** created ({sid}). Switch to it in the session dropdown."
    if cmd == "/sessions":
        sessions = _list_sessions()
        if not sessions:
            return "No sessions yet — type `/new` to create one."
        lines = [f"- {s['title']} · {s.get('message_count', 0)} msgs · {s['id']}" for s in sessions[:12]]
        return "**Sessions:**\n" + "\n".join(lines)
    if cmd == "/skills":
        return _slash_list_skills(args)
    if cmd == "/cron":
        return _slash_cron(args)
    if cmd == "/profile":
        return _slash_profiles(args)
    if cmd == "/memory":
        try:
            n = min(10, max(1, int(args or 5)))
        except ValueError:
            n = 5
        conn = _db()
        rows = conn.execute("SELECT * FROM memory WHERE superseded_by IS NULL ORDER BY id DESC LIMIT ?",
                            (n,)).fetchall()
        conn.close()
        if not rows:
            return "No memory entries yet."
        lines = [f"- [{r['tier']}] {r['content'][:140]}" for r in rows]
        return "**Recent shared memory:**\n" + "\n".join(lines)
    if cmd == "/status":
        try:
            probes = _probe_all()
            lines = [f"- **{name}** ({sid}): {probes.get(sid, {}).get('status', 'offline')}"
                     for sid, name, _, _, _, _, _ in SERVICES]
            return "**Fleet status:**\n" + "\n".join(lines)
        except Exception as e:
            return f"⚠️ status failed: {e}"
    if cmd == "/goals":
        goals = _goals_context(compact=False)
        return ("**Active goals:**\n" + goals) if goals else "No active goals — add some on the 🎯 Goals page."
    return (f"Unknown command `{cmd}`.\n\n" + _slash_help())


if os.environ.get("APPVAULT_CRON", "1") != "0":
    try:
        _start_cron()
    except Exception:
        pass
