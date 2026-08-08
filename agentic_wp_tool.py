# =============================================================================
# WORDPRESS TOOL (2026-08-08) — the first REAL external tool: WordPress REST
# API publishing via Application Passwords. Wired as: an action-skill (chat
# @wordpress-publishing executes the publish instead of an LLM pass), a
# pipeline node, an SEO final-publish flag, and a Gov-page config card.
# stdlib-only; Basic auth; every route has an OPTIONS guard.
# =============================================================================
import base64

def _wp_config():
    raw = _cfg_get("wp_tool") or ""
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _wp_save_config(patch):
    cfg = _wp_config()
    for k in ("site_url", "username", "app_password"):
        if patch.get(k) is not None:
            cfg[k] = str(patch.get(k)).strip()
    _cfg_set("wp_tool", json.dumps(cfg))
    _audit("store", "wp.config", "wordpress tool config saved")
    return cfg


def _wp_auth_headers():
    cfg = _wp_config()
    user = (cfg.get("username") or "").strip()
    pw = (cfg.get("app_password") or "").strip()
    if not user or not pw:
        return None
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _wp_publish(title, content, status="publish"):
    """Create a WordPress post via the REST API. Returns (ok, result)."""
    cfg = _wp_config()
    site = (cfg.get("site_url") or "").strip().rstrip("/")
    if not site:
        return False, "not configured — add site URL in 🛡️ Gov → WordPress Publisher"
    hdrs = _wp_auth_headers()
    if not hdrs:
        return False, "not configured — add username + app password in 🛡️ Gov → WordPress Publisher"
    data, code = _http(f"{site}/wp-json/wp/v2/posts", method="POST", headers=hdrs,
                       json_data={"title": str(title)[:200], "content": str(content),
                                  "status": status if status in ("publish", "draft", "pending", "private") else "draft"},
                       timeout=40)
    if code in (200, 201) and isinstance(data, dict):
        return True, {"id": data.get("id"), "link": data.get("link"),
                      "status": data.get("status"), "title": (data.get("title") or {}).get("rendered", title)}
    return False, f"HTTP {code}: {str(data)[:300]}"


def _wp_test():
    """Verify credentials: GET /wp-json/wp/v2/posts?per_page=1."""
    cfg = _wp_config()
    site = (cfg.get("site_url") or "").strip().rstrip("/")
    if not site:
        return False, "no site URL configured"
    hdrs = _wp_auth_headers()
    if not hdrs:
        return False, "no credentials configured"
    data, code = _http(f"{site}/wp-json/wp/v2/posts?per_page=1", headers=hdrs, timeout=25)
    if code == 200:
        n = len(data) if isinstance(data, list) else "?"
        return True, f"auth OK — API reachable (sample: {n} post)"
    return False, f"HTTP {code}: {str(data)[:300]}"


def _parse_wp_payload(msg):
    """Parse 'title: X\\ncontent' | JSON payload | plain content. Returns (title, content, status)."""
    msg = (msg or "").strip()
    status = "publish"
    try:
        obj = json.loads(msg)
        if isinstance(obj, dict):
            return str(obj.get("title") or "")[:200], str(obj.get("content") or ""), str(obj.get("status") or "publish")
    except Exception:
        pass
    lines = msg.split("\n", 1)
    first = lines[0].strip()
    m = re.match(r"^title:\s*(.+)$", first, re.I)
    if m and len(lines) > 1:
        return m.group(1).strip()[:200], lines[1].strip(), status
    if m:
        return m.group(1).strip()[:200], "", status
    return first[:80], msg, status


def _action_wp_publish(msg):
    """Action-skill handler for @wordpress-publishing in chat."""
    title, content, status = _parse_wp_payload(msg)
    if not content:
        return ("⚠️ Nothing to publish — send the article content after @wordpress-publishing "
                "(or use `title: My post` + content lines).")
    ok, res = _wp_publish(title or f"Post from AppVault {datetime.now().strftime('%Y-%m-%d %H:%M')}", content, status)
    _audit("chat", "wp.publish", f"{'ok' if ok else 'failed'} :: {res.get('link', str(res)[:120]) if isinstance(res, dict) else str(res)[:120]}")
    if ok:
        return (f"✅ **Published to WordPress** — [post #{res.get('id')}] ({res.get('link')}) "
                f"· status: {res.get('status')}")
    return f"⚠️ WordPress publish failed: {res}"


def _run_skill_action(skill_row, msg):
    """If the skill is an ACTION skill, execute its backend handler (else None)."""
    if (skill_row or {}).get("kind") != "action":
        return None
    handler = _ACTION_SKILL_HANDLERS.get((skill_row.get("name") or "").lower())
    if not handler:
        return None
    try:
        return handler(msg)
    except Exception as e:
        return f"⚠️ Skill action failed: {str(e)[:200]}"


_ACTION_SKILL_HANDLERS = {
    "wordpress publishing": _action_wp_publish,
    "wordpress-publishing": _action_wp_publish,
}


def _seed_wp_skill():
    """Replace the test stub with the REAL WordPress publishing skill (no uses bump)."""
    real_content = (
        "# WordPress Publishing\n\n"
        "Publish articles to your WordPress site through the built-in WordPress tool "
        "(REST API + Application Passwords).\n\n"
        "## When to Use\n- User asks to publish an article, blog post, or piece of content to WordPress\n"
        "- A generated draft should go live (SEO articles, Oracle posts)\n"
        "- A pipeline ends with 'publish to WordPress'\n\n"
        "## How It Works\n"
        "The tool is configured in 🛡️ Gov → WordPress Publisher (site URL, username, app password). "
        "You do NOT need to write any API code — publishing is executed for you.\n\n"
        "## Workflow\n"
        "1. Accept the article title + full content (markdown or HTML).\n"
        "2. If only raw text is given, derive a title from the first line.\n"
        "3. Publish via the WordPress tool; report the post link + status back.\n"
        "4. Never fabricate a published URL — only report what the tool returns.\n\n"
        "## Environment Note\n"
        "You are in a text-only agent. Do not attempt to call WordPress APIs yourself — "
        "the tool runs for you. Just present title + content; the publish happens automatically.\n"
    )
    conn = _db()
    row = conn.execute("SELECT id FROM skills WHERE lower(name)=lower('wordpress-publishing')").fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if row:
        conn.execute("UPDATE skills SET content=?, description=?, kind='action', source='builtin:wordpress', "
                     "tools='wordpress', updated=? WHERE id=?",
                     (real_content, "Publish articles to WordPress via the built-in REST tool (action skill — "
                      "@wordpress-publishing <content> publishes directly).", now, row["id"]))
    else:
        conn.execute("INSERT INTO skills (name, description, content, source, tools, kind, uses, created, updated) "
                     "VALUES (?,?,?,?,?,?,0,?,?)",
                     ("WordPress publishing", "Publish articles to WordPress via the built-in REST tool (action "
                      "skill — @wordpress-publishing <content> publishes directly).",
                      real_content, "builtin:wordpress", "wordpress", "action", now, now))
    conn.commit()
    conn.close()


# kind column for skills (prompt | action)
def _migrate_skills_kind():
    conn = _db()
    try:
        conn.execute("ALTER TABLE skills ADD COLUMN kind TEXT DEFAULT 'prompt'")
        conn.commit()
    except Exception:
        pass
    conn.close()


_migrate_skills_kind()
_seed_wp_skill()


@agentic_bp.route("/api/agentic/tools/wordpress/config", methods=["GET", "POST", "OPTIONS"])
def api_wp_config():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = _wp_save_config(data)
        return jsonify({"status": "ok", "config": {
            "site_url": cfg.get("site_url", ""),
            "username": cfg.get("username", ""),
            "app_password": ("********" if cfg.get("app_password") else ""),
        }})
    cfg = _wp_config()
    return jsonify({"status": "ok", "config": {
        "site_url": cfg.get("site_url", ""),
        "username": cfg.get("username", ""),
        "app_password": ("********" if cfg.get("app_password") else ""),
    }})


@agentic_bp.route("/api/agentic/tools/wordpress/test", methods=["POST", "OPTIONS"])
def api_wp_test():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    ok, res = _wp_test()
    _audit("store", "wp.test", "ok" if ok else f"failed: {res[:120]}")
    return jsonify({"status": "ok" if ok else "error", "detail": res})


@agentic_bp.route("/api/agentic/tools/wordpress/publish", methods=["POST", "OPTIONS"])
def api_wp_publish():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    ok, res = _wp_publish(data.get("title") or "Post from AppVault", content,
                          (data.get("status") or "publish"))
    _audit("store", "wp.publish", f"{'ok' if ok else 'failed'} :: {res.get('link', str(res)[:150]) if isinstance(res, dict) else str(res)[:150]}")
    if ok:
        return jsonify({"status": "ok", "post_id": res.get("id"), "link": res.get("link"),
                        "post_status": res.get("status")})
    return jsonify({"status": "error", "error": res}), 502
