# =============================================================================
# COMPOUNDING LAYER (2026-08-08) — the 7th layer of the Agent OS blueprint:
# identity profile · goals · SEO workflow · media agent · artifacts gallery ·
# skills library · conversation capture (daily log) · output loop enforcement.
# Spliced into agentic_plane.py as one appended block (stdlib-only: the agent
# image has NO requests module; every route has an OPTIONS guard first line).
# =============================================================================

# ---------------------------------------------------------------------------
# New tables (created on import — _init_db() already ran at module load)
# ---------------------------------------------------------------------------
def _init_compounding_tables():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, description TEXT,
        status TEXT DEFAULT 'active',
        priority INTEGER DEFAULT 3,
        progress INTEGER DEFAULT 0,
        kpis TEXT DEFAULT '',
        linked_feeds TEXT DEFAULT '',
        linked_crews TEXT DEFAULT '',
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS seo_keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cluster TEXT, keyword TEXT, intent TEXT,
        difficulty INTEGER DEFAULT 50, volume INTEGER DEFAULT 0,
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, description TEXT, content TEXT,
        source TEXT DEFAULT 'manual', uses INTEGER DEFAULT 0,
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS media_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt TEXT, style TEXT, file TEXT, provider TEXT,
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS capture_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, note TEXT
    );
    """)
    conn.commit()
    conn.close()

_init_compounding_tables()


# ---------------------------------------------------------------------------
# 1. IDENTITY / PROFILE  — who the user is; injected into EVERY LLM call.
#    (the video's Layer 2 pain: "none of them actually know who you are")
# ---------------------------------------------------------------------------
IDENTITY_DEFAULTS = {
    "name": "",
    "brand": "",
    "voice": "",
    "audience": "",
    "tone": "",
    "goals_summary": "",
    "keywords": "",
}

def _get_profile():
    raw = _cfg_get("identity") or ""
    try:
        prof = json.loads(raw) if raw else {}
    except Exception:
        prof = {}
    return {**IDENTITY_DEFAULTS, **{k: v for k, v in (prof or {}).items() if v is not None}}


def _set_profile(patch):
    prof = _get_profile()
    for k, v in (patch or {}).items():
        if k in prof and v is not None:
            prof[k] = str(v).strip()
    _cfg_set("identity", json.dumps(prof))
    _mirror_profile_to_vault(prof)
    return prof


def _identity_block():
    """Compact 'WHO YOU ARE' block appended to every system prompt."""
    p = _get_profile()
    lines = []
    if p.get("name"):
        lines.append(f"- Name: {p['name']}")
    if p.get("brand"):
        lines.append(f"- Brand/Product: {p['brand']}")
    if p.get("audience"):
        lines.append(f"- Audience: {p['audience']}")
    if p.get("voice"):
        lines.append(f"- Voice: {p['voice']}")
    if p.get("tone"):
        lines.append(f"- Tone: {p['tone']}")
    if p.get("keywords"):
        lines.append(f"- Focus keywords/topics: {p['keywords']}")
    if p.get("goals_summary"):
        lines.append(f"- Goals: {p['goals_summary']}")
    goals = _goals_context(compact=True)
    if goals:
        lines.append("- Active goals:\n" + goals)
    if not lines:
        return ""
    return ("\n\n===== WHO YOU ARE (user identity profile — use it to personalize "
            "every answer; never guess) =====\n" + "\n".join(lines) +
            "\n===== END IDENTITY =====\n")


def _mirror_profile_to_vault(prof):
    """Compounding loop: the identity profile is an artifact — write it to the vault."""
    try:
        vault = _vault_path()
        d = os.path.join(vault, "04_Projects")
        os.makedirs(d, exist_ok=True)
        body = f"# Identity Profile\n\n> Auto-synced from Agentic OS — every agent reads this.\n\n"
        for k, v in prof.items():
            if v:
                body += f"**{k.replace('_', ' ').title()}:** {v}\n\n"
        with open(os.path.join(d, "Identity_Profile.md"), "w", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        pass


@agentic_bp.route("/api/agentic/profile", methods=["GET", "POST", "OPTIONS"])
def api_profile():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        prof = _set_profile(request.get_json() or {})
        return jsonify({"status": "ok", "profile": prof})
    return jsonify({"status": "ok", "profile": _get_profile()})


# ---------------------------------------------------------------------------
# 2. GOALS — production layer. Active goals are injected into every LLM call
#    (via the identity block) and shown to oracle/crew dispatches.
# ---------------------------------------------------------------------------
def _goals_context(compact=False):
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM goals WHERE status='active' ORDER BY priority ASC, id DESC LIMIT 6"
        ).fetchall()
        conn.close()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = []
    for r in rows:
        kpi = f" — KPIs: {r['kpis']}" if r["kpis"] else ""
        if compact:
            lines.append(f"  * [{r['priority']}] {r['title']} ({r['progress']}%){kpi}")
        else:
            lines.append(f"  * {r['title']} — {r['description'] or ''} [{r['progress']}%]{kpi}")
    return "\n".join(lines)


def _goal_row_to_dict(r):
    return {
        "id": r["id"], "title": r["title"], "description": r["description"],
        "status": r["status"], "priority": r["priority"], "progress": r["progress"],
        "kpis": (r["kpis"] or "").split(",") if r["kpis"] else [],
        "linked_feeds": (r["linked_feeds"] or "").split(",") if r["linked_feeds"] else [],
        "linked_crews": (r["linked_crews"] or "").split(",") if r["linked_crews"] else [],
        "created": r["created"], "updated": r["updated"],
    }


@agentic_bp.route("/api/agentic/goals", methods=["GET", "POST", "OPTIONS"])
def api_goals():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title required"}), 400
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute(
            "INSERT INTO goals (title, description, status, priority, progress, kpis, "
            "linked_feeds, linked_crews, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, (data.get("description") or ""), (data.get("status") or "active"),
             int(data.get("priority", 3) or 3), int(data.get("progress", 0) or 0),
             ",".join(data.get("kpis") or []),
             ",".join(str(x) for x in (data.get("linked_feeds") or [])),
             ",".join(str(x) for x in (data.get("linked_crews") or [])),
             now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM goals WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        return jsonify({"status": "ok", "goal": _goal_row_to_dict(row)})
    conn = _db()
    rows = conn.execute("SELECT * FROM goals ORDER BY status='active' DESC, priority ASC, id DESC").fetchall()
    conn.close()
    return jsonify({"status": "ok", "goals": [_goal_row_to_dict(r) for r in rows]})


@agentic_bp.route("/api/agentic/goals/<int:gid>", methods=["PUT", "DELETE", "OPTIONS"])
def api_goal(gid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM goals WHERE id=?", (gid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": gid})
    row = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "goal not found"}), 404
    data = request.get_json() or {}
    # PARTIAL update — absent fields keep existing values (PUT pitfall rule)
    merged = {}
    for k in ("title", "description", "status", "kpis", "linked_feeds", "linked_crews"):
        if data.get(k) is not None:
            v = data[k]
            merged[k] = ",".join(v) if isinstance(v, list) else str(v)
    if data.get("priority") is not None:
        merged["priority"] = int(data["priority"])
    if data.get("progress") is not None:
        merged["progress"] = max(0, min(100, int(data["progress"])))
    merged["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k}=?" for k in merged)
    conn.execute(f"UPDATE goals SET {sets} WHERE id=?", (*merged.values(), gid))
    conn.commit()
    row = conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "goal": _goal_row_to_dict(row)})


# ---------------------------------------------------------------------------
# 3. SEO WORKFLOW — keyword research (LLM cluster) + SEO article generation
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/seo/keywords", methods=["GET", "POST", "DELETE", "OPTIONS"])
def api_seo_keywords():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        cluster = (data.get("cluster") or "General").strip()
        items = data.get("keywords") or []
        if not items:
            return jsonify({"error": "keywords list required"}), 400
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        added = 0
        for it in items:
            kw = (it.get("keyword") or "").strip()
            if not kw:
                continue
            conn.execute(
                "INSERT INTO seo_keywords (cluster, keyword, intent, difficulty, volume, created) "
                "VALUES (?,?,?,?,?,?)",
                (cluster, kw, (it.get("intent") or "informational"),
                 int(it.get("difficulty", 50) or 50), int(it.get("volume", 0) or 0), now))
            added += 1
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "cluster": cluster, "added": added})
    if request.method == "DELETE":
        data = request.get_json() or {}
        conn = _db()
        conn.execute("DELETE FROM seo_keywords WHERE cluster=? OR id=?",
                     ((data.get("cluster") or ""), int(data.get("id") or 0)))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    cluster = (request.args.get("cluster") or "").strip()
    conn = _db()
    if cluster:
        rows = conn.execute("SELECT * FROM seo_keywords WHERE cluster=? ORDER BY volume DESC, id DESC",
                            (cluster,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM seo_keywords ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    clusters = {}
    for r in rows:
        c = r["cluster"] or "General"
        clusters.setdefault(c, []).append({
            "id": r["id"], "keyword": r["keyword"], "intent": r["intent"],
            "difficulty": r["difficulty"], "volume": r["volume"], "created": r["created"]})
    return jsonify({"status": "ok", "clusters": clusters})


@agentic_bp.route("/api/agentic/seo/research", methods=["POST", "OPTIONS"])
def api_seo_research():
    """LLM keyword research: seed topic -> cluster of {keyword, intent, difficulty, volume}."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    seed = (data.get("seed") or "").strip()
    count = min(20, int(data.get("count", 10) or 10))
    if not seed:
        return jsonify({"error": "seed keyword required"}), 400
    sys_prompt = ("You are an SEO keyword researcher. Output STRICT JSON only — an array of "
                  "objects: [{\"keyword\": \"...\", \"intent\": \"informational|commercial|transactional|navigational\", "
                  "\"difficulty\": 0-100, \"volume\": 0-100000}] — no markdown, no preamble.")
    try:
        raw = _call_llm(
            f"Research a keyword cluster for the seed topic: \"{seed}\". Generate {count} keywords "
            f"ordered by relevance: head terms, long-tail variants, and question-based queries. "
            f"Estimate difficulty (0-100) and monthly search volume (0-100000) per keyword.",
            system_prompt=sys_prompt, agent="hermes", timeout=60)
    except Exception as e:
        return jsonify({"status": "error", "error": f"LLM research failed: {str(e)[:200]}"}), 502
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw or "").strip()
    m = re.search(r"\[.*\]", cleaned, re.S)
    try:
        items = json.loads(m.group(0)) if m else json.loads(cleaned)
    except Exception:
        return jsonify({"status": "error", "error": "LLM did not return valid keyword JSON"}), 502
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    added = 0
    for it in items[:count]:
        kw = (it.get("keyword") or "").strip()
        if not kw:
            continue
        conn.execute(
            "INSERT INTO seo_keywords (cluster, keyword, intent, difficulty, volume, created) "
            "VALUES (?,?,?,?,?,?)",
            (seed, kw, (it.get("intent") or "informational"),
             max(0, min(100, int(it.get("difficulty", 50) or 50))),
             max(0, int(it.get("volume", 0) or 0)), now))
        added += 1
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "cluster": seed, "added": added, "keywords": items[:count]})


@agentic_bp.route("/api/agentic/seo/generate", methods=["POST", "OPTIONS"])
def api_seo_generate():
    """SEO-optimized blog article from a keyword cluster (optionally + oracle signals)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    cluster = (data.get("cluster") or "").strip()
    title_seed = (data.get("title_seed") or cluster or "topic").strip()
    if not cluster:
        return jsonify({"error": "cluster (keyword topic) required"}), 400
    conn = _db()
    kws = conn.execute(
        "SELECT * FROM seo_keywords WHERE cluster=? ORDER BY volume DESC LIMIT 12", (cluster,)).fetchall()
    conn.close()
    kw_lines = "\n".join(
        f"- {r['keyword']} ({r['intent']}, diff {r['difficulty']}, vol ~{r['volume']})" for r in kws) \
        or f"- {cluster} (informational)"
    sig_block = ""
    if data.get("feed_id") is not None:
        feed = _get_feed(int(data["feed_id"]))
        if feed:
            sigs = _sweep_feed_sources(feed)[:6]
            sig_block = "\nResearch signals:\n" + "\n".join(
                f"- {s.get('title', '')} {s.get('link', '')}" for s in sigs)
    sys_prompt = ("You are an SEO content strategist. Write a 600-900 word blog article in "
                  "markdown with: an SEO title (H1) containing the primary keyword, a meta "
                  "description (2-3 lines, in a blockquote labelled META), an intro targeting "
                  "the primary keyword, H2/H3 sections covering the keyword cluster naturally, "
                  "a comparison/insight section, and a conclusion with a call to action. "
                  "Keywords must appear naturally — no keyword stuffing. Cite any research links inline.")
    try:
        content = _call_llm(
            f"Primary topic: {title_seed}\n\nKeyword cluster to target:\n{kw_lines}\n{sig_block}\n\n"
            f"Write the SEO article now.",
            system_prompt=sys_prompt, agent="oracle", timeout=90)
    except Exception as e:
        return jsonify({"status": "error", "error": f"LLM generation failed: {str(e)[:200]}"}), 502
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    cur = conn.execute(
        "INSERT INTO oracle_posts (feed_id, platform, title, content, status, created) VALUES (?,?,?,?,?,?)",
        (data.get("feed_id"), "blog", f"SEO: {title_seed}", content, "draft", now))
    post_id = cur.lastrowid
    conn.commit()
    conn.close()
    _write_vault_output("04_Projects/Outputs", f"SEO_{int(time.time())}.md",
                        f"# SEO Article — {title_seed}\n\n**Cluster:** {cluster}\n\n{content}\n",
                        tag="SEO Article", agent="Oracle SEO")
    return jsonify({"status": "ok", "post_id": post_id, "content": content, "cluster": cluster})


# ---------------------------------------------------------------------------
# 4. MEDIA AGENT — keyless image generation (pollinations.ai) into the vault.
# ---------------------------------------------------------------------------
def _http_bytes(url, timeout=120):
    """Download raw bytes (images). stdlib only."""
    req = urllib.request.Request(url, headers={"User-Agent": "AppVault-Agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.status
    except urllib.error.HTTPError as e:
        return b"", e.code
    except Exception as e:
        return b"", 0


MEDIA_STYLES = {
    "photo": "photorealistic, natural lighting, high detail",
    "art": "digital art, vibrant colors, painterly",
    "3d": "3D render, octane, depth of field",
    "logo": "minimal logo design, flat vector, transparent background feel",
    "anime": "anime style, clean lines, cel shading",
}


@agentic_bp.route("/api/agentic/media", methods=["GET", "POST", "OPTIONS"])
def api_media():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt required"}), 400
        style = (data.get("style") or "photo").strip()
        style_suffix = MEDIA_STYLES.get(style, style)
        w = int(data.get("width", 1024) or 1024)
        h = int(data.get("height", 1024) or 1024)
        full_prompt = f"{prompt}, {style_suffix}"
        url = ("https://image.pollinations.ai/prompt/" +
               urllib.parse.quote(full_prompt) +
               f"?width={w}&height={h}&nologo=true&seed={int(time.time()) % 1000000}")
        body, status = _http_bytes(url, timeout=120)
        if status != 200 or not body:
            return jsonify({"status": "error", "error": f"image provider HTTP {status}"}), 502
        vault = _vault_path()
        d = os.path.join(vault, "05_Media")
        os.makedirs(d, exist_ok=True)
        fname = f"IMG_{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        fpath = os.path.join(d, fname)
        with open(fpath, "wb") as f:
            f.write(body)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute(
            "INSERT INTO media_assets (prompt, style, file, provider, created) VALUES (?,?,?,?,?)",
            (prompt, style, fname, "pollinations", now))
        conn.commit()
        conn.close()
        # compounding loop: memory row points at the artifact
        try:
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), "Media Agent", "Media Generated",
                          f"Generated `{fname}`: {prompt[:180]} (05_Media/)", "auto", "media", now))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({"status": "ok", "file": fname, "prompt": prompt, "style": style,
                        "url": f"/api/agentic/media/file/{fname}", "id": cur.lastrowid})
    conn = _db()
    rows = conn.execute("SELECT * FROM media_assets ORDER BY id DESC LIMIT 60").fetchall()
    conn.close()
    return jsonify({"status": "ok", "assets": [dict(r) for r in rows]})


@agentic_bp.route("/api/agentic/media/file/<fname>", methods=["GET", "OPTIONS"])
def api_media_file(fname):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    fname = os.path.basename(fname)  # no traversal
    fpath = os.path.join(_vault_path(), "05_Media", fname)
    if not os.path.isfile(fpath):
        return jsonify({"error": "file not found"}), 404
    try:
        from flask import send_file
        return send_file(fpath, mimetype="image/png")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 5. SKILLS LIBRARY — reusable skill documents (the Hermes compounding pattern)
# ---------------------------------------------------------------------------
def _save_skill(name, description, content, source="auto"):
    """Insert or update a skill doc (dedup by name). Returns id or None."""
    try:
        name = (name or "").strip()
        if not name:
            return None
        content = (content or "").strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        row = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        if row:
            conn.execute("UPDATE skills SET description=?, content=?, source=?, updated=?, uses=uses+1 WHERE id=?",
                         (description or "", content or row["content"], source, now, row["id"]))
            sid = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO skills (name, description, content, source, uses, created, updated) "
                "VALUES (?,?,?,?,0,?,?)", (name, description or "", content, source, now, now))
            sid = cur.lastrowid
        conn.commit()
        conn.close()
        _mirror_skill_to_vault(sid)
        return sid
    except Exception:
        return None


def _mirror_skill_to_vault(sid):
    try:
        conn = _db()
        row = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
        conn.close()
        if not row:
            return
        vault = _vault_path()
        d = os.path.join(vault, "04_Projects", "Skills")
        os.makedirs(d, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", (row["name"] or "skill").lower()).strip("-")[:60]
        with open(os.path.join(d, f"{slug}.md"), "w", encoding="utf-8") as f:
            f.write(f"# {row['name']}\n\n> Auto-saved skill · {row['source']} · {row['created']}\n\n"
                    f"{row['description'] or ''}\n\n---\n\n{row['content']}\n")
    except Exception:
        pass


def _skills_context(query, limit=3):
    """Top skill docs matching the query — injected into the LLM call."""
    try:
        conn = _db()
        rows = conn.execute("SELECT * FROM skills ORDER BY uses DESC, updated DESC LIMIT 30").fetchall()
        conn.close()
    except Exception:
        return ""
    qtoks = set(_tokenize(query or ""))
    scored = []
    for r in rows:
        blob = f"{r['name']} {r['description']} {r['content']}"
        toks = set(_tokenize(blob))
        overlap = len(qtoks & toks) if qtoks else 0
        if overlap or r["uses"] >= 3:
            scored.append((overlap, r))
    scored.sort(key=lambda x: (x[0], x[1]["uses"]), reverse=True)
    top = [r for _, r in scored[:limit]]
    if not top:
        return ""
    lines = [f"- **{r['name']}** (used {r['uses']}x): {r['description'] or r['content'][:120]}" for r in top]
    return ("\n\n===== RELEVANT SKILL DOCUMENTS (apply them if they match the task) =====\n"
            + "\n".join(lines) + "\n===== END SKILLS =====\n")


@agentic_bp.route("/api/agentic/skills", methods=["GET", "POST", "OPTIONS"])
def api_skills():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        sid = _save_skill(data.get("name"), data.get("description"), data.get("content"),
                          source=(data.get("source") or "manual"))
        if not sid:
            return jsonify({"error": "name + content required"}), 400
        return jsonify({"status": "ok", "id": sid})
    conn = _db()
    rows = conn.execute("SELECT * FROM skills ORDER BY uses DESC, updated DESC").fetchall()
    conn.close()
    return jsonify({"status": "ok", "skills": [dict(r) for r in rows]})


@agentic_bp.route("/api/agentic/skills/<int:sid>", methods=["GET", "DELETE", "OPTIONS"])
def api_skill(sid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM skills WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": sid})
    row = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "skill not found"}), 404
    return jsonify({"status": "ok", "skill": dict(row)})


@agentic_bp.route("/api/agentic/skills/<int:sid>/use", methods=["POST", "OPTIONS"])
def api_skill_use(sid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    conn.execute("UPDATE skills SET uses=uses+1, updated=? WHERE id=?",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sid))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# 6. OUTPUT LOOP ENFORCEMENT — every crew/pipeline run writes a structured note
#    to the vault + a memory row + auto-saves a reusable skill doc.
# ---------------------------------------------------------------------------
def _write_vault_output(subdir, fname, body, tag="Output", agent="System"):
    """Write an artifact into the vault + log a memory row. Returns rel path or None."""
    try:
        vault = _vault_path()
        d = os.path.join(vault, *subdir.split("/"))
        os.makedirs(d, exist_ok=True)
        fpath = os.path.join(d, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(body)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), agent, tag,
                      f"Wrote `{subdir}/{fname}` to the vault.", "auto", tag, now))
        conn.commit()
        conn.close()
        return os.path.join(subdir, fname)
    except Exception:
        return None


def _distill_run_skill(name, description, body, source):
    """Auto-save a compact skill doc from a completed run (the compounding loop)."""
    if not body or len(body) < 80:
        return
    _save_skill(name, description, body[:3000], source=source)


# hook: crew dispatch (replaces the bare _dispatch_crew body)
def _dispatch_crew_compounding(crew_name, task, roles=None):
    """_dispatch_crew + output loop: vault note + memory + auto skill doc."""
    results, errors = _dispatch_crew(crew_name, task, roles=roles)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    body = f"# Crew Run: {crew_name}\n\n**Task:** {task}\n\n**Ran:** {ts}\n\n"
    for label, reply in results.items():
        body += f"\n## {label}\n\n{reply}\n"
    for label, err in errors.items():
        body += f"\n## {label} — ERROR\n\n{err}\n"
    fname = f"Crew_{ts}.md"
    _write_vault_output("04_Projects/Outputs", fname, body, tag="Crew Output", agent="CrewAI")
    if results:
        _distill_run_skill(
            f"Crew run: {crew_name}",
            f"Reusable crew execution pattern for '{crew_name}' — task template and role outputs.",
            f"Task: {task}\n\n" + "\n".join(f"### {l}\n{r[:600]}" for l, r in results.items()),
            source=f"crew:{crew_name}")
    return results, errors, fname


# hook: pipeline run (called by api_pipeline_run)
def _finalize_pipeline_run(name, nodes, outputs, logs, complete):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    body = (f"# Pipeline Run: {name or 'Untitled'}\n\n**Ran:** {ts} · **Status:** "
            f"{'complete' if complete else 'partial'}\n\n## Node Log\n")
    for lg in logs:
        body += f"- `{lg['node']}` [{lg['type']}] {lg['status']}: {lg.get('output_preview', lg.get('error', ''))[:200]}\n"
    body += "\n## Outputs\n"
    for nid, out in outputs.items():
        body += f"\n### {nid}\n\n{str(out)[:1200]}\n"
    fname = f"Pipeline_{ts}.md"
    _write_vault_output("04_Projects/Outputs", fname, body, tag="Pipeline Output", agent="Workflow")
    if complete and logs:
        _distill_run_skill(
            f"Pipeline: {name or 'Untitled'}",
            "Reusable workflow pattern — node sequence that ran successfully.",
            "\n".join(f"- {lg['node']} ({lg['type']}) → {lg['status']}" for lg in logs),
            source="pipeline")
    return fname


# ---------------------------------------------------------------------------
# 7. ARTIFACTS GALLERY — everything the OS has produced, in one place.
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/artifacts", methods=["GET", "OPTIONS"])
def api_artifacts():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    posts = [dict(r) for r in conn.execute(
        "SELECT * FROM oracle_posts ORDER BY id DESC LIMIT 40").fetchall()]
    media = [dict(r) for r in conn.execute(
        "SELECT * FROM media_assets ORDER BY id DESC LIMIT 40").fetchall()]
    sessions = [{"id": r["id"], "title": r["title"], "updated": r["updated"]}
                for r in conn.execute("SELECT * FROM sessions ORDER BY updated DESC LIMIT 25").fetchall()]
    pipelines = [{"id": r["id"], "name": r["name"], "updated": r["updated"]}
                 for r in conn.execute("SELECT * FROM pipelines ORDER BY updated DESC LIMIT 25").fetchall()]
    skills = [dict(r) for r in conn.execute(
        "SELECT id, name, description, uses, updated FROM skills ORDER BY updated DESC LIMIT 25").fetchall()]
    goals = [dict(r) for r in conn.execute(
        "SELECT id, title, status, progress FROM goals ORDER BY id DESC LIMIT 25").fetchall()]
    conn.close()
    notes = []
    vault = _vault_path()
    for root, _, files in os.walk(vault):
        if "/.git" in root or ".obsidian" in root:
            continue
        for f in files:
            if not f.endswith(".md"):
                continue
            full = os.path.join(root, f)
            try:
                st = os.stat(full)
                size = st.st_size
                if size > 400_000:
                    continue
                rel = os.path.relpath(full, vault).replace("\\", "/")
                with open(full, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(400)
                title = head.splitlines()[0].lstrip("# ").strip() if head.splitlines() else f
                notes.append({"rel": rel, "title": title[:120], "size": size,
                              "mtime": st.st_mtime, "preview": head[:600]})
            except Exception:
                continue
    notes.sort(key=lambda n: n["mtime"], reverse=True)
    return jsonify({"status": "ok", "notes": notes[:80], "posts": posts, "media": media,
                    "sessions": sessions, "pipelines": pipelines, "skills": skills, "goals": goals})


# ---------------------------------------------------------------------------
# 8. CONVERSATION CAPTURE — OMI-style daily log: the day's activity written to
#    the vault so the system compounds even when nobody is watching.
# ---------------------------------------------------------------------------
def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _append_capture(note):
    """Append a line to today's capture log + persist capture_log row."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = _db()
        conn.execute("INSERT INTO capture_log (ts, note) VALUES (?,?)", (ts, note))
        conn.commit()
        conn.close()
    except Exception:
        pass
    vault = _vault_path()
    d = os.path.join(vault, "02_Agent_Logs")
    try:
        os.makedirs(d, exist_ok=True)
        fpath = os.path.join(d, f"{_today_str()}.md")
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(f"- **{ts}** — {note}\n")
    except Exception:
        pass


def _run_daily_capture(force=False):
    """Write today's full activity digest to 02_Agent_Logs/<date>.md."""
    vault = _vault_path()
    d = os.path.join(vault, "02_Agent_Logs")
    os.makedirs(d, exist_ok=True)
    fpath = os.path.join(d, f"{_today_str()}.md")
    if os.path.isfile(fpath) and not force:
        return {"status": "exists", "file": f"02_Agent_Logs/{_today_str()}.md"}
    today = _today_str()
    conn = _db()
    try:
        convs = conn.execute("SELECT agent_id, messages FROM conversations").fetchall()
        chats = 0
        for c in convs:
            try:
                msgs = json.loads(c["messages"] or "[]")
                chats += sum(1 for m in msgs if str(m.get("ts", ""))[:10] == today)
            except Exception:
                pass
    except Exception:
        chats = 0
    try:
        sess = conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE substr(updated,1,10)=?", (today,)).fetchone()["n"]
    except Exception:
        sess = 0
    try:
        mem = conn.execute("SELECT COUNT(*) AS n FROM memory WHERE substr(updated,1,10)=? OR substr(ts,1,10)=?",
                           (today, today)).fetchone()["n"]
    except Exception:
        mem = 0
    try:
        posts = conn.execute("SELECT COUNT(*) AS n FROM oracle_posts WHERE substr(created,1,10)=?", (today,)).fetchone()["n"]
    except Exception:
        posts = 0
    try:
        sigs = conn.execute("SELECT COUNT(*) AS n FROM media_assets WHERE substr(created,1,10)=?", (today,)).fetchone()["n"]
    except Exception:
        sigs = 0
    conn.close()
    body = (f"# Agentic OS Daily Log — {today}\n\n"
            f"## Today's Numbers\n- Chat messages: **{chats}** · Sessions: **{sess}**\n"
            f"- Memory notes: **{mem}** · Oracle posts: **{posts}** · Media generated: **{sigs}**\n\n"
            f"## Capture Feed\n")
    cap = []
    try:
        conn = _db()
        cap = [{"ts": r["ts"], "note": r["note"]}
               for r in conn.execute("SELECT * FROM capture_log WHERE substr(ts,1,10)=? ORDER BY id", (today,)).fetchall()]
        conn.close()
    except Exception:
        pass
    if cap:
        body += "\n".join(f"- **{c['ts'][11:16]}** — {c['note']}" for c in cap) + "\n"
    else:
        body += "- (no manual captures today)\n"
    goals = _goals_context(compact=False)
    if goals:
        body += f"\n## Active Goals\n{goals}\n"
    body += "\n---\n*Auto-generated by the Agentic OS daily capture (OMI-style).*\n"
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        return {"status": "error", "error": "vault write failed"}
    _append_capture("Daily digest regenerated")
    return {"status": "ok", "file": f"02_Agent_Logs/{_today_str()}.md",
            "stats": {"chats": chats, "sessions": sess, "memory": mem, "posts": posts, "media": sigs}}


def _capture_loop():
    while True:
        try:
            _run_daily_capture()
        except Exception:
            pass
        time.sleep(6 * 3600)  # every 6 hours


@agentic_bp.route("/api/agentic/capture", methods=["POST", "OPTIONS"])
def api_capture():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    note = (data.get("note") or "").strip()
    if note:
        _append_capture(note)
        if not data.get("regenerate"):
            return jsonify({"status": "ok", "logged": note})
    res = _run_daily_capture(force=True)
    return jsonify(res)


# ---------------------------------------------------------------------------
# Injection wiring: identity + skills into EVERY LLM call (all three chat paths
# and crew/oracle go through _call_llm_with, so one hook covers them all).
# ---------------------------------------------------------------------------
def _inject_compounding_context(user_msg, sys_prompt):
    """Return (user_msg, sys_prompt) with identity block + skills context added."""
    try:
        identity = _identity_block()
        if identity and "WHO YOU ARE" not in (sys_prompt or ""):
            sys_prompt = (sys_prompt or "") + identity
        skills = _skills_context(user_msg)
        if skills:
            user_msg = user_msg + skills
    except Exception:
        pass
    return user_msg, sys_prompt


# Capture the ORIGINAL before rebinding — inside the wrapper, `_call_llm_with`
# would already resolve to the wrapper itself (infinite recursion).
_ORIG_CALL_LLM_WITH = _call_llm_with

def _call_llm_with_compounding(overrides, user_msg, system_prompt=None, agent="hermes", timeout=25):
    """_call_llm_with + compounding injection. Wraps the original function."""
    user_msg, system_prompt = _inject_compounding_context(user_msg, system_prompt)
    return _ORIG_CALL_LLM_WITH(overrides, user_msg, system_prompt=system_prompt, agent=agent, timeout=timeout)


_call_llm_with = _call_llm_with_compounding

# capture thread — mirrors the watchdog start pattern
def _start_capture():
    t = threading.Thread(target=_capture_loop, daemon=True)
    t.start()

if os.environ.get("APPVAULT_CAPTURE", "1") != "0":
    try:
        _start_capture()
    except Exception:
        pass
