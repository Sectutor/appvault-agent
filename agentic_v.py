# =============================================================================
# V LAYER (2026-08-08) — Vercel-V parity: channels · feedback & self-improvement
# · evals · router agent (V) · approvals & audit trail · event ingress ·
# per-agent souls · skill-tools + consortium. Spliced into agentic_plane.py.
# stdlib-only; every route has an OPTIONS guard as its FIRST line.
# =============================================================================

# ---------------------------------------------------------------------------
# Tables + migrations
# ---------------------------------------------------------------------------
def _init_v_tables():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, agent TEXT, rating INTEGER, comment TEXT, reply_preview TEXT
    );
    CREATE TABLE IF NOT EXISTS evals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, agent TEXT, prompt TEXT, expected_contains TEXT,
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS eval_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        eval_id INTEGER, ts TEXT, passed INTEGER, output TEXT, latency_ms INTEGER
    );
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, action TEXT, target TEXT, payload TEXT,
        status TEXT DEFAULT 'pending', requested_by TEXT, decided_at TEXT
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, actor TEXT, action TEXT, detail TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, event TEXT, source TEXT, payload TEXT
    );
    CREATE TABLE IF NOT EXISTS improvement_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, summary TEXT, details TEXT, status TEXT DEFAULT 'open',
        vault_file TEXT
    );
    """)
    try:
        conn.execute("ALTER TABLE skills ADD COLUMN tools TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    conn.close()

_init_v_tables()


def _audit(actor, action, detail):
    try:
        conn = _db()
        conn.execute("INSERT INTO audit_log (ts, actor, action, detail) VALUES (?,?,?,?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), actor, action, str(detail)[:500]))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. PER-AGENT SOULS — UI-editable instructions per roster agent.
#    (video: "it's not truly yours... a folder with an instructions.md")
# ---------------------------------------------------------------------------
def _get_agent_prompt(agent):
    """Soul override wins > built-in AGENT_PROMPTS > default."""
    try:
        raw = _cfg_get("souls") or ""
        souls = json.loads(raw) if raw else {}
        if souls.get(agent):
            return souls[agent]
    except Exception:
        pass
    return AGENT_PROMPTS.get(agent, DEFAULT_LLM_CONFIG["system_prompt"])


@agentic_bp.route("/api/agentic/souls", methods=["GET", "POST", "OPTIONS"])
def api_souls():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        raw = _cfg_get("souls") or ""
        try:
            souls = json.loads(raw) if raw else {}
        except Exception:
            souls = {}
        for agent, prompt in (data.get("souls") or {}).items():
            if prompt is not None:
                souls[agent] = str(prompt)
        _cfg_set("souls", json.dumps(souls))
        _audit("store", "souls.update", f"updated {len(data.get('souls') or {})} agent souls")
        return jsonify({"status": "ok", "souls": souls})
    raw = _cfg_get("souls") or ""
    try:
        souls = json.loads(raw) if raw else {}
    except Exception:
        souls = {}
    # include built-in prompts so the UI can show + edit everything
    all_souls = {agent: souls.get(agent, prompt) for agent, prompt in AGENT_PROMPTS.items()}
    return jsonify({"status": "ok", "souls": all_souls})


# ---------------------------------------------------------------------------
# 2. CHANNELS — Telegram bridge (long-poll, stdlib). The agent lives where the
#    team talks: your own "@V". Config stores the bot token; keyless fallback
#    = the store UI. Digest/capture delivery uses the same send helper.
# ---------------------------------------------------------------------------
def _telegram_cfg():
    raw = _cfg_get("channels") or ""
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _telegram_send(text, chat_id=None, token=None):
    """Send a message via the Telegram Bot API. Returns (ok, error)."""
    token = token or (_telegram_cfg().get("telegram", {}) or {}).get("bot_token") or ""
    chat_id = chat_id or (_telegram_cfg().get("telegram", {}) or {}).get("chat_id") or ""
    if not token or not chat_id:
        return False, "no token/chat_id configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data, status = _http(url, method="POST",
                         json_data={"chat_id": chat_id, "text": str(text)[:3800]}, timeout=20)
    return status == 200, (str(data)[:200] if status != 200 else "ok")


def _telegram_poll_once(token):
    """One getUpdates pass. Returns list of messages handled."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    data, status = _http(url, method="POST",
                         json_data={"timeout": 25, "offset": _TELEGRAM_OFFSET[0]}, timeout=35)
    if status != 200 or not isinstance(data, dict):
        return 0
    n = 0
    for upd in (data.get("result") or []):
        _TELEGRAM_OFFSET[0] = upd.get("update_id", 0) + 1
        msg = upd.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text or text.startswith("/"):
            continue
        try:
            reply = _router_reply(text, agent_id="v", source="telegram")
            _telegram_send(reply, chat_id=chat_id, token=token)
            n += 1
        except Exception:
            pass
    return n


_TELEGRAM_OFFSET = [0]

def _telegram_loop():
    while True:
        try:
            cfg = _telegram_cfg().get("telegram", {})
            token = (cfg or {}).get("bot_token") or ""
            if token:
                _telegram_poll_once(token)
        except Exception:
            pass
        time.sleep(3)


@agentic_bp.route("/api/agentic/channels/config", methods=["GET", "POST", "OPTIONS"])
def api_channels_config():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        cur = _telegram_cfg()
        tg = dict(cur.get("telegram", {}) or {})
        for k in ("bot_token", "chat_id"):
            if data.get(k) is not None:
                tg[k] = str(data.get(k)).strip()
        if data.get("bot_token") == "********":
            tg["bot_token"] = tg.get("bot_token", "")  # masked -> keep existing
        cur["telegram"] = tg
        _cfg_set("channels", json.dumps(cur))
        _audit("store", "channels.config", "telegram config updated")
        return jsonify({"status": "ok", "telegram": {
            "bot_token": ("********" if tg.get("bot_token") else ""),
            "chat_id": tg.get("chat_id", "")}})
    cur = _telegram_cfg()
    tg = cur.get("telegram", {}) or {}
    return jsonify({"status": "ok", "telegram": {
        "bot_token": ("********" if tg.get("bot_token") else ""),
        "chat_id": tg.get("chat_id", "")}})


@agentic_bp.route("/api/agentic/channels/test", methods=["POST", "OPTIONS"])
def api_channels_test():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    token = (data.get("bot_token") or "").strip() or (_telegram_cfg().get("telegram", {}) or {}).get("bot_token") or ""
    if not token:
        return jsonify({"status": "error", "error": "no bot token"}), 400
    me, status = _http(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
    if status == 200:
        return jsonify({"status": "ok", "bot": (me or {}).get("result", {}).get("username", "?")})
    return jsonify({"status": "error", "error": f"HTTP {status}: {str(me)[:200]}"})


@agentic_bp.route("/api/agentic/channels/send", methods=["POST", "OPTIONS"])
def api_channels_send():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    ok, err = _telegram_send(text)
    _audit("store", "channels.send", f"telegram {'ok' if ok else 'failed: ' + err}")
    return jsonify({"status": "ok" if ok else "error", "error": err if not ok else None})


# ---------------------------------------------------------------------------
# 3. FEEDBACK + SELF-IMPROVEMENT — 👍/👎 on every reply; nightly job aggregates
#    negatives and proposes how the agent improves itself.
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/feedback", methods=["GET", "POST", "OPTIONS"])
def api_feedback():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        rating = 1 if int(data.get("rating", 0) or 0) > 0 else -1
        conn = _db()
        cur = conn.execute(
            "INSERT INTO feedback (ts, agent, rating, comment, reply_preview) VALUES (?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), (data.get("agent") or "hermes"),
             rating, (data.get("comment") or ""), (data.get("reply_preview") or "")[:400]))
        conn.commit()
        conn.close()
        _audit("store", "feedback", f"{'👍' if rating > 0 else '👎'} {data.get('agent')} :: {(data.get('comment') or '')[:80]}")
        return jsonify({"status": "ok", "id": cur.lastrowid, "rating": rating})
    conn = _db()
    rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify({"status": "ok", "feedback": [dict(r) for r in rows]})


def _run_self_improvement(force=False):
    """Aggregate negative feedback (7d) -> LLM proposes improvements -> vault +
    proposals table. Runs once/day from the watchdog loop."""
    last = _cfg_get("self_improve_last_run") or ""
    today = _today_str()
    if last == today and not force:
        return {"status": "skipped", "reason": "already ran today"}
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM feedback WHERE rating < 0 AND substr(ts,1,10) >= date('now','-7 days') "
        "ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    _cfg_set("self_improve_last_run", today)
    if not rows:
        _audit("agent", "self-improve", "no negative feedback in 7d — nothing to propose")
        return {"status": "ok", "proposals": 0}
    items = "\n".join(
        f"- [{r['agent']}] {(r['reply_preview'] or '')[:200]} -> {r['comment'] or '(no comment)'}"
        for r in rows[:10])
    sys_prompt = ("You are the self-improvement engine of an agent OS. Based ONLY on the negative "
                  "feedback below, propose 2-4 concrete improvements to the agent's skills, prompts, "
                  "or knowledge. Output STRICT JSON: [{\"summary\": \"short title\", \"details\": "
                  "\"specific change to make (e.g. update the X skill to do Y)\"}]. No preamble.")
    try:
        raw = _call_llm_with({}, f"Negative feedback from the last 7 days:\n{items}\n\nPropose improvements.",
                             system_prompt=sys_prompt, agent="hermes", timeout=60)
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw or "").strip()
    m = re.search(r"\[.*\]", cleaned, re.S)
    try:
        proposals = json.loads(m.group(0)) if m else json.loads(cleaned)
    except Exception:
        proposals = []
    vault = _vault_path()
    d = os.path.join(vault, "04_Projects", "Skills", "Improvements")
    os.makedirs(d, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"Improvements_{ts}.md"
    body = f"# Agent Self-Improvement Proposals — {ts}\n\nBased on {len(rows)} negative feedback items:\n\n"
    conn = _db()
    n = 0
    for p in proposals[:6]:
        summary = (p.get("summary") or "Improvement").strip()
        details = (p.get("details") or "").strip()
        if not details:
            continue
        body += f"## {summary}\n\n{details}\n\n"
        conn.execute("INSERT INTO improvement_proposals (ts, summary, details, status, vault_file) "
                     "VALUES (?,?,?,?,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            summary, details, "open", f"04_Projects/Skills/Improvements/{fname}"))
        n += 1
    conn.commit()
    conn.close()
    with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
        f.write(body)
    _audit("agent", "self-improve", f"{n} proposals from {len(rows)} negative feedback items -> {fname}")
    return {"status": "ok", "proposals": n, "file": f"04_Projects/Skills/Improvements/{fname}"}


@agentic_bp.route("/api/agentic/self-improve", methods=["POST", "GET", "OPTIONS"])
def api_self_improve():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        return jsonify(_run_self_improvement(force=True))
    conn = _db()
    rows = conn.execute("SELECT * FROM improvement_proposals ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify({"status": "ok", "proposals": [dict(r) for r in rows]})


@agentic_bp.route("/api/agentic/self-improve/<int:pid>/status", methods=["POST", "OPTIONS"])
def api_self_improve_status(pid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    status = data.get("status", "applied")
    if status not in ("open", "applied", "dismissed"):
        return jsonify({"error": "bad status"}), 400
    conn = _db()
    row = conn.execute("SELECT * FROM improvement_proposals WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    if status == "applied":
        # applying = save the proposal as a real skill doc
        _save_skill(row["summary"], row["details"][:200], row["details"], source="self-improvement")
    conn.execute("UPDATE improvement_proposals SET status=? WHERE id=?", (status, pid))
    conn.commit()
    conn.close()
    _audit("store", "self-improve.status", f"proposal {pid} -> {status}")
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# 4. EVALS — test suites for the agent: logic + information + personality.
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/evals", methods=["GET", "POST", "OPTIONS"])
def api_evals():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        prompt = (data.get("prompt") or "").strip()
        if not name or not prompt:
            return jsonify({"error": "name + prompt required"}), 400
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute(
            "INSERT INTO evals (name, agent, prompt, expected_contains, created, updated) VALUES (?,?,?,?,?,?)",
            (name, (data.get("agent") or "hermes"), prompt,
             (data.get("expected_contains") or ""), now, now))
        conn.commit()
        conn.close()
        _audit("store", "eval.create", f"eval #{cur.lastrowid} {name}")
        return jsonify({"status": "ok", "id": cur.lastrowid})
    conn = _db()
    rows = conn.execute("SELECT * FROM evals ORDER BY id DESC").fetchall()
    runs = {}
    for r in conn.execute("SELECT eval_id, COUNT(*) n, SUM(passed) ok, MAX(ts) last FROM eval_runs GROUP BY eval_id").fetchall():
        runs[r["eval_id"]] = {"runs": r["n"], "passed": r["ok"] or 0, "last": r["last"]}
    conn.close()
    evals = []
    for r in rows:
        e = dict(r)
        e["stats"] = runs.get(r["id"], {"runs": 0, "passed": 0, "last": None})
        evals.append(e)
    return jsonify({"status": "ok", "evals": evals})


@agentic_bp.route("/api/agentic/evals/<int:eid>", methods=["DELETE", "OPTIONS"])
def api_eval(eid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    conn.execute("DELETE FROM evals WHERE id=?", (eid,))
    conn.execute("DELETE FROM eval_runs WHERE eval_id=?", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "deleted": eid})


def _run_eval(row):
    t0 = time.time()
    try:
        output = _call_llm_with({}, row["prompt"], agent=row["agent"] or "hermes", timeout=60)
    except Exception as e:
        output = f"[ERROR] {e}"
    latency = int((time.time() - t0) * 1000)
    expected = (row["expected_contains"] or "").strip().lower()
    passed = 1 if (not expected or expected in (output or "").lower()) else 0
    conn = _db()
    conn.execute("INSERT INTO eval_runs (eval_id, ts, passed, output, latency_ms) VALUES (?,?,?,?,?)",
                 (row["id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), passed, (output or "")[:2000], latency))
    conn.commit()
    conn.close()
    return {"eval_id": row["id"], "name": row["name"], "passed": bool(passed),
            "output": (output or "")[:300], "latency_ms": latency}


@agentic_bp.route("/api/agentic/evals/<int:eid>/run", methods=["POST", "OPTIONS"])
def api_eval_run(eid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM evals WHERE id=?", (eid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    res = _run_eval(row)
    _audit("store", "eval.run", f"{res['name']} -> {'PASS' if res['passed'] else 'FAIL'} ({res['latency_ms']}ms)")
    return jsonify({"status": "ok", **res})


@agentic_bp.route("/api/agentic/evals/run-all", methods=["POST", "OPTIONS"])
def api_evals_run_all():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    rows = conn.execute("SELECT * FROM evals").fetchall()
    conn.close()
    results = [_run_eval(r) for r in rows]
    passed = sum(1 for r in results if r["passed"])
    _audit("store", "eval.run-all", f"{passed}/{len(results)} passed")
    return jsonify({"status": "ok", "results": results, "passed": passed, "total": len(results)})


@agentic_bp.route("/api/agentic/evals/<int:eid>/history", methods=["GET", "OPTIONS"])
def api_eval_history(eid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    rows = conn.execute("SELECT * FROM eval_runs WHERE eval_id=? ORDER BY id DESC LIMIT 20", (eid,)).fetchall()
    conn.close()
    return jsonify({"status": "ok", "runs": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# 5. ROUTER AGENT ("V") — one entry point that delegates to the right
#    capability: knowledge, content, media, SEO, crew, or plain chat.
# ---------------------------------------------------------------------------
_ROUTER_AGENT_KEYWORDS = {
    "media": ["image", "picture", "photo", "logo", "generate an image", "draw", "visual", "icon"],
    "seo": ["keyword", "seo", "rank", "search volume"],
    "crew": ["crew", "delegate", "team of agents", "audit", "analyze this project"],
    "content": ["post", "publish", "linkedin", "blog", "article", "x post", "tweet", "content about"],
    "knowledge": ["what is", "who is", "search", "find", "remember", "do we know", "memory", "notes about"],
}


def _router_reply(message, agent_id="v", source="store"):
    """Route a message to the right capability; persist the conversation."""
    msg = (message or "").strip()
    low = msg.lower()
    route = "chat"
    for cap, kws in _ROUTER_AGENT_KEYWORDS.items():
        if any(k in low for k in kws):
            route = cap
            break
    # media: real image generation
    if route == "media":
        style = "photo"
        for s, lbl in (("anime", "anime"), ("logo", "logo"), ("3d", "3d"), ("art", "art")):
            if lbl in low:
                style = s
        try:
            conn = _db()
            prompt = msg
            style_suffix = MEDIA_STYLES.get(style, style)
            url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(f"{prompt}, {style_suffix}") +
                   f"?width=1024&height=1024&nologo=true&seed={int(time.time()) % 1000000}")
            body, status = _http_bytes(url, timeout=120)
            if status == 200 and body:
                vault = _vault_path()
                d = os.path.join(vault, "05_Media")
                os.makedirs(d, exist_ok=True)
                fname = f"V_{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
                with open(os.path.join(d, fname), "wb") as f:
                    f.write(body)
                conn.execute("INSERT INTO media_assets (prompt, style, file, provider, created) VALUES (?,?,?,?,?)",
                             (msg, style, fname, "pollinations", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
                conn.close()
                return f"🖼 Generated image: `{fname}` — open it in 📦 Artifacts → Media, or here:\n{API_BASE_HINT()}/api/agentic/media/file/{fname}"
            conn.close()
        except Exception:
            pass
        return "⚠️ Image generation failed — check the agent has internet egress to the image provider."
    # seo: keyword research
    if route == "seo":
        try:
            conn = _db()
            kws = conn.execute("SELECT * FROM seo_keywords ORDER BY id DESC LIMIT 8").fetchall()
            conn.close()
            if kws:
                lines = "\n".join(f"- {r['keyword']} ({r['intent']}, diff {r['difficulty']}, ~{r['volume']})" for r in kws)
                return f"🧲 Your latest keyword clusters:\n{lines}\n\nUse 🧲 SEO Studio to research a new cluster or generate an article."
        except Exception:
            pass
        return "🧲 SEO: run keyword research in SEO Studio (seed topic → cluster → article)."
    # crew: dispatch a default crew
    if route == "crew":
        try:
            results, errors = _dispatch_crew("V-Routed Crew", msg, roles=[("Architect", "crew-architect"),
                                                                          ("Lead Engineer", "crew-engineer"),
                                                                          ("Code Reviewer", "crew-reviewer")])
            parts = [f"👥 Crew dispatch on: {msg[:120]}"]
            for label, reply in results.items():
                parts.append(f"\n## {label}\n{reply[:500]}")
            for label, err in errors.items():
                parts.append(f"\n## {label} — ERROR\n{err[:200]}")
            return "\n".join(parts)[:3000]
        except Exception as e:
            return f"⚠️ Crew dispatch failed: {str(e)[:200]}"
    # content: oracle-style generation (signals -> draft)
    if route == "content":
        try:
            sys_prompt = ("You are a content strategist. Write a LinkedIn post (200-320 words) from the "
                          "research signals: bold hook, 3 concrete takeaways, a question to drive comments. "
                          "Plain text, no hashtag spam. Output ONLY the post body.")
            sigs = _sweep_feeds(limit=5)
            sig_lines = "\n".join(f"- {s.get('title','')} {s.get('link','')}" for s in sigs[:5])
            content = _call_llm_with({}, f"Topic: {msg}\n\nSignals:\n{sig_lines}\n\nWrite the post.",
                                     system_prompt=sys_prompt, agent="oracle", timeout=60)
            conn = _db()
            conn.execute("INSERT INTO oracle_posts (feed_id, platform, title, content, status, created) "
                         "VALUES (?,?,?,?,?,?)",
                         (None, "linkedin", msg[:80], content, "draft",
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
            _write_vault_output("04_Projects/Outputs", f"V_Post_{int(time.time())}.md",
                                f"# V-Routed Post: {msg}\n\n{content}\n", tag="V Content", agent="V")
            return f"📝 Drafted a LinkedIn post from live signals (saved to vault + posts pipeline):\n\n{content[:900]}"
        except Exception as e:
            return f"⚠️ Content generation failed: {str(e)[:200]}"
    # knowledge + default: full-context chat (identity + memory + vault + skills)
    ctx = _memory_context(msg)
    try:
        return _call_llm_with({}, f"Question: {msg}\n\n{ctx if ctx else ''}",
                              agent="hermes", timeout=60)
    except Exception as e:
        return f"⚠️ LLM call failed: {str(e)[:200]}"


def API_BASE_HINT():
    return "http://localhost:8086"


@agentic_bp.route("/api/agentic/v", methods=["POST", "GET", "OPTIONS"])
def api_router_v():
    """The V entry point: post a message, get a routed, persisted reply."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        return jsonify({"status": "ok", "conversation": _get_conversation("v"),
                        "routes": {k: v for k, v in _ROUTER_AGENT_KEYWORDS.items()}})
    data = request.get_json() or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "message required"}), 400
    conn = _db()
    msgs = _get_conversation("v")
    reply = _router_reply(msg, agent_id="v", source="store")
    ts = datetime.now().strftime("%H:%M")
    msgs.append({"role": "user", "sender": "You", "text": msg, "timestamp": ts})
    msgs.append({"role": "assistant", "sender": "V", "text": reply, "timestamp": ts})
    _save_conversation("v", msgs)
    _audit("store", "v.chat", f"{msg[:80]} -> routed {_router_route_of(msg)}")
    return jsonify({"status": "ok", "reply": reply, "conversation": msgs[-10:]})


def _router_route_of(msg):
    low = (msg or "").lower()
    for cap, kws in _ROUTER_AGENT_KEYWORDS.items():
        if any(k in low for k in kws):
            return cap
    return "chat"


# ---------------------------------------------------------------------------
# 6. APPROVALS + AUDIT TRAIL — human-in-the-loop for sensitive agent actions.
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/approvals", methods=["GET", "POST", "OPTIONS"])
def api_approvals():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        action = (data.get("action") or "").strip()
        if action not in ("publish", "restart", "install", "custom"):
            return jsonify({"error": "action must be publish|restart|install|custom"}), 400
        conn = _db()
        cur = conn.execute(
            "INSERT INTO approvals (ts, action, target, payload, status, requested_by) VALUES (?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, (data.get("target") or ""),
             json.dumps(data.get("payload") or {}), "pending", (data.get("requested_by") or "store")))
        conn.commit()
        conn.close()
        _audit("store", "approval.request", f"{action} {data.get('target')} -> pending #{cur.lastrowid}")
        return jsonify({"status": "ok", "id": cur.lastrowid})
    conn = _db()
    status = (request.args.get("status") or "").strip()
    if status:
        rows = conn.execute("SELECT * FROM approvals WHERE status=? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"status": "ok", "approvals": [dict(r) for r in rows]})


def _execute_approval(a):
    """Execute an approved action. Returns (ok, detail)."""
    action = a["action"]
    payload = {}
    try:
        payload = json.loads(a["payload"] or "{}")
    except Exception:
        pass
    if action == "publish":
        webhook = payload.get("webhook_url") or _cfg_get("n8n_webhook") or \
            "http://host.docker.internal:37950/webhook/appvault-publish"
        resp, status = _http(webhook, method="POST", json_data=payload, timeout=15)
        return status in (200, 201, 202), f"n8n HTTP {status}"
    if action == "restart":
        ok, detail = _docker_restart(payload.get("container") or a["target"])
        return ok, detail
    return True, f"custom action {a['target']} acknowledged"


@agentic_bp.route("/api/agentic/approvals/<int:aid>/decide", methods=["POST", "OPTIONS"])
def api_approval_decide(aid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    decision = (data.get("approved") is True) or (str(data.get("decision", "")).lower() == "approve")
    conn = _db()
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    a = dict(row)
    conn.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?",
                 ("approved" if decision else "denied", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), aid))
    conn.commit()
    conn.close()
    detail = "denied by operator"
    if decision:
        ok, detail = _execute_approval(a)
        detail = f"{'executed' if ok else 'FAILED'}: {detail}"
    _audit("store", "approval.decide", f"#{aid} {a['action']} -> {'APPROVED' if decision else 'DENIED'} ({detail[:200]})")
    return jsonify({"status": "ok", "approved": decision, "detail": detail})


@agentic_bp.route("/api/agentic/audit", methods=["GET", "OPTIONS"])
def api_audit():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify({"status": "ok", "log": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# 7. EVENT INGRESS — any system (n8n/Stripe/email) can fire an event into the
#    agent's brain; optional pipeline trigger by event type.
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/events", methods=["GET", "POST", "OPTIONS"])
def api_events():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        ev = (data.get("event") or data.get("type") or "").strip()
        if not ev:
            return jsonify({"error": "event name required"}), 400
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute("INSERT INTO events (ts, event, source, payload) VALUES (?,?,?,?)",
                           (now, ev, (data.get("source") or "external"), json.dumps(data.get("payload") or {})))
        conn.commit()
        conn.close()
        _audit("events", ev, f"from {data.get('source') or 'external'}: {str(data.get('payload') or {})[:200]}")
        # memory row so future chats know about the event
        try:
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), data.get("source") or "Event", "Event",
                          f"Event `{ev}`: {str(data.get('payload') or '')[:250]}", "auto", "event", now))
            conn.commit()
            conn.close()
        except Exception:
            pass
        # optional pipeline trigger
        triggers = {}
        try:
            raw = _cfg_get("event_triggers") or ""
            triggers = json.loads(raw) if raw else {}
        except Exception:
            pass
        pid = triggers.get(ev)
        if pid:
            try:
                conn = _db()
                row = conn.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
                conn.close()
                if row:
                    nodes = json.loads(row["nodes"] or "[]")
                    edges = json.loads(row["edges"] or "[]")
                    outputs, logs, complete = _run_pipeline_nodes(nodes, edges)
                    _finalize_pipeline_run(row["name"] or pid, nodes, outputs, logs, complete)
                    return jsonify({"status": "ok", "event_id": cur.lastrowid, "triggered_pipeline": pid,
                                    "complete": complete})
            except Exception:
                pass
        return jsonify({"status": "ok", "event_id": cur.lastrowid})
    conn = _db()
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"status": "ok", "events": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# 8. CONSORTIUM — ask N providers the same question, summarize consensus.
# ---------------------------------------------------------------------------
CONSORTIUM_MODELS = {
    "deepseek": "deepseek-chat",
    "grok": "grok-3",
    "anthropic": "claude-3-5-sonnet-20241022",
    "ollama": "",
    "openai": "gpt-4o-mini",
}


@agentic_bp.route("/api/agentic/consortium", methods=["POST", "OPTIONS"])
def api_consortium():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question required"}), 400
    providers = [p for p in (data.get("providers") or ["deepseek", "grok", "anthropic"]) if p in CONSORTIUM_MODELS]
    providers = providers[:3]
    cfg = _get_llm_config()
    pkeys = cfg.get("provider_keys") or {}
    answers = {}
    for p in providers:
        model = data.get("models", {}).get(p) or CONSORTIUM_MODELS.get(p) or cfg.get("model")
        overrides = {"provider": p, "model": model}
        try:
            ans = _call_llm_with(overrides, question, agent="hermes", timeout=45)
            answers[p] = ans
        except Exception as e:
            answers[p] = f"[ERROR] {str(e)[:150]}"
    summary = ""
    if len(answers) >= 2:
        try:
            joined = "\n\n".join(f"### {p}\n{a[:1200]}" for p, a in answers.items())
            summary = _call_llm_with(
                {}, f"Three models answered the question: \"{question}\".\n\n{joined}\n\n"
                    f"Produce a consensus answer: what they agree on, where they differ, and the best synthesis.",
                agent="hermes", timeout=60)
        except Exception:
            pass
    _audit("store", "consortium", f"{question[:80]} across {list(answers.keys())}")
    return jsonify({"status": "ok", "question": question, "answers": answers, "summary": summary})


# ---------------------------------------------------------------------------
# Watchdog hooks: daily self-improvement + digest-to-channel delivery.
# ---------------------------------------------------------------------------
def _maybe_deliver_digest_to_channel():
    """If a Telegram channel is configured, send today's digest + daily log."""
    tg = (_telegram_cfg().get("telegram", {}) or {})
    if not tg.get("bot_token") or not tg.get("chat_id"):
        return
    vault = _vault_path()
    today = _today_str()
    digest = os.path.join(vault, "00_Intelligence", f"Digest_{today}.md")
    log = os.path.join(vault, "02_Agent_Logs", f"{today}.md")
    sent = False
    if os.path.isfile(digest):
        try:
            with open(digest, encoding="utf-8") as f:
                text = f.read()
            _telegram_send(f"📬 Daily Intelligence Digest — {today}\n\n{text[:3500]}")
            sent = True
        except Exception:
            pass
    if not sent and os.path.isfile(log):
        try:
            with open(log, encoding="utf-8") as f:
                text = f.read()
            _telegram_send(f"📔 Daily Log — {today}\n\n{text[:3500]}")
        except Exception:
            pass


def _start_v_threads():
    threading.Thread(target=_telegram_loop, daemon=True).start()

if os.environ.get("APPVAULT_V", "1") != "0":
    try:
        _start_v_threads()
    except Exception:
        pass
