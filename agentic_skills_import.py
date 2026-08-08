# =============================================================================
# SKILL PLUGIN LAYER (2026-08-08) — plug external skills (SKILL.md format, e.g.
# Claude Code / EVE skill repos) into the Agentic OS: GitHub import with
# reference files, @skill chat invocation, pipeline `skill` node, SEO final
# pass, and apply-on-demand. Spliced into agentic_plane.py. stdlib-only.
# =============================================================================

SKILL_MARKET = [
    {"name": "humanise-text", "title": "Humanise Text", "repo": "199-biotechnologies/humanise-text-skill",
     "url": "https://github.com/199-biotechnologies/humanise-text-skill",
     "desc": "Strip AI writing tells — de-slop any text, make it sound human."},
    {"name": "docx", "title": "DOCX Document Writer", "repo": "anthropics/skills",
     "url": "https://github.com/anthropics/skills/tree/main/skills/docx",
     "desc": "Create / read / format .docx documents (Anthropic official)."},
    {"name": "pdf", "title": "PDF Document Reader", "repo": "anthropics/skills",
     "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
     "desc": "Parse and extract content from PDF files (Anthropic official)."},
    {"name": "pptx", "title": "PPTX Deck Builder", "repo": "anthropics/skills",
     "url": "https://github.com/anthropics/skills/tree/main/skills/pptx",
     "desc": "Build PowerPoint decks programmatically (Anthropic official)."},
]

_IMPORT_EXCLUDE = {"README.md", "LICENSE", "CONTRIBUTING.md", ".gitignore", "LICENSE.md"}


def _http_text(url, timeout=15):
    """Full raw text fetch (SKILL.md is not JSON — _http would truncate it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "AppVault-Agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.status
    except urllib.error.HTTPError as e:
        try:
            return e.read().decode("utf-8", errors="replace"), e.code
        except Exception:
            return "", e.code
    except Exception as e:
        return "", 0


def _parse_frontmatter(text):
    """Extract name/description/allowed-tools from a SKILL.md frontmatter block."""
    fm = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return fm
    for line in m.group(1).splitlines():
        mm = re.match(r"^\s*([A-Za-z0-9\-_]+):\s*(.*)$", line)
        if mm:
            fm[mm.group(1).strip()] = mm.group(2).strip().strip('"').strip("'")
    return fm


def _github_fetch_skill(url):
    """Fetch a SKILL.md repo: locate SKILL.md, download the skill's files.
    Returns {name, description, tools, content, files:{rel:body}, source} or {error}."""
    url = (url or "").strip()
    owner = repo = subdir = None
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/[^/]+/(.*))?/?$", url)
    if m:
        owner, repo = m.group(1), m.group(2)
        subdir = (m.group(3) or "").rstrip("/")
    else:
        m2 = re.match(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+?)/[^/]+/(.*)$", url)
        if m2:
            owner, repo = m2.group(1), m2.group(2)
            parts = m2.group(3).split("/")
            subdir = "/".join(parts[:-1])
    if not owner or not repo:
        return {"error": "not a GitHub URL"}
    skill_body = None
    skill_dir = ""
    branch_used = "main"
    for branch in ("main", "master"):
        base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
        cands = [f"{subdir}/SKILL.md"] if subdir else ["SKILL.md", "skills/SKILL.md", "skill/SKILL.md"]
        for c in cands:
            body, status = _http_text(f"{base}/{c}", timeout=15)
            if status == 200 and body:
                skill_body = body
                skill_dir = "/".join(c.split("/")[:-1]) if "/" in c else ""
                branch_used = branch
                break
        if skill_body:
            break
    if not skill_body:
        return {"error": "no SKILL.md found in repo (root, skills/, or skill/ — try a raw file URL)"}

    # list the repo tree and grab the skill's own files
    files = {}
    try:
        tree_data, tstatus = _http(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch_used}?recursive=1",
                                   timeout=20)
        if tstatus == 200 and isinstance(tree_data, dict):
            prefix = skill_dir + "/" if skill_dir else ""
            count = 0
            for t in (tree_data.get("tree") or []):
                if t.get("type") != "blob":
                    continue
                path = t.get("path") or ""
                if prefix and not path.startswith(prefix):
                    continue
                rel = path[len(prefix):] if prefix else path
                base_name = rel.split("/")[-1]
                if not prefix and "/" in path:
                    continue  # root skill: only root-level files belong to it
                if base_name in _IMPORT_EXCLUDE or rel == "SKILL.md":
                    continue
                if count >= 15:
                    break
                fbody, fstatus = _http_text(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch_used}/{path}",
                                            timeout=15)
                if fstatus == 200 and fbody and len(fbody) < 100_000:
                    files[rel] = fbody
                    count += 1
    except Exception:
        pass

    fm = _parse_frontmatter(skill_body)
    body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", skill_body, count=1, flags=re.S)
    name = (fm.get("name") or "").strip() or repo
    description = (fm.get("description") or "").strip() or f"Imported skill from {owner}/{repo}"
    tools_raw = fm.get("allowed-tools") or ""
    tools = [t.strip() for t in re.split(r"[,;]", tools_raw) if t.strip()]
    # inline the markdown rule files so the LLM actually has the rules (it has
    # no file access — the prompt IS its filesystem)
    refs = []
    for rel in sorted(files):
        if rel.endswith(".md") or rel.endswith(".txt"):
            refs.append(f"\n\n---\n### File: {rel}\n\n{files[rel]}")
    content = body.strip() + "\n".join(refs)
    return {"name": name, "description": description, "tools": tools, "content": content,
            "files": files, "source": f"github:{owner}/{repo}"}


def _import_skill_from_data(data):
    """Persist an imported skill (from GitHub fetch or manual paste)."""
    name = (data.get("name") or "").strip()
    content = (data.get("content") or "").strip()
    if not name or not content:
        return None, "name + content required"
    tools = data.get("tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    files = data.get("files") or {}
    source = data.get("source") or "manual"
    # write the skill's supporting files into the vault (scripts, examples, refs)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    written = []
    if files:
        vault = _vault_path()
        base_dir = os.path.join(vault, "04_Projects", "Skills", slug)
        try:
            for rel, fbody in files.items():
                fpath = os.path.join(base_dir, rel)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(fbody)
                written.append(rel)
        except Exception:
            pass
    sid = _save_skill(name, data.get("description") or "", content, source=source, tools=tools)
    _audit("store", "skill.import", f"{name} ({source}) files={len(written)}")
    return sid, f"imported {name} + {len(written)} supporting files"


@agentic_bp.route("/api/agentic/skills/import", methods=["POST", "OPTIONS"])
def api_skill_import():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if url:
        fetched = _github_fetch_skill(url)
        if "error" in fetched:
            return jsonify({"status": "error", "error": fetched["error"]}), 400
        sid, msg = _import_skill_from_data(fetched)
        if not sid:
            return jsonify({"status": "error", "error": msg}), 400
        return jsonify({"status": "ok", "id": sid, "message": msg,
                        "skill": {"name": fetched["name"], "files": list(fetched["files"].keys())}})
    sid, msg = _import_skill_from_data(data)
    if not sid:
        return jsonify({"status": "error", "error": msg}), 400
    return jsonify({"status": "ok", "id": sid, "message": msg})


@agentic_bp.route("/api/agentic/skills/market", methods=["GET", "OPTIONS"])
def api_skill_market():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    imported = {r["name"].lower() for r in conn.execute("SELECT name FROM skills").fetchall()}
    conn.close()
    for item in SKILL_MARKET:
        item["imported"] = item["name"].lower() in imported
    return jsonify({"status": "ok", "market": SKILL_MARKET})


def _load_skill_content(skill_row):
    """Full prompt-ready skill content (SKILL.md body + inlined reference files)."""
    return (skill_row.get("content") or "").strip()


def _apply_skill(skill_row, text, timeout=90):
    """Run the LLM with the skill's instructions applied to `text`."""
    content = _load_skill_content(skill_row)
    sys_prompt = (f"You are applying the skill '{skill_row.get('name')}'. Follow its instructions "
                  f"EXACTLY. Output only the result (the transformed text), no preamble.\n\n"
                  f"===== SKILL =====\n{content}")
    return _call_llm_with({}, f"Apply the skill to this text:\n\n{text}",
                          system_prompt=sys_prompt, agent="hermes", timeout=timeout)


@agentic_bp.route("/api/agentic/skills/<int:sid>/apply", methods=["POST", "OPTIONS"])
def api_skill_apply(sid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    conn = _db()
    row = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "skill not found"}), 404
    conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    try:
        out = _apply_skill(dict(row), text)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)[:200]}), 502
    _audit("store", "skill.apply", f"{row['name']} applied to {len(text)} chars")
    return jsonify({"status": "ok", "skill": row["name"], "result": out})


def _maybe_extract_skill(msg):
    """If the message starts with @<skillname>, return (rest_of_message, skill_row)."""
    msg = (msg or "").strip()
    m = re.match(r"^@([\w\- ]+?)\s+(.+)$", msg, re.S)
    if not m:
        return msg, None
    name = m.group(1).strip()
    try:
        conn = _db()
        row = conn.execute("SELECT * FROM skills WHERE lower(name)=lower(?) LIMIT 1", (name,)).fetchone()
        conn.close()
    except Exception:
        row = None
    if row:
        return m.group(2).strip(), dict(row)
    return msg, None
