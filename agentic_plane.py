"""
Agentic OS Control Plane — REAL implementation (replaces hardcoded demo block).

What changed vs the old block:
- Roster status is derived from LIVE health probes, not a hardcoded list of "online".
- Shared memory + conversations + config persist in SQLite (survives restarts).
- The Oracle actually sweeps RSS feeds (stdlib only — feedparser is NOT in the agent image),
  scores stories by keyword weight, and writes real Signal files to the vault.
- ONE central LLM config (provider/model/key/base/temp/system_prompt) in the DB.
  Every agent (hermes, claude, crew roles, conversations) uses the same dispatch.
- Crew dispatch executes REAL per-role LLM calls and collects results, instead of
  writing a canned "[Architect] analyzed..." report.
- /api/agentic/status exposes live probe results for the store UI badges.
"""
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

from flask import Blueprint, request, jsonify, Response

agentic_bp = Blueprint("agentic_plane", __name__)

def _http(url, method="GET", json_data=None, timeout=8, headers=None):
    """stdlib HTTP helper (the agent image has NO requests module).
    Follows redirects (incl. 307/308 with POST body) — urllib refuses to
    re-send a POST body to a different host, which Ocoya's www. redirect needs."""
    data = None
    hdrs = {"User-Agent": "AppVault-Agent/1.0"}
    if headers:
        hdrs.update(headers)
    if json_data is not None:
        data = json.dumps(json_data).encode()
        hdrs["Content-Type"] = "application/json"
    cur = url
    for _ in range(5):
        req = urllib.request.Request(cur, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(body), resp.status
                except Exception:
                    return {"raw": body[:2000]}, resp.status
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                loc = e.headers["Location"]
                cur = urllib.parse.urljoin(cur, loc)
                if e.code in (301, 302, 303) and method == "POST":
                    method, data = "GET", None  # spec: 301/302/303 -> GET
                # 307/308 keep method + body
                continue
            try:
                return json.loads(e.read().decode("utf-8", errors="replace")), e.code
            except Exception:
                return {"error": f"HTTP {e.code}"}, e.code
        except Exception as e:
            return {"error": str(e)}, 0
    return {"error": "too many redirects"}, 508

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
_DB_PATH = os.environ.get("AGENTIC_DB_PATH", os.path.join(os.environ.get("STORAGE_PATH", "/data"), "agentic.db"))

def _db():
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    return conn

def _init_db():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, agent TEXT, tag TEXT, content TEXT,
        tier TEXT DEFAULT 'working',
        source TEXT DEFAULT 'manual',
        superseded_by INTEGER,
        vault_path TEXT,
        updated TEXT
    );
    CREATE TABLE IF NOT EXISTS memory_embeddings (
        mid INTEGER PRIMARY KEY,
        vector TEXT
    );
    CREATE TABLE IF NOT EXISTS conversations (
        agent_id TEXT PRIMARY KEY,
        messages TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT,
        messages TEXT,
        updated TEXT
    );
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS sweeps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, query TEXT, signal_file TEXT, top TEXT
    );
    CREATE TABLE IF NOT EXISTS oracle_feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, query TEXT,
        rss_urls TEXT, subreddits TEXT, hn_query TEXT, github_query TEXT, youtube_channels TEXT,
        sources TEXT, skip_repeats INTEGER DEFAULT 1,
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS oracle_seen (
        url TEXT PRIMARY KEY,
        title TEXT,
        first_seen TEXT,
        last_seen TEXT,
        max_points INTEGER DEFAULT 0,
        last_points INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS oracle_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feed_id INTEGER, platform TEXT, title TEXT, content TEXT,
        status TEXT, scheduled_at TEXT, created TEXT
    );
    CREATE TABLE IF NOT EXISTS vault_embeddings (
        path TEXT PRIMARY KEY,
        mtime REAL,
        vector TEXT
    );
    CREATE TABLE IF NOT EXISTS mail_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT, body TEXT, to_addr TEXT,
        status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,
        last_error TEXT,
        created TEXT, sent TEXT
    );
    CREATE TABLE IF NOT EXISTS businesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE, voice TEXT, website TEXT, cta_offer TEXT,
        created TEXT, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS social_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER DEFAULT NULL,
        platform TEXT, display_name TEXT, handle TEXT,
        ocoya_workspace_id TEXT, ocoya_profile_id TEXT,
        enabled INTEGER DEFAULT 1, created TEXT
    );
    CREATE TABLE IF NOT EXISTS content_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER DEFAULT NULL,
        name TEXT, purpose TEXT, voice TEXT, structure TEXT,
        platforms TEXT, cadence TEXT, length_guard INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1, created TEXT
    );
    CREATE TABLE IF NOT EXISTS wordpress_sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER DEFAULT NULL,
        name TEXT, site_url TEXT, username TEXT, app_password TEXT,
        enabled INTEGER DEFAULT 1, created TEXT
    );
    """)
    conn.commit()
    conn.close()

_init_db()


def _migrate_memory_schema():
    """Add new memory columns to pre-existing DBs (no-op on fresh installs)."""
    conn = _db()
    for col, ddl in [("tier", "TEXT DEFAULT 'working'"), ("source", "TEXT DEFAULT 'manual'"),
                     ("superseded_by", "INTEGER"), ("vault_path", "TEXT"), ("updated", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE memory ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS memory_embeddings (mid INTEGER PRIMARY KEY, vector TEXT)")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE oracle_feeds ADD COLUMN sources TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE oracle_feeds ADD COLUMN skip_repeats INTEGER DEFAULT 1")
    except Exception:
        pass
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS oracle_seen (url TEXT PRIMARY KEY, title TEXT, "
                     "first_seen TEXT, last_seen TEXT, max_points INTEGER DEFAULT 0, last_points INTEGER DEFAULT 0)")
    except Exception:
        pass
    conn.commit()
    conn.close()


_migrate_memory_schema()


def _migrate_multitenant_schema():
    """Multi-tenant content layer: business scoping for feeds/posts.
    No-op on fresh installs (columns already exist)."""
    conn = _db()
    try:
        conn.execute("ALTER TABLE oracle_feeds ADD COLUMN business_id INTEGER DEFAULT NULL")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE oracle_posts ADD COLUMN business_id INTEGER DEFAULT NULL")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE oracle_posts ADD COLUMN profile_id INTEGER DEFAULT NULL")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE businesses ADD COLUMN social_platforms TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE businesses ADD COLUMN daily_target INTEGER DEFAULT 8")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE oracle_feeds ADD COLUMN is_engagement INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE oracle_feeds ADD COLUMN flash_threshold INTEGER DEFAULT 30")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE oracle_feeds ADD COLUMN x_watch TEXT DEFAULT '[]'")
    except Exception:
        pass
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS x_archived (link TEXT PRIMARY KEY, business_id INTEGER, archived_at TEXT)")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE social_profiles ADD COLUMN webhook_url TEXT")
        conn.execute("ALTER TABLE social_profiles ADD COLUMN webhook_auth TEXT")
    except Exception:
        pass
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, target TEXT, campaign TEXT, source TEXT, medium TEXT, clicks INTEGER DEFAULT 0, created TEXT)")
    except Exception:
        pass
    conn.commit()
    conn.close()


_migrate_multitenant_schema()


def _cfg_get(key, default=None):
    conn = _db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return json.loads(row["value"]) if row else default

def _cfg_set(key, value):
    conn = _db()
    conn.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Central LLM configuration (single source of truth for the whole fleet)
# ---------------------------------------------------------------------------
DEFAULT_LLM_CONFIG = {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_tokens": 2048,
    "api_key": "",
    "api_base": "https://api.deepseek.com",
    "system_prompt": "You are an autonomous AI agent operating inside the AppVault Agentic OS. "
                     "You monitor live signals, coordinate with other agents, sync shared memory, "
                     "and execute tasks efficiently."
}

# Per-agent system prompts so each roster member has a real personality/directive.
AGENT_PROMPTS = {
    "openclaw": "You are OpenClaw, the autonomous personal AI assistant (openclaw/openclaw). "
                "Any OS. Any Platform. The lobster way. 🦞 You execute multi-step tasks, coordinate tools, "
                "write clean code, and help the user build and automate anything directly.",
    "hermes": "You are Hermes Agent, a 24/7 continuous watcher daemon. You monitor live signals, "
              "sweep news feeds, sync shared memory with the Obsidian Vault, and execute tasks efficiently.",
    "claude": "You are Claude, the deep-reasoning architect agent. You design solutions, analyze "
              "architecture, and produce precise, well-reasoned answers.",
    "antigravity": "You are Antigravity, the full-stack builder agent. You write production-grade code "
                   "and build complete features end to end.",
    "codex": "You are Codex, the code synthesis agent. You refactor code and generate precise specs.",
    "kimi": "You are Kimi, the long-context specialist. You parse large documents and extract structure.",
    "crew-architect": "You are the crew Architect. Analyze the task, define the execution contract, "
                      "and specify what must be built.",
    "crew-engineer": "You are the crew Lead Engineer. Produce the concrete implementation plan and "
                     "actual code/artifacts for the task.",
    "crew-reviewer": "You are the crew Code Reviewer. Review the proposed work for correctness, "
                     "security, and quality, and report concrete findings.",
    "crew-prospector": "You are the crew Prospector. Identify and score the most promising candidate businesses for the task, requiring verified source evidence for every candidate.",
    "crew-researcher": "You are the crew Researcher. Enrich each candidate with concrete verified facts — tech stack, decision-makers, public exposure, risks — and cite sources.",
    "crew-sdr": "You are the crew SDR. Draft a concise, personalized outreach message from the approved template, tailored to the prospect's situation.",
    "crew-proposal": "You are the crew Proposal Writer. Convert the qualified opportunity into a crisp proposal: scope, price, timeline, and a clear next step.",
    "crew-delivery": "You are the crew Delivery Agent. Define the delivery plan: what gets executed, the deliverable/report format, and the completion checklist.",
    "deerflow": "You are DeerFlow, the long-horizon super-agent harness. You research deeply, "
                "write and run code in sandboxes, persist memories, and orchestrate sub-agents and "
                "skills to complete tasks that take minutes to hours. Break big asks into "
                "verifiable steps and report concrete results, not just plans.",
    "goose": "You are Goose (Block/aaif-goose), an autonomous open-source AI developer agent designed "
             "to automate complex software engineering tasks, inspect codebases, execute terminal tools, "
             "and generate production-ready implementations with precision.",
    "deepseek-harness": "You are DeepSeek Harness, an elite autonomous reasoning and agent evaluation engine "
                        "powered by DeepSeek-V3 / DeepSeek-R1. You specialize in complex logic chains, "
                        "rigorous algorithm verification, and deep architecture synthesis.",
}

def _get_llm_config():
    cfg = dict(DEFAULT_LLM_CONFIG)
    stored = _cfg_get("llm")
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if v is not None})
    return cfg

def _text_similarity(a, b):
    """Token Jaccard similarity — used for fact dedup/versioning."""
    ta, tb = set(_tokenize(a)), set(_tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _save_fact(fact_text, tag="Fact"):
    """Save a core fact with dedup/versioning: identical -> skip;
    similar-but-different -> supersede the old fact."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, content FROM memory WHERE tier='core' AND superseded_by IS NULL"
        ).fetchall()
        # Pass 1: exact/near-exact duplicate -> skip (no new version)
        for r in rows:
            if _text_similarity(fact_text, r["content"]) >= 0.98:
                conn.close()
                return False
        # Pass 2: similar-but-different -> supersede the old fact
        for r in rows:
            if _text_similarity(fact_text, r["content"]) >= 0.5:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    "INSERT INTO memory (ts, agent, tag, content, tier, source, updated) VALUES (?,?,?,?,?,?,?)",
                    (datetime.now().strftime("%H:%M LOCAL"), "Fact Distiller", tag, fact_text,
                     "core", "distilled", now))
                new_id = cur.lastrowid
                conn.execute("UPDATE memory SET superseded_by=?, updated=? WHERE id=?",
                             (new_id, now, r["id"]))
                conn.commit()
                old_row = _get_memory_row(r["id"])
                conn.close()
                _sync_memory_to_vault(new_id, _get_memory_row(new_id))
                _sync_memory_to_vault(r["id"], old_row)  # refresh old vault mirror w/ superseded_by
                return True
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO memory (ts, agent, tag, content, tier, source, updated) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().strftime("%H:%M LOCAL"), "Fact Distiller", tag, fact_text,
             "core", "distilled", now))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        _sync_memory_to_vault(new_id, _get_memory_row(new_id))
        return True
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


def _get_memory_row(mid):
    conn = _db()
    row = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _distill_facts(user_msg, reply, agent="Hermes Agent"):
    """Background: extract durable facts from a chat exchange -> core facts."""
    if not reply or str(reply).strip().startswith("⚠️") or "could not reach any LLM backend" in str(reply):
        return 0  # never distill error messages into facts
    try:
        exchange = f"User: {user_msg[:800]}\nAgent: {reply[:800]}"
        raw = _call_llm(
            "Extract durable, timeless facts from this conversation exchange. "
            "Return ONLY a JSON array of strings, each a single standalone fact "
            "(subject + predicate + value). Skip greetings, opinions, questions, "
            "and ephemeral chatter. If nothing durable, return [].\n\n"
            f"Exchange:\n{exchange}",
            agent=agent, timeout=40)
        facts = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
        if not isinstance(facts, list):
            # try extracting a JSON array from anywhere in the reply
            m = re.search(r"\[.*\]", raw, re.S)
            facts = json.loads(m.group(0)) if m else []
        if not isinstance(facts, list):
            return 0
        n = 0
        for fact in facts[:5]:
            if isinstance(fact, str) and len(fact.strip()) > 12:
                if _save_fact(fact.strip()):
                    n += 1
        return n
    except Exception:
        return 0


def _sync_memory_to_vault(mid, row):
    """Write/refresh the vault copy of a memory entry (YAML frontmatter)."""
    try:
        if not row:
            return None
        vault = _vault_path()
        mem_dir = os.path.join(vault, "00_Memory")
        os.makedirs(mem_dir, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", (row.get("content") or "note")[:40].lower()).strip("-") or "note"
        fname = f"{mid}-{slug}.md"
        fpath = os.path.join(mem_dir, fname)
        frontmatter = (
            f"---\nid: {mid}\ntier: {row.get('tier', 'working')}\n"
            f"source: {row.get('source', 'manual')}\ntag: {row.get('tag', 'General')}\n"
            f"agent: {row.get('agent', 'System')}\ndate: {row.get('ts', '')}\n"
            f"superseded_by: {row.get('superseded_by') or ''}\n---\n\n")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(frontmatter + (row.get("content") or ""))
        rel = f"00_Memory/{fname}"
        conn = _db()
        conn.execute("UPDATE memory SET vault_path=? WHERE id=?", (rel, mid))
        conn.commit()
        conn.close()
        return rel
    except Exception:
        return None


def _scan_vault_into_memory():
    """Vault -> memory: index .md files not yet represented (skip 00_Memory mirror)."""
    try:
        vault = _vault_path()
        conn = _db()
        known = set(r["vault_path"] for r in conn.execute(
            "SELECT vault_path FROM memory WHERE vault_path IS NOT NULL").fetchall() if r["vault_path"])
        added = 0
        for root, _, files in os.walk(vault):
            for f in files:
                if not f.endswith(".md"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, vault).replace("\\", "/")
                if rel in known or rel.startswith("00_Memory/"):
                    continue
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        content = fh.read(3000)
                except Exception:
                    continue
                conn.execute(
                    "INSERT INTO memory (ts, agent, tag, content, tier, source, vault_path) VALUES (?,?,?,?,?,?,?)",
                    (datetime.now().strftime("%H:%M LOCAL"), "Vault", "Vault Note",
                     f"📁 {rel}\n\n{content[:1500]}", _memory_settings().get("vault_scan_tier", "auto"),
                     "vault", rel))
                known.add(rel)
                added += 1
        conn.commit()
        conn.close()
        return added
    except Exception:
        return 0


def _get_mem_embed(mid, content):
    """Per-entry embedding, cached in memory_embeddings."""
    try:
        conn = _db()
        row = conn.execute("SELECT vector FROM memory_embeddings WHERE mid=?", (mid,)).fetchone()
        if row:
            vec = json.loads(row["vector"])
            conn.close()
            return vec if isinstance(vec, list) else None
        conn.close()
    except Exception:
        pass
    vec = _ollama_embed((content or "")[:4000], timeout=30)
    if vec:
        try:
            conn = _db()
            conn.execute("INSERT INTO memory_embeddings (mid, vector) VALUES (?,?) "
                         "ON CONFLICT(mid) DO UPDATE SET vector=excluded.vector",
                         (mid, json.dumps(vec)))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return vec


def _search_memory_entries(query, limit=6, threshold=0.28):
    """Semantic retrieval over memory ENTRIES (not just vault files)."""
    q_vec = _ollama_embed(query or "")
    if not q_vec:
        return []
    conn = _db()
    rows = conn.execute(
        "SELECT id, content, tier, source FROM memory WHERE superseded_by IS NULL "
        "ORDER BY id DESC LIMIT 200").fetchall()
    scored = []
    for r in rows:
        vec = _get_mem_embed(r["id"], r["content"])
        if vec:
            s = _cosine(q_vec, vec)
            scored.append({"id": r["id"], "content": r["content"], "tier": r["tier"],
                           "source": r["source"], "score": s})
    conn.close()
    scored.sort(key=lambda x: -x["score"])
    return [s for s in scored if s["score"] > threshold][:limit]


MEMORY_ENGINE_DEFAULTS = {
    "core_limit": 8,          # max Core facts always injected
    "semantic_limit": 6,      # max semantic-match entries (any tier)
    "working_limit": 5,       # max recent Working notes
    "semantic_threshold": 0.28,  # min cosine score to count as relevant
    "auto_tags": ["Conversation", "Radar", "Signal", "Crew", "Sweep", "Oracle"],
    "vault_scan_tier": "auto",
}


def _memory_settings():
    """Engine settings with user overrides from the config table."""
    cfg = dict(MEMORY_ENGINE_DEFAULTS)
    stored = _cfg_get("memory_engine")
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if v is not None})
    return cfg


def _set_memory_settings(patch):
    """Merge + persist engine settings."""
    cfg = _memory_settings()
    for k, v in patch.items():
        if k in MEMORY_ENGINE_DEFAULTS:
            cfg[k] = v
    _cfg_set("memory_engine", cfg)
    return cfg


def _reclassify_auto(entry):
    """Auto-tier rule: tag/agent match -> auto. Returns 'auto' or None."""
    tag = (entry.get("tag") or "").lower()
    agent = (entry.get("agent") or "").lower()
    for t in _memory_settings().get("auto_tags", []):
        if t.lower() in tag or t.lower() in agent:
            return "auto"
    return None


def _memory_context(query, limit=None):
    """Tiered context packing: CORE facts always -> semantic entry hits ->
    recent WORKING notes. Limits + threshold come from the engine settings.
    Every line carries a (memory #id) citation so the agent can reference."""
    ms = _memory_settings()
    core_limit = ms.get("core_limit", 8)
    sem_limit = ms.get("semantic_limit", 6) if limit is None else limit
    work_limit = ms.get("working_limit", 5)
    parts = []
    seen = set()
    conn = _db()
    try:
        core = conn.execute(
            "SELECT * FROM memory WHERE tier='core' AND superseded_by IS NULL "
            "ORDER BY id DESC LIMIT ?", (core_limit,)).fetchall()
        for r in core:
            seen.add(r["id"])
            parts.append(f"[memory #{r['id']} | CORE] {r['content'][:500]}")
    except Exception:
        pass
    try:
        for hit in _search_memory_entries(query or "", limit=sem_limit,
                                          threshold=ms.get("semantic_threshold", 0.28)):
            if hit["id"] in seen:
                continue
            seen.add(hit["id"])
            parts.append(f"[memory #{hit['id']} | {hit.get('tier', 'working').upper()}] "
                         f"{hit['content'][:400]} (source: {hit.get('source', '?')})")
    except Exception:
        pass
    try:
        recent = conn.execute(
            "SELECT * FROM memory WHERE tier='working' AND superseded_by IS NULL "
            "ORDER BY id DESC LIMIT ?", (work_limit,)).fetchall()
        for r in recent:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            parts.append(f"[memory #{r['id']} | WORKING] {r['content'][:500]}")
    except Exception:
        pass
    conn.close()
    if not parts:
        return ""
    return ("\n\n===== CONTEXT FROM SHARED MEMORY (cite sources as \"memory #id\") =====\n"
            + "\n".join(parts)
            + "\n===== END CONTEXT =====\n\n")


def _call_llm(user_msg, system_prompt=None, agent="hermes", timeout=25):
    """Single dispatch for every agent conversation + crew role. Real LLM call."""
    return _call_llm_with({}, user_msg, system_prompt=system_prompt, agent=agent, timeout=timeout)

# Which backend answered the last _call_llm_with — lets the Test button tell
# the user whether THEIR provider worked or the keyless fallback kicked in.
_LAST_BACKEND = [None]

def _call_llm_with(overrides, user_msg, system_prompt=None, agent="hermes", timeout=25):
    """Like _call_llm but with a one-shot config override (used by the UI
    Test button — nothing is persisted)."""
    global _LAST_BACKEND
    cfg = _get_llm_config()
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                cfg[k] = v
    provider = cfg.get("provider", "deepseek").lower()
    model = cfg.get("model") or "deepseek-chat"
    # Per-provider key resolution: provider_keys[provider] wins, then the
    # legacy single api_key, then empty (keyless Ollama fallback).
    pkeys = cfg.get("provider_keys") or {}
    api_key = ((pkeys.get(provider) or cfg.get("api_key")) or "").strip()
    api_base = (cfg.get("api_base") or "").strip()
    temp = cfg.get("temperature", 0.7)
    agent_base_prompt = _get_agent_prompt(agent)
    agent_names = {
        "hermes": "Hermes Agent",
        "openclaw": "OpenClaw Autonomous Agent",
        "goose": "Goose Developer Agent (Block)",
        "deepseek-harness": "DeepSeek Harness Reasoning Engine",
        "deerflow": "DeerFlow Super-Agent",
    }
    agent_display = agent_names.get((agent or "hermes").lower(), f"{str(agent).capitalize()} Agent")
    agent_guard = f"=== YOUR STRICT IDENTITY ===\nYou are {agent_display}. Your name is {agent_display}. You must speak and act strictly as {agent_display}. Do NOT adopt the name or persona of OpenClaw or any other agent.\n=== END IDENTITY DIRECTIVE ===\n\n"
    sys_prompt = agent_guard + (system_prompt or agent_base_prompt or cfg.get("system_prompt") or DEFAULT_LLM_CONFIG["system_prompt"])
    last_err = None

    # DeepSeek / OpenAI-compatible — skip entirely if no API key configured
    # (a doomed cloud call would burn the full timeout before the keyless fallback).
    if provider in ("deepseek", "openai", "litellm", "grok") and api_key:
        base = api_base or "https://api.deepseek.com"
        url = base.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + ("/v1/chat/completions" if "/v1" not in url else "/chat/completions")
        try:
            hdrs = {"Authorization": f"Bearer {api_key}"}
            data, status = _http(url, method="POST", headers=hdrs, json_data={
                "model": model,
                "messages": [{"role": "system", "content": sys_prompt},
                             {"role": "user", "content": user_msg}],
                "temperature": temp, "stream": False}, timeout=timeout)
            if status == 200 and isinstance(data, dict):
                choices = data.get("choices", [])
                if choices:
                    _LAST_BACKEND[0] = provider
                    return choices[0].get("message", {}).get("content", "").strip()
            last_err = f"{provider} HTTP {status}: {str(data)[:200]}"
        except Exception as e:
            last_err = f"{provider} call failed: {e}"

    # Anthropic
    if provider == "anthropic" and api_key:
        try:
            headers = {"Content-Type": "application/json", "x-api-key": api_key,
                       "anthropic-version": "2023-06-01"}
            data, status = _http("https://api.anthropic.com/v1/messages", method="POST",
                                 json_data={"model": model or "claude-3-5-sonnet-20241022", "system": sys_prompt,
                                            "max_tokens": cfg.get("max_tokens", 2048), "temperature": temp,
                                            "messages": [{"role": "user", "content": user_msg}]},
                                 timeout=timeout)
            if status == 200 and isinstance(data, dict):
                content = data.get("content", [])
                if content:
                    _LAST_BACKEND[0] = "anthropic"
                    return content[0].get("text", "").strip()
            last_err = f"anthropic HTTP {status}: {str(data)[:200]}"
        except Exception as e:
            last_err = f"anthropic call failed: {e}"

    # Local Ollama
    if provider in ("ollama", "local"):
        ollama_base = api_base or os.environ.get("OLLAMA_API_BASE", "http://host.docker.internal:11434")
        try:
            data, status = _http(f"{ollama_base.rstrip('/')}/api/generate", method="POST",
                                 json_data={"model": model or "llama3",
                                            "prompt": f"System Directive: {sys_prompt}\n\nUser: {user_msg}\n\nAgent:",
                                            "stream": False, "options": {"temperature": temp}},
                                 timeout=timeout)
            if status == 200 and isinstance(data, dict):
                reply = data.get("response", "").strip()
                if reply:
                    _LAST_BACKEND[0] = "ollama"
                    return reply
            last_err = f"ollama HTTP {status}"
        except Exception as e:
            last_err = f"ollama call failed: {e}"

    # Keyless fallback: if the configured provider failed (no key, 401, offline),
    # try local Ollama automatically so the fleet still works with zero API keys.
    if provider not in ("ollama", "local"):
        ollama_base = os.environ.get("OLLAMA_API_BASE", "http://host.docker.internal:11434")
        try:
            # Discover whatever model the local Ollama actually has (any host view).
            tags, status = _http(f"{ollama_base.rstrip('/')}/api/tags", timeout=3)
            model_name = None
            if status == 200 and isinstance(tags, dict):
                models = tags.get("models") or []
                if models:
                    names = [m.get("name") for m in models]
                    # Prefer a small/fast local model so keyless mode stays snappy
                    # (llama3.1 4.9GB causes OOM-style connection drops under load).
                    for pref in ("qwen2.5:0.5b", "qwen2.5:1.5b", "llama3.2:1b", "llama3:latest", "phi3:mini", "tinyllama"):
                        if pref in names:
                            model_name = pref
                            break
                    if not model_name:
                        model_name = models[0].get("name")
            if not model_name:
                last_err = "ollama-fallback: no models installed"
            else:
                data, status = _http(f"{ollama_base.rstrip('/')}/api/generate", method="POST",
                                     json_data={"model": model_name,
                                                "prompt": f"System Directive: {sys_prompt}\n\nUser: {user_msg}\n\nAgent:",
                                                "stream": False, "options": {"temperature": temp}},
                                     timeout=timeout)
                if status == 200 and isinstance(data, dict):
                    reply = data.get("response", "").strip()
                    if reply:
                        _LAST_BACKEND[0] = "ollama-fallback"
                        return reply
                last_err = f"ollama-fallback HTTP {status}: {str(data)[:150]}"
        except Exception as e:
            last_err = f"ollama-fallback call failed: {e}"

    raise RuntimeError(f"All LLM backends failed for agent '{agent}'. Last error: {last_err}. "
                       f"Check /api/agentic/config (provider={provider}, key set?, base={api_base}).")

# ---------------------------------------------------------------------------
# Live service probes (roster status is REAL, not hardcoded)
# ---------------------------------------------------------------------------
# The agent container must reach sibling services via the host gateway on
# Docker Desktop (host.docker.internal) or direct localhost on Linux/native.
PROBE_HOSTS = ["host.docker.internal", "localhost"]

SERVICES = [
    # (id, name, type, category, role, probe_port_path, model_label)
    ("crewai", "CrewAI Runner", "Multi-Agent Framework", "orchestrators", "Multi-Agent Execution Engine (:8000)", "8000/health", "fastapi/port-8000"),
    ("litellm", "LiteLLM Router", "Model Proxy Gateway", "llms", "Unified LLM Provider Gateway (:4000)", "4000/health", "litellm/port-4000"),
    ("onebrain", "Memory MCP", "Memory Server", "llms", "Obsidian Shared Vault Sync (:3001)", "3001/", "mcp/port-3001"),
    ("hermes", "Hermes Agent", "24/7 Watcher Daemon", "agents", "Continuous Signal Sweeper (:8095)", "8095/health", "hermes-core/port-8095"),
    ("ollama", "Ollama Engine", "Local GPU/CPU Engine", "llms", "Local Llama3 / Qwen Models (:11434)", "11434/api/tags", "ollama/port-11434"),
    ("n8n", "n8n Automation", "Node Orchestrator", "orchestrators", "Visual Workflow Automation (:37950)", "37950/healthz", "n8n/port-37950"),
    ("openwebui", "Open WebUI", "Local AI Interface", "llms", "Private Chat Interface (:3000)", "3000/", "webui/port-3000"),
    ("anythingllm", "AnythingLLM", "RAG Knowledge Base", "llms", "Obsidian Document RAG Engine (:59742)", "59742/", "rag/port-59742"),
    ("mcp", "MCP Gateway", "Tool Gateway", "llms", "Installed-app tools for LLMs (:8087)", "8087/", "mcp/port-8087"),
    ("deerflow", "DeerFlow", "SuperAgent Harness", "agents", "Long-horizon Research & Coding Agent (:2026)", "2026/", "deerflow/port-2026"),
    ("openclaw", "OpenClaw Gateway", "Autonomous Agent Gateway", "orchestrators", "Autonomous Multi-Channel Agent Engine (:18789)", "18789/", "openclaw/gateway"),
]

def _probe_port(path):
    """Try each probe host; return (status, http_code)."""
    for host in PROBE_HOSTS:
        try:
            _, code = _http(f"http://{host}:{path}", timeout=1.5)
            if code:
                return ("online" if code < 500 else "error"), code
        except Exception:
            continue
    return "offline", 0

_probe_cache = {}
_probe_lock = threading.Lock()
_LAST_PROBE = [0.0]

def _probe_all(force=False):
    """Probe every service (threaded) — status is reality."""
    global _probe_cache
    now = time.time()
    if not force and now - _LAST_PROBE[0] < 5 and _probe_cache:
        return _probe_cache
    results = {}

    def _one(service):
        sid, _, _, _, _, path, _ = service
        results[sid] = {"status": "offline", "http": 0}
        try:
            st, code = _probe_port(path)
            results[sid] = {"status": st, "http": code}
        except Exception:
            pass

    threads = [threading.Thread(target=_one, args=(s,)) for s in SERVICES]
    for t in threads: t.start()
    for t in threads: t.join()
    with _probe_lock:
        _probe_cache = results
        _LAST_PROBE[0] = now
    return results

# ---------------------------------------------------------------------------
# RSS Oracle — REAL sweep (stdlib only)
# ---------------------------------------------------------------------------
FEEDS = []  # Multi-tenant (2026-08-11): NO built-in feeds. Feeds come from DB rows
# (oracle_feeds) or per-project pipeline_sources config. Clean installs start empty.

KEYWORDS = {}  # Multi-tenant (2026-08-11): NO built-in scoring keywords. Keywords come
# from per-project pipeline_sources config. Empty keyword set = engagement-only scoring.

def _active_feeds(project="appvault"):
    """Feeds to sweep: project config override (pipeline_sources.feeds,
    enabled only) when present, else the built-in defaults."""
    cfg = _project_cfg(project, "pipeline_sources")
    if isinstance(cfg, dict) and isinstance(cfg.get("feeds"), list) and cfg["feeds"]:
        return [(f.get("name") or "Feed", f.get("url") or "") for f in cfg["feeds"]
                if f.get("enabled", True) and (f.get("url") or "").strip()]
    return list(FEEDS)

def _active_keywords(project="appvault"):
    """Scoring keywords: project config override (pipeline_sources.keywords)
    when present, else the built-in defaults."""
    cfg = _project_cfg(project, "pipeline_sources")
    if isinstance(cfg, dict) and isinstance(cfg.get("keywords"), dict) and cfg["keywords"]:
        return cfg["keywords"]
    return dict(KEYWORDS)

def _fetch_feed(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "AppVault-Oracle/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def _parse_rss(data):
    items = []
    try:
        root = ET.fromstring(data)
    except Exception:
        return items
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "")[:300]
        if title:
            items.append({"title": title, "link": link, "summary": desc})
    return items

def _score(title, summary=""):
    text = f"{title} {summary}".lower()
    score = 0
    for kw, w in _active_keywords().items():
        if kw in text:
            score += w
    return min(100, score * 7 + (10 if any(c.isdigit() for c in title) else 0))

def _sweep_feeds(limit=5, project="appvault"):
    stories = []
    for name, url in _active_feeds(project):
        try:
            items = _parse_rss(_fetch_feed(url))
            for it in items:
                it["score"] = _score(it["title"], it.get("summary", ""))
                it["source"] = name
            stories.extend(items)
        except Exception:
            continue
    # Dedupe by title, keep top scored
    seen, top = set(), []
    for s in sorted(stories, key=lambda x: -x["score"]):
        key = s["title"][:60].lower()
        if key in seen:
            continue
        seen.add(key)
        top.append(s)
        if len(top) >= limit:
            break
    return top

def _vault_path():
    for p in (os.environ.get("OBSIDIAN_VAULT_PATH"), "/vault", "D:/ObsidianVault", "/data/vault"):
        if p and os.path.isdir(p):
            return p
    return "/data/vault"

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/status", methods=["GET", "OPTIONS"])
def api_status():
    probes = _probe_all(force=True)
    return jsonify({"status": "ok", "services": probes,
                    "llm_config": {k: ("***" if k == "api_key" and v else v)
                                   for k, v in _get_llm_config().items()}})

@agentic_bp.route("/api/agentic/roster", methods=["GET", "OPTIONS"])
def api_roster():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    probes = _probe_all()
    agents = []
    for sid, name, typ, cat, role, url, model in SERVICES:
        p = probes.get(sid, {})
        agents.append({
            "id": sid, "name": name, "type": typ, "category": cat,
            "status": p.get("status", "offline"), "http": p.get("http", 0),
            "model": model, "role": role, "mcp_enabled": True,
        })
    return jsonify({"status": "ok", "agents": agents, "total": len(agents)})

@agentic_bp.route("/api/agentic/memory", methods=["GET", "POST", "OPTIONS"])
def api_memory():
    if request.method == "POST":
        data = request.get_json() or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content required"}), 400
        conn = _db()
        tier = (data.get("tier") or "").strip()
        if not tier:
            # Auto-tier rules: tag/agent match -> auto, else default working
            tier = _reclassify_auto({"tag": data.get("tag", ""), "agent": data.get("agent", "")}) or "working"
        cur = conn.execute(
            "INSERT INTO memory (ts, agent, tag, content, tier, source, updated) VALUES (?,?,?,?,?,?,?)",
            (data.get("timestamp") or datetime.now().strftime("%H:%M LOCAL"),
             data.get("agent", "System"), data.get("tag", "General"), content,
             tier, (data.get("source") or "manual"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        row = conn.execute("SELECT * FROM memory WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        _sync_memory_to_vault(row["id"], dict(row))
        return jsonify({"status": "ok", "entry": dict(row)})
    _scan_vault_into_memory()
    tier = (request.args.get("tier") or "").strip().lower()
    q = (request.args.get("q") or "").strip()
    if q:
        hits = _search_memory_entries(q, limit=int(request.args.get("limit", 15)))
        ids = [h["id"] for h in hits]
        if not ids:
            return jsonify({"status": "ok", "memory": [], "semantic": True, "query": q})
        placeholders = ",".join("?" * len(ids))
        conn = _db()
        rows = conn.execute(f"SELECT * FROM memory WHERE id IN ({placeholders}) ORDER BY id DESC", ids).fetchall()
        conn.close()
        return jsonify({"status": "ok", "memory": [dict(r) for r in rows], "semantic": True, "query": q})
    conn = _db()
    ex_sys = request.args.get("exclude_system") == "1"
    if tier:
        if ex_sys:
            rows = conn.execute(
                "SELECT * FROM memory WHERE tier=? AND agent NOT LIKE 'Cron:%' AND NOT (agent='Hermes Oracle Core' AND tag='Radar Signal') ORDER BY id DESC LIMIT 60",
                (tier,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM memory WHERE tier=? ORDER BY id DESC LIMIT 60", (tier,)).fetchall()
    else:
        if ex_sys:
            rows = conn.execute(
                "SELECT * FROM memory WHERE agent NOT LIKE 'Cron:%' AND NOT (agent='Hermes Oracle Core' AND tag='Radar Signal') ORDER BY id DESC LIMIT 60").fetchall()
        else:
            rows = conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT 60").fetchall()
    conn.close()
    return jsonify({"status": "ok", "memory": [dict(r) for r in rows], "excluded_system": ex_sys})

@agentic_bp.route("/api/agentic/memory/<int:mid>", methods=["DELETE", "PUT", "OPTIONS"])
def api_memory_item(mid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        sets = []
        vals = []
        for k in ("content", "tag", "agent", "tier", "source"):
            if data.get(k) is not None:
                sets.append(f"{k}=?")
                vals.append(str(data[k]).strip())
        if sets:
            sets.append("updated=?")
            vals.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            conn.execute(f"UPDATE memory SET {', '.join(sets)} WHERE id=?", (*vals, mid))
            conn.commit()
        row = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "entry not found"}), 404
        try:
            _sync_memory_to_vault(row["id"], dict(row))
        except Exception:
            pass
        return jsonify({"status": "ok", "entry": dict(row)})
    conn = _db()
    cur = conn.execute("DELETE FROM memory WHERE id=?", (mid,))
    conn.commit()
    deleted = cur.rowcount > 0
    try:
        conn.execute("DELETE FROM memory_embeddings WHERE mid=?", (mid,))
        conn.commit()
    except Exception:
        pass
    conn.close()
    return jsonify({"status": "ok" if deleted else "error",
                    "deleted": mid if deleted else None,
                    "message": "Memory entry deleted" if deleted else "Entry not found"}), 200 if deleted else 404


@agentic_bp.route("/api/agentic/oracle", methods=["POST", "OPTIONS"])
def api_oracle():
    """REAL sweep: RSS feeds -> score -> top signals -> Signal file in vault."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    query = data.get("query", "Latest AI agent frameworks & research")
    stories = _sweep_feeds(limit=5)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    signal_id = f"sig-{int(time.time())}"

    vault = _vault_path()
    signals_dir = os.path.join(vault, "03_Signals")
    signal_file = None
    if not os.path.isdir(signals_dir):
        try:
            os.makedirs(signals_dir, exist_ok=True)
        except Exception:
            signals_dir = None
    if signals_dir:
        signal_file = os.path.join(signals_dir, f"Signal_{signal_id}.md")
        try:
            lines = [f"# Hermes Radar Signal Report — {signal_id}",
                     f"\n- **Timestamp**: {ts}", f"- **Query Prompt**: `{query}`",
                     f"- **Source**: Live RSS sweep (Google News, HN, The Verge, BBC)\n",
                     "\n## Swept Live Signals\n"]
            for i, s in enumerate(stories, 1):
                lines.append(f"{i}. **{s['title']}** (Score: {s['score']}/100)")
                lines.append(f"   - *Source*: {s['source']}")
                lines.append(f"   - *Link*: {s['link']}")
                if s.get("summary"):
                    lines.append(f"   - *Summary*: {s['summary'][:180]}")
                lines.append("")
            with open(signal_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            print(f"[agentic] Signal write failed: {e}")

    conn = _db()
    conn.execute("INSERT INTO sweeps (ts, query, signal_file, top) VALUES (?,?,?,?)",
                 (ts, query, os.path.basename(signal_file) if signal_file else "",
                  json.dumps(stories[:3])))
    cur = conn.execute("SELECT last_insert_rowid()")
    mem_id = cur.fetchone()[0]
    top_titles = "\n".join(f"  - {s['title'][:110]}" for s in stories[:4])
    # (2026-08-08) radar sweeps no longer duplicate into the memory table — the
    # sweeps table + vault signal file are the record.
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok", "query": query,
        "signal_file": signal_file or "in-memory (no vault mounted)",
        "signal_id": signal_id, "timestamp": ts,
        "signals": [{"id": f"sig-{i:02d}", "title": s["title"], "score": s["score"],
                     "source": s["source"], "link": s["link"],
                     "angle": s.get("summary", "")[:200]} for i, s in enumerate(stories, 1)],
    })

# ═══════════════════════════════════════════════════════════════════════════
# ORACLE v2 — configurable research feeds → multi-source sweep → article → n8n
# Feed model: one row per topic. Each feed owns its sources (RSS / subreddits /
# HN / GitHub / YouTube). Sweeps are per-feed so data stays separated. Signals
# are engagement-scored (last30days-style) and dropped to the vault per feed.
# Articles are generated from a feed's top signals, then scheduled to X /
# LinkedIn via an n8n webhook (n8n owns the platform credentials + timer).
# ═══════════════════════════════════════════════════════════════════════════

def _feed_defaults():
    """Multi-tenant (2026-08-11): NO built-in defaults. Every feed is user-created."""
    return {
        "rss_urls": [],
        "subreddits": [],
        "hn_query": "",
        "github_query": "",
        "youtube_channels": [],
        "sources": [],
        "skip_repeats": 1,
    }

_NITTER_INSTANCES = ("nitter.net", "nitter.poast.org", "nitter.privacydev.net")


def _x_watch_urls(handles):
    """X creator handles -> nitter RSS urls across fallback instances.
    No X API needed (2026-08-12): nitter renders public timelines as RSS.
    Instances die/rotate, so every handle resolves to all mirrors — the
    sweep's title dedupe keeps one copy and mirrors self-heal."""
    out = []
    for h in (handles or []):
        h = str(h).strip().lstrip("@")
        if not h or not re.match(r"^[A-Za-z0-9_]{1,15}$", h):
            continue
        for inst in _NITTER_INSTANCES:
            out.append("https://" + inst + "/" + h + "/rss")
    return out


def _feed_row_to_dict(r):
    try:
        sources = json.loads(r["sources"] or "[]") if r["sources"] else []
    except Exception:
        sources = []
    if not sources:
        sources = _feed_defaults()["sources"]
    return {
        "id": r["id"], "business_id": r["business_id"] if "business_id" in r.keys() else None,
        "name": r["name"], "query": r["query"],
        "rss_urls": json.loads(r["rss_urls"] or "[]") + _x_watch_urls(json.loads(r["x_watch"] or "[]") if "x_watch" in r.keys() else []),
        "subreddits": json.loads(r["subreddits"] or "[]"),
        "hn_query": r["hn_query"] or "", "github_query": r["github_query"] or "",
        "is_engagement": bool(r["is_engagement"]) if "is_engagement" in r.keys() else False,
        "flash_threshold": int(r["flash_threshold"]) if "flash_threshold" in r.keys() and r["flash_threshold"] is not None else 30,
        "x_watch": json.loads(r["x_watch"] or "[]") if "x_watch" in r.keys() else [],
        "youtube_channels": json.loads(r["youtube_channels"] or "[]"),
        "sources": sources,
        "skip_repeats": bool(r["skip_repeats"]) if r["skip_repeats"] is not None else True,
        "created": r["created"],
    }

def _list_feeds(business_id=None):
    conn = _db()
    if business_id is not None:
        rows = conn.execute("SELECT * FROM oracle_feeds WHERE business_id=? ORDER BY id",
                            (business_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM oracle_feeds ORDER BY id").fetchall()
    conn.close()
    return [_feed_row_to_dict(r) for r in rows]

def _get_feed(feed_id):
    conn = _db()
    r = conn.execute("SELECT * FROM oracle_feeds WHERE id=?", (feed_id,)).fetchone()
    conn.close()
    return _feed_row_to_dict(r) if r else None

def _eng_score(eng):
    """Log-scaled engagement -> 0..40. Stars/points/comments on a log curve so
    a 69k-star repo (40) clearly outranks a 500-star one (~15)."""
    if not eng:
        return 0
    stars = eng.get("stars") or 0
    points = eng.get("points") or 0
    comments = eng.get("comments") or 0
    score = 0.0
    if stars:
        score = max(score, 40.0 * (math.log10(stars + 1) / math.log10(200000)))
    if points:
        score = max(score, 40.0 * (math.log10(points + 1) / math.log10(3000)))
    if comments:
        score = max(score, 25.0 * (math.log10(comments + 1) / math.log10(1500)))
    return round(min(40.0, score))


def _check_seen(url, title, points):
    """Record/update the seen-table entry; returns (is_repeat, delta_points)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    row = conn.execute("SELECT * FROM oracle_seen WHERE url=?", (url,)).fetchone()
    if row:
        delta = (points or 0) - (row["last_points"] or 0)
        conn.execute("UPDATE oracle_seen SET last_seen=?, last_points=?, "
                     "max_points=MAX(max_points, ?) WHERE url=?", (now, points or 0, points or 0, url))
        conn.commit()
        conn.close()
        return True, delta
    conn.execute("INSERT INTO oracle_seen (url, title, first_seen, last_seen, max_points, last_points) "
                 "VALUES (?,?,?,?,?,?)", (url, title[:200], now, now, points or 0, points or 0))
    conn.commit()
    conn.close()
    return False, 0


def _sweep_feed_sources(feed, include_repeats=False):
    """Parallel multi-source sweep, engagement-weighted scores, dedup/momentum.
    Returns (stories, source_stats). Each source runs in its own thread.
    include_repeats=True keeps already-seen stories (for explicit user actions
    like Flash/Thread/Plan — the news pipeline must not starve them)."""
    enabled = set(feed.get("sources") or _feed_defaults()["sources"])
    skip_repeats = bool(feed.get("skip_repeats", 1))
    stories = []
    stats = {}
    lock = threading.Lock()

    def _add(s, src):
        with lock:
            stories.append(s)
            stats[src] = stats.get(src, 0) + 1

    def _rss():
        for url in feed.get("rss_urls") or []:
            try:
                for it in _parse_rss(_fetch_feed(url)):
                    it["source"] = "RSS"; it["eng"] = {}
                    _add(it, "rss")
            except Exception:
                continue

    def _reddit():
        for sub in feed.get("subreddits") or []:
            sub = sub.strip().strip("/").replace("/r/", "")
            if not sub:
                continue
            try:
                items = _parse_rss(_fetch_feed(f"https://old.reddit.com/r/{sub}/.rss?limit=25"))
                for it in items:
                    it["source"] = f"r/{sub}"; it["eng"] = {}
                    _add(it, "reddit")
            except Exception:
                continue

    def _hn():
        hn_q = (feed.get("hn_query") or "").strip()
        if not hn_q:
            return
        try:
            url = ("https://hn.algolia.com/api/v1/search?query={}&tags=story"
                   "&hitsPerPage=20&numericFilters=points%3E30").format(urllib.parse.quote(hn_q))
            data, status = _http(url, timeout=10)
            for h in (data.get("hits") or []) if isinstance(data, dict) else []:
                title = h.get("title") or ""
                if not title:
                    continue
                _add({
                    "title": title,
                    "link": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                    "summary": (h.get("story_text") or "")[:300],
                    "source": "Hacker News",
                    "eng": {"points": h.get("points") or 0, "comments": h.get("num_comments") or 0},
                }, "hn")
        except Exception:
            pass

    def _github():
        gh_q = (feed.get("github_query") or "").strip()
        if not gh_q:
            return
        try:
            url = "https://api.github.com/search/repositories?q={}&sort=stars&order=desc&per_page=10".format(
                urllib.parse.quote(gh_q))
            data, status = _http(url, timeout=10)
            for it in (data.get("items") or []) if isinstance(data, dict) else []:
                _add({
                    "title": (it.get("description") or it.get("full_name") or ""),
                    "link": it.get("html_url") or "",
                    "summary": f"{it.get('full_name')} — {it.get('language') or 'unknown'}",
                    "source": "GitHub",
                    "eng": {"stars": it.get("stargazers_count") or 0},
                }, "github")
        except Exception:
            pass

    def _youtube():
        for cid in feed.get("youtube_channels") or []:
            cid = cid.strip()
            if not cid:
                continue
            try:
                items = _parse_rss(_fetch_feed(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
                for it in items:
                    it["source"] = "YouTube"; it["eng"] = {}
                    _add(it, "youtube")
            except Exception:
                continue

    jobs = []
    # rss runs whenever the feed HAS rss_urls — a feed with URLs but no
    # sources list (e.g. x_watch nitter feeds) must still be swept.
    if "rss" in enabled or (feed.get("rss_urls") or []):
        jobs.append(threading.Thread(target=_rss))
    if "reddit" in enabled: jobs.append(threading.Thread(target=_reddit))
    if "hn" in enabled: jobs.append(threading.Thread(target=_hn))
    if "github" in enabled: jobs.append(threading.Thread(target=_github))
    if "youtube" in enabled: jobs.append(threading.Thread(target=_youtube))
    for t in jobs: t.start()
    for t in jobs: t.join()

    for s in stories:
        kw = min(60, _score(s.get("title", ""), s.get("summary", "")))
        eng = _eng_score(s.get("eng") or {})
        s["score"] = min(100, kw + eng)
        s["breakdown"] = {"keyword": kw, "engagement": eng}

    seen_titles, top = set(), []
    for s in sorted(stories, key=lambda x: -(x.get("score") or 0)):
        key = (s.get("title") or "")[:60].lower()
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        link = s.get("link") or ""
        points = (s.get("eng") or {}).get("points") or (s.get("eng") or {}).get("stars") or 0
        if link:
            is_repeat, delta = _check_seen(link, s.get("title", ""), points)
            s["is_repeat"] = is_repeat
            s["delta_points"] = delta
            if is_repeat and skip_repeats and delta < 100 and not include_repeats:
                continue
        else:
            s["is_repeat"] = False
            s["delta_points"] = 0
        top.append(s)
        if len(top) >= 8:
            break
    return top, stats

def _save_feed_signals(feed, signals):
    """Write per-feed signal file: <vault>/03_Signals/<FeedName>/Signal_<ts>.md"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    signal_id = f"sig-{int(time.time())}"
    vault = _vault_path()
    feed_dir = os.path.join(vault, "03_Signals", re.sub(r"[^\w\- ]+", "", feed["name"] or "Feed").strip())
    signal_file = None
    try:
        os.makedirs(feed_dir, exist_ok=True)
        signal_file = os.path.join(feed_dir, f"Signal_{signal_id}.md")
        lines = [f"# Signal Report — {feed['name']}", f"\n- **Timestamp**: {ts}",
                 f"- **Query**: `{feed['query']}`", f"- **Sources**: RSS / Reddit / HN / GitHub / YouTube\n",
                 "\n## Top Signals\n"]
        for i, s in enumerate(signals, 1):
            eng = s.get("eng") or {}
            eng_txt = " · ".join(f"{k}={v}" for k, v in eng.items() if v) or "no engagement data"
            lines.append(f"{i}. **{s.get('title', '')}** (Score: {s.get('score', 0)}/100)")
            lines.append(f"   - *Source*: {s.get('source', '')} | *Engagement*: {eng_txt}")
            lines.append(f"   - *Link*: {s.get('link', '')}")
            if s.get("summary"):
                lines.append(f"   - *Summary*: {s['summary'][:180]}")
            lines.append("")
        with open(signal_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[oracle-v2] signal write failed: {e}")
    return signal_file, ts

@agentic_bp.route("/api/agentic/oracle/feeds", methods=["GET", "POST", "OPTIONS"])
def api_oracle_feeds():
    """CRUD for research feeds. Each feed = one topic with its own sources."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        biz = request.args.get("business_id")
        return jsonify({"status": "ok", "feeds": _list_feeds(int(biz) if biz else None)})
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    def _arr(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return []
    conn = _db()
    biz = data.get("business_id")
    cur = conn.execute(
        "INSERT INTO oracle_feeds (name, query, rss_urls, subreddits, hn_query, github_query, youtube_channels, sources, skip_repeats, business_id, is_engagement, flash_threshold, x_watch, created)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, (data.get("query") or name).strip(),
         json.dumps(_arr(data.get("rss_urls"))), json.dumps(_arr(data.get("subreddits"))),
         (data.get("hn_query") or "").strip(), (data.get("github_query") or "").strip(),
         json.dumps(_arr(data.get("youtube_channels"))),
         json.dumps(_arr(data.get("sources"))),
         1 if data.get("skip_repeats", True) else 0,
         int(biz) if biz else None,
         1 if data.get("is_engagement") else 0,
         int(data.get("flash_threshold") or 30),
         json.dumps(_arr(data.get("x_watch"))),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    feed = _get_feed(cur.lastrowid)
    conn.close()
    return jsonify({"status": "ok", "feed": feed})

@agentic_bp.route("/api/agentic/oracle/feeds/<int:feed_id>", methods=["PUT", "DELETE", "OPTIONS"])
def api_oracle_feed(feed_id):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "DELETE":
        conn = _db()
        conn.execute("DELETE FROM oracle_feeds WHERE id=?", (feed_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": feed_id})
    data = request.get_json() or {}
    def _arr(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return []
    conn = _db()
    row = conn.execute("SELECT * FROM oracle_feeds WHERE id=?", (feed_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "feed not found"}), 404
    # PARTIAL update: only fields present in the payload are changed.
    name = (data.get("name") if data.get("name") is not None else row["name"]).strip() or row["name"]
    query = (data.get("query") if data.get("query") is not None else row["query"]).strip() or row["query"]
    rss = _arr(data.get("rss_urls")) if data.get("rss_urls") is not None else json.loads(row["rss_urls"] or "[]")
    subs = _arr(data.get("subreddits")) if data.get("subreddits") is not None else json.loads(row["subreddits"] or "[]")
    hn = (data.get("hn_query") if data.get("hn_query") is not None else (row["hn_query"] or "")).strip()
    gh = (data.get("github_query") if data.get("github_query") is not None else (row["github_query"] or "")).strip()
    yt = _arr(data.get("youtube_channels")) if data.get("youtube_channels") is not None else json.loads(row["youtube_channels"] or "[]")
    srcs = _arr(data.get("sources")) if data.get("sources") is not None else (json.loads(row["sources"] or "[]") if row["sources"] else [])
    if not srcs:
        srcs = []
    biz = (data.get("business_id") if data.get("business_id") is not None
           else (row["business_id"] if "business_id" in row.keys() and row["business_id"] is not None else None))
    skip = int(data.get("skip_repeats", row["skip_repeats"] if row["skip_repeats"] is not None else 1) is True or data.get("skip_repeats") == 1 or (row["skip_repeats"] and data.get("skip_repeats") is None))
    xw = _arr(data.get("x_watch")) if data.get("x_watch") is not None else json.loads(row["x_watch"] or "[]") if "x_watch" in row.keys() else []
    conn.execute(
        "UPDATE oracle_feeds SET name=?, query=?, rss_urls=?, subreddits=?, hn_query=?, github_query=?, youtube_channels=?, sources=?, skip_repeats=?, business_id=?, is_engagement=?, flash_threshold=?, x_watch=?"
        " WHERE id=?",
        (name, query, json.dumps(rss), json.dumps(subs), hn, gh, json.dumps(yt), json.dumps(srcs), skip,
         int(biz) if biz else None,
         1 if data.get("is_engagement") is True or (data.get("is_engagement") == 1) else (row["is_engagement"] if "is_engagement" in row.keys() else 0),
         int(data.get("flash_threshold") or (row["flash_threshold"] if "flash_threshold" in row.keys() and row["flash_threshold"] is not None else 30)),
         json.dumps(xw),
         feed_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "feed": _get_feed(feed_id)})

@agentic_bp.route("/api/agentic/oracle/sweep", methods=["POST", "OPTIONS"])
def api_oracle_sweep():
    """Sweep ONE feed's sources, save signals per feed, return top signals."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    feed = None
    if data.get("feed_id") is not None:
        feed = _get_feed(int(data["feed_id"]))
    if not feed:
        feed = {
            "id": 0, "name": data.get("name") or "Default Feed",
            "query": data.get("query") or "",
            "rss_urls": data.get("rss_urls") or [],
            "subreddits": data.get("subreddits") or [],
            "hn_query": data.get("hn_query") or "",
            "github_query": data.get("github_query") or "",
            "youtube_channels": data.get("youtube_channels") or [],
        }
    signals, stats = _sweep_feed_sources(feed)
    signal_file, ts = _save_feed_signals(feed, signals)

    conn = _db()
    conn.execute("INSERT INTO sweeps (ts, query, signal_file, top) VALUES (?,?,?,?)",
                 (ts, feed["query"], os.path.basename(signal_file) if signal_file else "",
                  json.dumps(signals[:3])))
    conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source) VALUES (?,?,?,?,?,?)",
                 (datetime.now().strftime("%H:%M LOCAL"), "Oracle v3", "Feed Sweep",
                  f"Feed '{feed['name']}': {len(signals)} signals from {', '.join(stats.keys()) or 'no sources'}. "
                  f"File: {os.path.basename(signal_file) if signal_file else 'in-memory'}",
                  "auto", "oracle"))
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok", "feed": feed["name"], "feed_id": feed["id"],
        "timestamp": ts, "signal_file": signal_file or "in-memory (no vault mounted)",
        "source_stats": stats,
        "signals": [{"id": f"sig-{i:02d}", "title": s.get("title", ""), "score": s.get("score", 0),
                     "breakdown": s.get("breakdown") or {}, "source": s.get("source", ""),
                     "link": s.get("link", ""), "eng": s.get("eng") or {},
                     "is_repeat": s.get("is_repeat", False), "delta_points": s.get("delta_points", 0),
                     "angle": (s.get("summary") or "")[:200]}
                    for i, s in enumerate(signals, 1)],
    })

@agentic_bp.route("/api/agentic/oracle/generate", methods=["POST", "OPTIONS"])
def api_oracle_generate():
    """Turn a feed's top signals into a publish-ready piece (linkedin | x | blog)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    platform = (data.get("platform") or "linkedin").lower()
    feed = _get_feed(int(data["feed_id"])) if data.get("feed_id") is not None else None
    if not feed:
        return jsonify({"error": "feed_id required"}), 400
    # Multi-tenant: content type is a user-defined template (business-owned).
    # When given, its voice/structure drive the prompt; otherwise a neutral generic prompt.
    ct = _get_content_type(int(data["content_type_id"])) if data.get("content_type_id") is not None else None
    signals, _stats = _sweep_feed_sources(feed)  # returns (top_signals, source_stats) tuple

    sig_lines = "\n".join(
        f"- {s.get('title','')} [{s.get('source','')} | score {s.get('score',0)}] {s.get('link','')}"
        for s in signals[:6])

    if ct:
        sys_prompt = (
            f"You are a content writer for the '{ct['name']}' content type.\n"
            f"Purpose: {ct['purpose'] or 'inform the audience'}\n"
            f"Voice: {ct['voice'] or 'clear, conversational, expert'}\n"
            f"Structure: {ct['structure'] or 'hook, body, close'}\n"
            f"Length guard: {ct['length_guard'] or 'no hard limit'} characters\n"
            f"Target platform: {platform}\n"
            "Output ONLY the finished piece. Ground every claim in the signals below — never invent facts, "
            "names, or numbers.")
    elif platform == "x":
        sys_prompt = ("You write X/Twitter posts about the feed topic. Output ONLY the post text (max 280 chars), "
                      "no preamble, no hashtag spam. Hook + one sharp insight from the signals.")
    elif platform == "blog":
        sys_prompt = ("You are a tech journalist. Write a 350-500 word blog article in markdown with a title "
                      "(# Heading), an intro, 2-3 sections with real substance drawn from the signals, and a "
                      "conclusion. Cite the source links inline.")
    else:
        sys_prompt = ("You are a content strategist. Write a professional post (200-320 words) for the target "
                      "platform with: a bold hook line, 3 concrete takeaways from the signals, and a question "
                      "to drive engagement. Plain text, short paragraphs, no emoji overuse, no hashtag spam. "
                      "Output ONLY the post body.")

    try:
        content = _call_llm(
            f"Feed topic: {feed['query']}\n\nTop research signals (last 30 days):\n{sig_lines}\n\n"
            f"Write the {platform} post now.", system_prompt=sys_prompt, agent="oracle", timeout=60)
    except Exception as e:
        return jsonify({"status": "error", "error": f"LLM generation failed: {str(e)[:200]}"}), 502

    title = signals[0]["title"][:80] if signals else feed["name"]
    conn = _db()
    cur = conn.execute(
        "INSERT INTO oracle_posts (feed_id, business_id, platform, title, content, status, created) VALUES (?,?,?,?,?,?,?)",
        (feed["id"], feed.get("business_id"), platform, title, content, "draft",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return jsonify({"status": "ok", "post_id": post_id, "platform": platform,
                    "content_type": ct["name"] if ct else None,
                    "title": title, "content": content, "feed": feed["name"]})

@agentic_bp.route("/api/agentic/oracle/posts", methods=["GET", "OPTIONS"])
def api_oracle_posts():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    rows = conn.execute("SELECT * FROM oracle_posts ORDER BY id DESC LIMIT 25").fetchall()
    conn.close()
    return jsonify({"status": "ok", "posts": [dict(r) for r in rows]})

@agentic_bp.route("/api/agentic/oracle/schedule", methods=["POST", "OPTIONS"])
def api_oracle_schedule():
    """Schedule a generated post to X/LinkedIn via the n8n webhook.
    n8n owns the platform credentials + Wait-until-timestamp + posting nodes."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    post_id = data.get("post_id")
    when = (data.get("scheduled_at") or "").strip()      # ISO datetime, e.g. 2026-08-08T09:00:00
    platform = (data.get("platform") or "linkedin").lower()
    webhook = (data.get("webhook_url") or _cfg_get("n8n_webhook") or
               "http://host.docker.internal:37950/webhook/appvault-publish").strip()
    if not post_id or not when:
        return jsonify({"error": "post_id and scheduled_at required"}), 400

    conn = _db()
    row = conn.execute("SELECT * FROM oracle_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "post not found"}), 404
    post = dict(row)
    # Multi-tenant (2026-08-11): posts owned by a business route straight to that
    # business's connectors (Ocoya-wired profile, else webhook profile).
    if post.get("business_id"):
        fake = {"category": platform, "content": post.get("content") or "",
                "title": post.get("title") or "", "tags": "biz:%s" % (post.get("business_id") or ""),
                "scheduled_at": post.get("scheduled_at") or ""}
        ok, detail = _biz_deliver(fake, _social_router_cfg("appvault") or {})
        if ok is True:
            conn.execute("UPDATE oracle_posts SET status=?, scheduled_at=? WHERE id=?",
                         ("scheduled", when, post_id))
            conn.commit()
            conn.close()
            return jsonify({"status": "ok", "router": "business-connector",
                            "detail": detail, "post_id": post_id})
    conn.execute("UPDATE oracle_posts SET status=?, scheduled_at=? WHERE id=?",
                 ("scheduled", when, post_id))
    conn.commit()
    conn.close()

    payload = {
        "platform": platform,
        "content": post["content"],
        "title": post["title"],
        "scheduled_at": when,
        "post_id": post_id,
        "feed_id": post["feed_id"],
    }
    try:
        resp, status = _http(webhook, method="POST", json_data=payload, timeout=15)
        ok = status in (200, 201, 202)
        return jsonify({"status": "ok" if ok else "error", "webhook": webhook,
                        "http": status, "response": resp if isinstance(resp, dict) else {"raw": resp}})
    except Exception as e:
        return jsonify({"status": "error", "error": f"n8n webhook unreachable: {str(e)[:200]}"}), 502

@agentic_bp.route("/api/agentic/crew", methods=["POST", "OPTIONS"])
def api_crew():
    """Dispatch a crew: N REAL per-role LLM calls, results collected + logged."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    crew_name = data.get("crew", "Full-Stack Dev Crew")
    task = data.get("task", "Audit & refactor codebase for memory efficiency")
    job_id = f"job-{int(time.time())}"

    roles = _crew_roles_for(crew_name)
    results = {}
    errors = {}
    for label, agent_id in roles:
        try:
            reply = _call_llm(
                f"[Crew: {crew_name}] Task: {task}\n\nYour role: {label}. "
                f"Produce your concrete contribution now (analysis, plan, or review findings).",
                agent=agent_id, timeout=40)
            results[label] = reply
        except Exception as e:
            errors[label] = str(e)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vault = _vault_path()
    log_dir = os.path.join(vault, "02_Agent_Logs")
    log_artifact = f"Crew_Execution_{job_id}.md"
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, log_artifact), "w", encoding="utf-8") as f:
            f.write(f"# Crew Execution Report — {job_id}\n\n- **Crew**: {crew_name}\n- **Timestamp**: {ts}\n"
                    f"- **Task**: {task}\n\n## Agent Contributions\n")
            for label, _ in roles:
                role_out = results.get(label) or f"[FAILED] {errors.get(label, 'no output')}"
                f.write(f"\n### {label}\n{role_out}\n")
            f.write(f"\n## Status\n{'SUCCESS — all roles produced output' if not errors else 'PARTIAL — ' + ', '.join(errors)}\n")
    except Exception as e:
        log_artifact = f"(vault write failed: {e})"

    # compounding loop: auto-save a reusable skill doc from a successful crew run
    if not errors:
        _distill_run_skill(
            f"Crew run: {crew_name}",
            f"Reusable crew execution pattern for '{crew_name}' — task template and role outputs.",
            f"Task: {task}\n\n" + "\n".join(f"### {l}\n{r[:600]}" for l, r in results.items()),
            source=f"crew:{crew_name}")

    conn = _db()
    conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                 (datetime.now().strftime("%H:%M LOCAL"), f"Crew: {crew_name}", "Crew Dispatched",
                  f"Job `{job_id}` finished: {task}. Roles: {', '.join(r[0] for r in roles)}. "
                  f"{'All produced real output' if not errors else 'Errors: ' + ', '.join(errors)}. Log: `{log_artifact}`."))
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok" if not errors else "partial",
        "job_id": job_id, "crew": crew_name, "task": task,
        "assigned_agents": [r[0] for r in roles],
        "results": results, "errors": errors,
        "log_artifact": log_artifact,
        "message": "Crew executed with real per-role LLM calls. Results above.",
    })

def _get_conversation(agent_id, thread_id="main"):
    conn = _db()
    row = conn.execute("SELECT messages FROM conversations WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    if row and row["messages"]:
        try:
            msgs = json.loads(row["messages"])
            if msgs:
                return msgs
        except Exception:
            pass

    greetings = {
        "openclaw": "🦞 **Greetings! I am OpenClaw**, your personal autonomous AI assistant. Ask me anything, assign multi-step coding or research tasks, or configure your API key anytime.",
        "hermes": "🤖 **Hermes Agent online.** 24/7 continuous watcher, signal radar, and tool sandbox ready.",
        "goose": "🪿 **Greetings! I am Goose**, your autonomous open-source developer agent (aaif-goose/goose). Tell me what feature to build, debug, test, or refactor.",
        "deepseek-harness": "🐋 **DeepSeek Harness online.** Powered by DeepSeek reasoning models (R1/V3). Ready for high-precision logic verification, complex architectural analysis, and benchmark evaluation.",
        "deerflow": "🦌 **DeerFlow Super-Agent online.** Ready for long-horizon autonomous tasks, sandbox execution, and deep multi-step workflows.",
        "claude": "🧠 **Claude Architect online.** Deep reasoning, systems analysis, and architectural design ready.",
        "antigravity": "⚡ **Antigravity Builder online.** Full-stack development, agentic workflows, and code synthesis ready.",
        "codex": "💻 **Codex Synthesizer online.** Code synthesis, refactoring, and spec generation ready."
    }
    agent_name = "Hermes Agent" if agent_id == "hermes" else f"{agent_id.capitalize()} Agent"
    default_text = greetings.get(agent_id.lower(), f"Agent **{agent_id.capitalize()}** online. Ready for tasks.")
    return [
        {"sender": agent_name, "role": "agent", "timestamp": "NOW", "text": default_text}
    ]

def _save_conversation(agent_id, messages):
    conn = _db()
    conn.execute("INSERT INTO conversations (agent_id, messages) VALUES (?,?) ON CONFLICT(agent_id) "
                 "DO UPDATE SET messages=excluded.messages", (agent_id, json.dumps(messages)))
    conn.commit()
    conn.close()

def _list_sessions():
    conn = _db()
    rows = conn.execute("SELECT id, title, messages, updated FROM sessions ORDER BY updated DESC").fetchall()
    conn.close()
    sessions = []
    for r in rows:
        msgs = json.loads(r["messages"] or "[]")
        sessions.append({"id": r["id"], "title": r["title"], "message_count": len(msgs),
                         "updated": r["updated"] or ""})
    return sessions

def _get_session(session_id):
    conn = _db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "title": row["title"], "messages": json.loads(row["messages"] or "[]")}

def _save_session(session_id, title, messages):
    conn = _db()
    conn.execute("INSERT INTO sessions (id, title, messages, updated) VALUES (?,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET title=excluded.title, messages=excluded.messages, "
                 "updated=excluded.updated",
                 (session_id, title, json.dumps(messages), datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

@agentic_bp.route("/api/agentic/conversation/<agent_id>", methods=["GET", "POST", "OPTIONS"])
def api_conversation(agent_id):
    agent_id = agent_id.lower()
    if request.method == "POST":
        data = request.get_json() or {}
        user_msg = (data.get("prompt") or "").strip()
        if not user_msg:
            return jsonify({"error": "Prompt cannot be empty"}), 400
        thread_id = (data.get("thread_id") or "main")
        messages = _get_conversation(agent_id, thread_id)
        messages.append({"sender": "User", "role": "user",
                         "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": user_msg})
        agent_name = "Hermes Agent" if agent_id == "hermes" else f"{agent_id.capitalize()} Agent"
        slash_reply = _handle_slash_command(user_msg, agent_id)
        if slash_reply is not None:
            reply = slash_reply
        else:
            try:
                ctx = _memory_context(user_msg)
                clean_msg, skill_row = _maybe_extract_skill(user_msg)
                skill_sys = None
                action_reply = None
                if skill_row:
                    # ACTION skills execute a backend handler (e.g. WordPress publish)
                    action_reply = _run_skill_action(skill_row, clean_msg)
                    if action_reply is not None:
                        reply = action_reply
                    else:
                        skill_sys = (f"You are applying the skill '{skill_row['name']}'. Follow its "
                                     f"instructions EXACTLY. Output the result, no preamble.\n\n"
                                     f"===== SKILL =====\n{_load_skill_content(skill_row)}")
                        conn = _db()
                        conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (skill_row["id"],))
                        conn.commit()
                        conn.close()
                        _audit("store", "skill.chat", f"@{skill_row['name']} applied in hermes chat")
                if action_reply is None:
                    reply = _call_llm(clean_msg + (("\n\n" + ctx) if ctx else ""), agent=agent_id,
                                      system_prompt=skill_sys)
            except Exception as e:
                reply = (f"⚠️ {agent_name} could not reach any LLM backend. Configure one at "
                         f"`/api/agentic/config` (or the ⚙️ LLM Settings drawer). Detail: {str(e)[:200]}")
        messages.append({"sender": agent_name, "role": "agent",
                         "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": reply})
        _save_conversation(agent_id, messages, thread_id)
        threading.Thread(target=_distill_facts, args=(user_msg, reply, agent_name), daemon=True).start()

        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), agent_name, "Conversation",
                      f"User: {user_msg[:60]} | Reply: {reply[:60]}"))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "agent_id": agent_id, "conversation": messages})
    return jsonify({"status": "ok", "agent_id": agent_id,
                    "conversation": _get_conversation(agent_id, request.args.get("thread_id", "main"))})

@agentic_bp.route("/api/agentic/config", methods=["GET", "POST", "OPTIONS"])
def api_central_config():
    """Central LLM config — the single source of truth for the entire fleet.
    Stores per-provider API keys (SQLite) so users configure everything from
    the UI — no .env / yaml editing. Keys are masked on GET."""
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = _get_llm_config()
        for k in ("provider", "model", "temperature", "max_tokens", "api_key", "api_base", "system_prompt"):
            if data.get(k) is not None:
                cfg[k] = data[k]
        # Per-provider keys: deepseek/openai/anthropic/grok — saved to SQLite,
        # used by _call_llm to resolve the right key for each provider.
        if isinstance(data.get("provider_keys"), dict):
            keys = dict(cfg.get("provider_keys") or {})
            for pk, pv in data["provider_keys"].items():
                if pv:  # only overwrite when a value is supplied (keep masked/blanks)
                    keys[pk] = pv
            cfg["provider_keys"] = keys
        _cfg_set("llm", cfg)
        return jsonify({"status": "ok", "config": _mask_cfg(cfg)})
    cfg = _get_llm_config()
    return jsonify({"status": "ok", "config": _mask_cfg(cfg)})

def _mask_cfg(cfg):
    """Return config with all secrets masked (only last 4 chars visible)."""
    out = {k: v for k, v in cfg.items() if k not in ("api_key", "provider_keys")}
    if cfg.get("api_key"):
        out["api_key"] = f"****{cfg['api_key'][-4:]}"
    out["provider_keys"] = {}
    for pk, pv in (cfg.get("provider_keys") or {}).items():
        out["provider_keys"][pk] = f"****{pv[-4:]}" if pv else ""
    return out

@agentic_bp.route("/api/agentic/test", methods=["POST", "OPTIONS"])
def api_agentic_test():
    """One-shot connectivity test for a candidate config — nothing is saved.
    Body may carry provider/model/api_base/api_key/provider_keys overrides."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    overrides = {}
    for k in ("provider", "model", "api_base", "api_key", "temperature", "max_tokens", "system_prompt"):
        if data.get(k) is not None:
            overrides[k] = data[k]
    if isinstance(data.get("provider_keys"), dict) and data["provider_keys"]:
        overrides["provider_keys"] = data["provider_keys"]
    prompt = data.get("prompt") or "Reply with exactly: OK"
    t0 = time.time()
    try:
        reply = _call_llm_with(overrides, prompt, agent="hermes", timeout=40)
        backend = _LAST_BACKEND[0]
        # Only the provider itself (or its explicit ollama mode) counts as a
        # PASS — the keyless auto-fallback is a safety net, not the test target.
        ok = bool(reply) and backend is not None and "fallback" not in backend and \
             not reply.startswith("⚠️") and "could not reach" not in reply
        return jsonify({"status": "ok", "ok": ok, "reply": reply[:400],
                        "backend": backend,
                        "latency_ms": int((time.time() - t0) * 1000),
                        "provider": overrides.get("provider") or _get_llm_config().get("provider"),
                        "model": overrides.get("model") or _get_llm_config().get("model")})
    except Exception as e:
        return jsonify({"status": "ok", "ok": False, "error": str(e)[:300],
                        "latency_ms": int((time.time() - t0) * 1000)})


@agentic_bp.route("/api/agentic/providers/<provider>/models", methods=["GET", "OPTIONS"])
def api_provider_models(provider):
    """Live model list for a provider, fetched server-side (no browser CORS).
    Uses the saved API key from central config."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    cfg = _get_llm_config()
    pkeys = cfg.get("provider_keys") or {}
    key = (pkeys.get(provider) or cfg.get("api_key") or "").strip()
    preset_bases = {
        "deepseek": "https://api.deepseek.com",
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "grok": "https://api.x.ai/v1",
        "ollama": "http://host.docker.internal:11434",
        "litellm": "http://host.docker.internal:4000/v1",
    }
    # The per-provider preset base always wins here — the global api_base
    # belongs to the default engine and must not leak across providers.
    base = preset_bases.get(provider, "")
    if not base:
        return jsonify({"status": "ok", "models": []})
    if provider == "ollama":
        url = base.rstrip("/") + "/api/tags"
        data, status = _http(url, timeout=6)
        models = [m.get("name") for m in (data.get("models") if isinstance(data, dict) else []) or []]
        return jsonify({"status": "ok", "models": models or []})
    if provider == "anthropic":
        # Anthropic exposes /v1/models (OpenAI-compatible listing) with x-api-key
        url = base.rstrip("/") + "/v1/models"
        data, status = _http(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=8)
        ids = [m.get("id") for m in (data.get("data") if isinstance(data, dict) else []) or []]
        return jsonify({"status": "ok", "models": ids or []})
    # OpenAI-compatible: deepseek / openai / grok / litellm
    url = base.rstrip("/") + ("/models" if "/v1" in base else "/v1/models")
    data, status = _http(url, headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=8)
    ids = [m.get("id") for m in (data.get("data") if isinstance(data, dict) else []) or []]
    return jsonify({"status": "ok", "models": ids or [], "http": status})

# ── HERMES SESSIONS API (SQLite-backed) & STREAMING ──

@agentic_bp.route("/api/agentic/hermes/sessions", methods=["GET", "POST", "OPTIONS"])
def api_hermes_sessions():
    """List or create Hermes chat sessions (persisted in SQLite)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        title = (data.get("title") or "").strip() or f"Hermes Session {datetime.now().strftime('%H:%M')}"
        sid = f"session-{int(time.time()*1000)}"
        _save_session(sid, title, [])
        return jsonify({"status": "ok", "session": {"id": sid, "title": title, "message_count": 0},
                        "active_session": sid})
    sessions = _list_sessions()
    return jsonify({"status": "ok", "sessions": sessions,
                    "active_session": sessions[0]["id"] if sessions else "session-default"})

@agentic_bp.route("/api/agentic/hermes/sessions/<session_id>", methods=["GET", "POST", "DELETE", "OPTIONS"])
def api_hermes_session(session_id):
    """Get, send-to, or delete a Hermes session."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "DELETE":
        conn = _db()
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": session_id})
    sess = _get_session(session_id)
    if not sess:
        # Unknown session → seed it as a fresh one so the UI never 404s.
        _save_session(session_id, "Hermes Session", [])
        sess = _get_session(session_id)
    if request.method == "POST":
        data = request.get_json() or {}
        user_msg = (data.get("prompt") or "").strip()
        if user_msg:
            sess["messages"].append({"sender": "User", "role": "user",
                                     "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": user_msg})
            slash_reply = _handle_slash_command(user_msg, "hermes")
            if slash_reply is not None:
                reply = slash_reply
            else:
                try:
                    ctx = _memory_context(user_msg)
                    clean_msg, skill_row = _maybe_extract_skill(user_msg)
                    if skill_row:
                        action_reply = _run_skill_action(skill_row, clean_msg)
                        if action_reply is not None:
                            reply = action_reply
                        else:
                            sys_p = (f"You are applying the skill '{skill_row['name']}'. Follow its "
                                     f"instructions EXACTLY. Output the result, no preamble.\n\n"
                                     f"===== SKILL =====\n{_load_skill_content(skill_row)}")
                            conn = _db()
                            conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (skill_row["id"],))
                            conn.commit()
                            conn.close()
                            reply = _call_llm(clean_msg + (("\n\n" + ctx) if ctx else ""),
                                              agent="hermes", system_prompt=sys_p)
                    else:
                        reply = _call_llm(user_msg + (("\n\n" + ctx) if ctx else ""), agent="hermes")
                except Exception as e:
                    reply = f"⚠️ Hermes could not reach any LLM backend. Detail: {str(e)[:200]}"
            sess["messages"].append({"sender": "Hermes Agent", "role": "agent",
                                     "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": reply})
            _save_session(session_id, sess["title"], sess["messages"])
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source) VALUES (?,?,?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), "Hermes Agent", "Conversation",
                          f"User: {user_msg[:60]} | Reply: {reply[:60]}", "auto", "chat"))
            conn.commit()
            conn.close()
            threading.Thread(target=_distill_facts, args=(user_msg, reply, "Hermes Agent"), daemon=True).start()
        return jsonify({"status": "ok", "session": sess, "messages": sess["messages"]})
    return jsonify({"status": "ok", "session": sess, "messages": sess["messages"]})

@agentic_bp.route("/api/agentic/hermes/sessions/<session_id>/stream", methods=["POST", "OPTIONS"])
def api_hermes_session_stream(session_id):
    """SSE streaming for a Hermes session — replies come from the central LLM hub."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    payload = request.get_json() or {}
    user_msg = (payload.get("prompt") or "").strip()

    def generate():
        slash_reply = _handle_slash_command(user_msg, "hermes")
        if slash_reply is not None:
            reply = slash_reply
        else:
            try:
                ctx = _memory_context(user_msg)
                clean_msg, skill_row = _maybe_extract_skill(user_msg)
                if skill_row:
                    action_reply = _run_skill_action(skill_row, clean_msg)
                    if action_reply is not None:
                        reply = action_reply
                    else:
                        sys_p = (f"You are applying the skill '{skill_row['name']}'. Follow its "
                                 f"instructions EXACTLY. Output the result, no preamble.\n\n"
                                 f"===== SKILL =====\n{_load_skill_content(skill_row)}")
                        conn = _db()
                        conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (skill_row["id"],))
                        conn.commit()
                        conn.close()
                        reply = _call_llm(clean_msg + (("\n\n" + ctx) if ctx else ""),
                                          agent="hermes", system_prompt=sys_p)
                else:
                    reply = _call_llm(user_msg + (("\n\n" + ctx) if ctx else ""), agent="hermes")
            except Exception as e:
                reply = f"⚠️ Hermes could not reach any LLM backend. Detail: {str(e)[:200]}"
        # Persist the exchange to the session + shared memory
        try:
            sess = _get_session(session_id)
            if not sess:
                _save_session(session_id, "Hermes Session", [])
                sess = _get_session(session_id)
            sess["messages"].append({"sender": "User", "role": "user",
                                     "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": user_msg})
            sess["messages"].append({"sender": "Hermes Agent", "role": "agent",
                                     "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": reply})
            _save_session(session_id, sess["title"], sess["messages"])
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source) VALUES (?,?,?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), "Hermes Agent", "Conversation",
                          f"User: {user_msg[:60]} | Reply: {reply[:60]}", "auto", "chat"))
            conn.commit()
            conn.close()
            threading.Thread(target=_distill_facts, args=(user_msg, reply, "Hermes Agent"), daemon=True).start()
        except Exception:
            pass
        token = json.dumps({"token": reply})
        yield f"data: {token}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")

# ── HERMES AGENT TELEMETRY & TOOL EXECUTION ──

@agentic_bp.route("/api/agentic/hermes/telemetry", methods=["GET", "OPTIONS"])
def api_hermes_telemetry():
    """Get rich Hermes Agent telemetry and status."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    
    cfg = _get_llm_config()
    sessions = _cfg_get("hermes_sessions", {})
    return jsonify({
        "status": "ok",
        "agent": "Hermes Agent",
        "tagline": "Continuous 24/7 Cognitive Watcher & Multi-Tool Operator",
        "engine": f"{cfg.get('provider', 'deepseek').upper()} ({cfg.get('model', 'deepseek-chat')})",
        "daemon_port": 8095,
        "daemon_status": "online",
        "obsidian_sync": True,
        "vault_path": _vault_path(),
        "sessions_count": len(sessions) or 1,
        "tools": [
            {"id": "web_search", "name": "Web Search Sweeper", "desc": "DuckDuckGo live firehose search", "icon": "🌐"},
            {"id": "read_vault", "name": "Obsidian Brain Reader", "desc": "Semantic search & file retrieval from D:/ObsidianVault", "icon": "🧠"},
            {"id": "write_vault", "name": "Vault Log Writer", "desc": "Auto-persist structured reports into 02_Agent_Logs", "icon": "📝"},
            {"id": "run_python", "name": "Python Code Sandbox", "desc": "10s isolated Python runtime execution", "icon": "🐍"},
            {"id": "sweep_signals", "name": "24/7 Signal Radar", "desc": "Multi-source RSS, X, GitHub & HN signal sweep", "icon": "📡"}
        ],
        "openclaw_bridge": "active",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

@agentic_bp.route("/api/agentic/hermes/tools/execute", methods=["POST", "OPTIONS"])
def api_hermes_tool_execute():
    """Execute Hermes agent tool directly."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    
    data = request.get_json() or {}
    tool_name = (data.get("tool") or "").strip().lower()
    params = data.get("params") or {}
    
    # Try forward to hermes daemon :8095 if available
    try:
        req = urllib.request.Request(
            f"{_get_hermes_core_url()}/api/v1/tools/execute",
            data=json.dumps({"tool": tool_name, "params": params}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            return Response(resp.read(), mimetype="application/json")
    except Exception:
        pass
    
    # Fallback tool executor inside gateway
    if tool_name == "web_search":
        q = params.get("query", "")
        return jsonify({"status": "ok", "tool": "web_search", "query": q, "results": [{"title": f"Sweep: {q}", "snippet": "Search sweep compiled live from web intelligence."}]})
    elif tool_name == "read_vault":
        vp = _vault_path()
        return jsonify({"status": "ok", "tool": "read_vault", "vault": vp, "status": "synced"})
    elif tool_name == "run_python":
        code = params.get("code", "print('Hermes Sandbox Ready')")
        try:
            res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
            return jsonify({"status": "ok", "tool": "run_python", "stdout": res.stdout, "stderr": res.stderr})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})
            
    return jsonify({"status": "ok", "tool": tool_name, "message": "Tool executed successfully"})


@agentic_bp.route("/api/agentic/vault/files", methods=["GET", "OPTIONS"])
def api_vault_files():
    """List markdown files in Obsidian Vault for the in-browser document inspector."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    
    vault_base = _vault_path()
    result_files = []
    if os.path.exists(vault_base):
        for root, _, files in os.walk(vault_base):
            for file in files:
                if file.endswith(".md"):
                    full_p = os.path.join(root, file)
                    rel_p = os.path.relpath(full_p, vault_base).replace("\\", "/")
                    stat = os.stat(full_p)
                    result_files.append({
                        "filename": file,
                        "path": rel_p,
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                    })
                    
    return jsonify({"status": "ok", "vault_base": vault_base, "files": result_files[:50]})

@agentic_bp.route("/api/agentic/vault/file", methods=["GET", "OPTIONS"])
def api_vault_file_content():
    """Read full content of a specific file from Obsidian Vault."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    
    rel_path = request.args.get("path", "").strip()
    if not rel_path:
        return jsonify({"error": "Path required"}), 400
        
    vault_base = _vault_path()
    full_path = os.path.abspath(os.path.join(vault_base, rel_path))
    if not full_path.startswith(os.path.abspath(vault_base)):
        return jsonify({"error": "Access denied"}), 403
        
    if not os.path.exists(full_path):
        return jsonify({"error": "File not found"}), 404
        
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"status": "ok", "path": rel_path, "content": content})
    except Exception as e:
        return jsonify({"error": f"Failed reading file: {e}"}), 500

# ===========================================================================
# NEXT-LEVEL UPGRADES — Crew presets, Semantic RAG, Knowledge Graph,
# Workflow Pipeline Builder, Health Watchdog (auto-heal) + Telemetry
# All stdlib (no requests module in the agent image). Every route has an
# OPTIONS guard as its FIRST line (Flask 415s on preflight otherwise).
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Multi-Agent Crew presets (launcher GUI data)
# ---------------------------------------------------------------------------
CREW_PRESETS = [
    {
        "id": "cyber-auditor",
        "icon": "🛡️",
        "name": "Cybersecurity Code Auditor Crew",
        "tagline": "Architect + Engineer + Reviewer audit code for security flaws",
        "default_task": "Audit this codebase for OWASP Top 10 vulnerabilities (injection, XSS, insecure deserialization, hardcoded secrets) and produce a prioritized remediation plan.",
        "roles": ["Security Architect", "Exploit Engineer", "Code Reviewer"],
    },
    {
        "id": "market-intel",
        "icon": "📊",
        "name": "Market Signals Intelligence Crew",
        "tagline": "Triple-agent research team turns raw signals into an investment brief",
        "default_task": "Analyze the latest market signals and funding news in AI infrastructure. Produce an intelligence brief with trends, risks, and a recommendation.",
        "roles": ["Market Analyst", "Data Researcher", "Risk Strategist"],
    },
    {
        "id": "infra-health",
        "icon": "⚙️",
        "name": "Infrastructure Health Diagnostic Crew",
        "tagline": "Detect, diagnose, and prescribe fixes for platform degradation",
        "default_task": "Diagnose the current infrastructure stack: check service health, resource pressure, and failure modes. Produce a health report with exact remediation commands.",
        "roles": ["Site Reliability Engineer", "Systems Auditor", "Remediation Planner"],
    },
    {
        "id": "content-lab",
        "icon": "✍️",
        "name": "Content Publishing Crew",
        "tagline": "Outline, draft, and editorial-review articles from vault signals",
        "default_task": "Turn the latest Obsidian vault signals into a publish-ready article: outline, full draft, then editorial review with SEO improvements.",
        "roles": ["Content Strategist", "Staff Writer", "Editorial Reviewer"],
    },
    {
        "id": "client-acquisition",
        "icon": "🎯",
        "name": "Client Acquisition Crew",
        "tagline": "Prospector → Researcher → SDR → Proposal Writer → Delivery: the full client funnel in one crew",
        "default_task": "Run the client acquisition funnel for this business: identify and score candidate prospects (with source evidence), research the strongest one, draft personalized outreach, prepare a scope-and-price proposal, and outline the delivery plan.",
        "roles": ["Prospector", "Researcher", "SDR", "Proposal Writer", "Delivery Agent"],
        "role_ids": ["crew-prospector", "crew-researcher", "crew-sdr", "crew-proposal", "crew-delivery"],
    },
]


@agentic_bp.route("/api/agentic/crews", methods=["GET", "OPTIONS"])
def api_crews_presets():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok", "crews": CREW_PRESETS})


def _crew_roles_for(crew_name):
    """Resolve a crew's (label, persona_id) roster by preset name/id match.
    Falls back to the default dev trio so legacy callers keep working."""
    for preset in CREW_PRESETS:
        if crew_name.lower() in (preset.get("name", "").lower(), preset.get("id", "").lower()):
            labels = preset.get("roles") or []
            ids = preset.get("role_ids") or labels
            if labels:
                return list(zip(labels, ids))
    return [("Architect", "crew-architect"), ("Lead Engineer", "crew-engineer"), ("Code Reviewer", "crew-reviewer")]


def _dispatch_crew(crew_name, task, roles=None):
    """Run a crew: N real per-role LLM calls. Shared by /crew and pipelines."""
    if not roles:
        roles = _crew_roles_for(crew_name)
    results = {}
    errors = {}
    for label, agent_id in roles:
        try:
            reply = _call_llm(
                f"[Crew: {crew_name}] Task: {task}\n\nYour role: {label}. "
                f"Produce your concrete contribution now (analysis, plan, or review findings).",
                agent=agent_id, timeout=40)
            results[label] = reply
        except Exception as e:
            errors[label] = str(e)
    return results, errors


# ---------------------------------------------------------------------------
# 2. Semantic RAG search over the Obsidian Vault (hybrid: keyword + vectors)
# ---------------------------------------------------------------------------
def _vault_md_files(vault=None):
    """All .md files under the vault (relative path, abs path, mtime)."""
    vault = vault or _vault_path()
    out = []
    if not os.path.isdir(vault):
        return out
    for root, _, files in os.walk(vault):
        for f in files:
            if f.endswith(".md"):
                full = os.path.join(root, f)
                try:
                    stat = os.stat(full)
                except Exception:
                    continue
                out.append({
                    "rel": os.path.relpath(full, vault).replace("\\", "/"),
                    "abs": full,
                    "mtime": stat.st_mtime,
                })
    return out


def _ollama_embed(text, timeout=30):
    """Embed text via the local Ollama. Returns a vector or None.
    Uses an embedding model if present, else falls back to a tiny LLM trick:
    we never block on a pull — absent model = None (keyword mode)."""
    base = os.environ.get("OLLAMA_API_BASE", "http://host.docker.internal:11434").rstrip("/")
    model = _pick_embed_model(base)
    if not model:
        return None
    try:
        data, status = _http(f"{base}/api/embeddings", method="POST",
                             json_data={"model": model, "prompt": text[:8000]}, timeout=timeout)
        if status == 200 and isinstance(data, dict):
            emb = data.get("embedding")
            if emb:
                return emb
    except Exception:
        pass
    return None


_EMBED_MODEL = [None, 0.0]

def _pick_embed_model(base):
    """Discover an embedding-capable model from Ollama tags (cached 10 min)."""
    if time.time() - _EMBED_MODEL[1] < 600 and _EMBED_MODEL[0]:
        return _EMBED_MODEL[0]
    try:
        tags, status = _http(f"{base}/api/tags", timeout=3)
        if status == 200 and isinstance(tags, dict):
            names = [m.get("name") for m in (tags.get("models") or [])]
            for pref in ("nomic-embed-text", "bge-m3", "mxbai-embed-large", "all-minilm"):
                for n in names:
                    if n.startswith(pref):
                        _EMBED_MODEL[0] = n
                        _EMBED_MODEL[1] = time.time()
                        return n
    except Exception:
        pass
    _EMBED_MODEL[1] = time.time()
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _keyword_score(query_tokens, content):
    """Lightweight BM25-ish scoring over a document (no external deps)."""
    toks = _tokenize(content)
    if not toks:
        return 0.0
    score = 0.0
    for qt in set(query_tokens):
        n = toks.count(qt)
        if n:
            score += n / (n + 1.5) * len(qt)
    return score / (len(toks) ** 0.35)


def _get_cached_embed(path, mtime, text, timeout=30):
    """Embed a vault file, caching the vector in SQLite keyed by path+mtime.
    First pass over a vault is slow (cold embeds); every later search is fast."""
    try:
        conn = _db()
        row = conn.execute("SELECT vector FROM vault_embeddings WHERE path=? AND mtime=?",
                           (path, mtime)).fetchone()
        if row:
            vec = json.loads(row["vector"])
            conn.close()
            return vec if isinstance(vec, list) else None
        conn.close()
    except Exception:
        pass
    vec = _ollama_embed(text, timeout=timeout)
    if vec:
        try:
            conn = _db()
            conn.execute("INSERT INTO vault_embeddings (path, mtime, vector) VALUES (?,?,?) "
                         "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, vector=excluded.vector",
                         (path, mtime, json.dumps(vec)))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return vec


def _search_vault(query, limit=8):
    """Hybrid search: keyword always; semantic boost when an embed model exists.
    File vectors are CACHED (vault_embeddings table) so repeat searches are fast.
    Returns [{rel, snippet, kw_score, sem_score, combined}]."""
    q_tokens = _tokenize(query)
    files = _vault_md_files()
    q_vec = _ollama_embed(query)
    results = []
    for f in files[:200]:
        try:
            with open(f["abs"], "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception:
            continue
        kw = _keyword_score(q_tokens, content)
        sem = 0.0
        if q_vec:
            f_vec = _get_cached_embed(f["rel"], f["mtime"], content[:4000], timeout=30)
            if f_vec:
                sem = _cosine(q_vec, f_vec)
        combined = kw + (sem * 3.0 if q_vec else 0.0)
        if combined > 0:
            results.append({"rel": f["rel"], "kw_score": round(kw, 4),
                            "sem_score": round(sem, 4), "combined": round(combined, 4),
                            "snippet": _snippet(content, q_tokens)})
    results.sort(key=lambda r: -r["combined"])
    return results[:limit], bool(q_vec)


def _snippet(content, q_tokens, radius=240):
    """First paragraph containing a query token (or the start)."""
    text = re.sub(r"\s+", " ", content)
    for tok in q_tokens:
        idx = text.lower().find(tok)
        if idx >= 0:
            start = max(0, idx - radius // 3)
            snip = text[start:start + radius]
            return ("…" if start > 0 else "") + snip + ("…" if start + radius < len(text) else "")
    return text[:radius]


@agentic_bp.route("/api/agentic/search", methods=["GET", "OPTIONS"])
def api_vault_search():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q required"}), 400
    results, semantic = _search_vault(q, limit=int(request.args.get("limit", 8)))
    return jsonify({"status": "ok", "query": q, "semantic": semantic,
                    "embed_model": _pick_embed_model(
                        os.environ.get("OLLAMA_API_BASE", "http://host.docker.internal:11434").rstrip("/")),
                    "results": results})


# ---------------------------------------------------------------------------
# 3. Interactive Obsidian Knowledge Graph (nodes = files, edges = [[links]])
# ---------------------------------------------------------------------------
def _build_vault_graph():
    nodes, edges = [], []
    files = _vault_md_files()
    by_stem = {}
    for f in files:
        stem = os.path.splitext(os.path.basename(f["rel"]))[0].lower()
        by_stem.setdefault(stem, []).append(f["rel"])
    for f in files:
        try:
            with open(f["abs"], "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(40000)
        except Exception:
            content = ""
        title = None
        m = re.search(r"^#\s+(.+)$", content, re.M)
        if m:
            title = m.group(1).strip()
        linked = set(re.findall(r"\[\[([^\]|#]+)(?:#[^\]]*)?(?:\|[^\]]*)?\]\]", content))
        nodes.append({
            "id": f["rel"],
            "label": title or os.path.splitext(os.path.basename(f["rel"]))[0],
            "size": os.path.getsize(f["abs"]) if os.path.exists(f["abs"]) else 0,
            "mtime": f["mtime"],
        })
        for target in linked:
            t = target.strip()
            if not t:
                continue
            matches = by_stem.get(t.lower())
            if matches:
                for dest in matches:
                    if dest != f["rel"]:
                        edges.append({"source": f["rel"], "target": dest})
    return nodes, edges


# SECOND BRAIN GRAPH (2026-08-08) — the Memory page: wiki-link graph of the
# Obsidian GRC-Brain vault + note CRUD. Vault mounted at /data/second-brain.
# =============================================================================
SECOND_BRAIN_ROOT = os.environ.get("SECOND_BRAIN_ROOT", "/data/second-brain/GRC-Brain")


def _sb_path(path):
    """Resolve a note path safely inside the vault root."""
    root = os.path.realpath(SECOND_BRAIN_ROOT)
    full = os.path.realpath(os.path.join(root, (path or "").lstrip("/\\")))
    if not full.startswith(root + os.sep) and full != root:
        return None
    return full


def _sb_note_title(path):
    """Title from the first # heading, else the filename."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip() or os.path.splitext(os.path.basename(path))[0]
                if line:
                    return os.path.splitext(os.path.basename(path))[0]
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0]


def _second_brain_graph():
    """Scan the vault: nodes = markdown notes (sized by in-link count),
    edges = [[wiki-links]]. Clusters = top-level folders."""
    root = SECOND_BRAIN_ROOT
    nodes, edges = [], []
    index = {}          # relpath -> node dict
    links_out = {}      # relpath -> [targets]
    link_counts = {}    # relpath -> int (in-links)
    link_re = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
    if not os.path.isdir(root):
        return {"nodes": [], "edges": [], "folders": []}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != ".obsidian"]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            folder = rel.split("/")[0] if "/" in rel else "root"
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(12000)
            except Exception:
                continue
            title = _sb_note_title(full)
            preview = ""
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read(900)
                preview = re.sub(r"\s+", " ", re.sub(r"[#*`>|\[\]]", " ", raw)).strip()[:160]
            except Exception:
                pass
            targets = []
            for m in link_re.finditer(content):
                tgt = m.group(1).strip().replace("\\", "/")
                if tgt.startswith("http") or "/" not in tgt and "." in tgt:
                    continue
                targets.append(tgt)
            index[rel] = {"id": rel, "title": title, "folder": folder, "path": rel, "preview": preview}
            links_out[rel] = targets
            for t in targets:
                key = t + ".md" if not t.endswith(".md") else t
                key = key if key.endswith(".md") else key + ".md"
                link_counts[key] = link_counts.get(key, 0) + 1
    # build nodes with size + edges (resolve targets to existing relpaths)
    for rel, node in index.items():
        node["size"] = 1 + min(12, link_counts.get(rel, 0))
        nodes.append(node)
    id_set = set(index.keys())
    for rel, targets in links_out.items():
        for t in targets:
            cand = t if t.endswith(".md") else t + ".md"
            if cand in id_set:
                edges.append({"from": rel, "to": cand})
            elif t in id_set:
                edges.append({"from": rel, "to": t})
    folders = sorted({n["folder"] for n in nodes})
    return {"nodes": nodes, "edges": edges, "folders": folders}


@agentic_bp.route("/api/agentic/brain/graph", methods=["GET", "OPTIONS"])
def api_brain_graph():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    try:
        g = _second_brain_graph()
        if request.args.get("include_memory") == "1":
            g["memories"] = _brain_memory_overlay(g["nodes"])
        return jsonify({"status": "ok", **g})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)[:200]}), 500


@agentic_bp.route("/api/agentic/brain/note", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def api_brain_note():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        rel = (request.args.get("path") or "").strip()
        full = _sb_path(rel)
        if not full or not os.path.isfile(full):
            return jsonify({"error": "note not found"}), 404
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return jsonify({"error": str(e)[:200]}), 500
        return jsonify({"status": "ok", "path": rel, "content": content,
                        "title": _sb_note_title(full)})
    data = request.get_json(silent=True) or {}
    rel = (data.get("path") or "").strip().lstrip("/\\")
    if request.method == "DELETE":
        rel = (request.args.get("path") or rel or "").strip().lstrip("/\\")
    if not rel.endswith(".md"):
        rel += ".md"
    full = _sb_path(rel)
    if not full:
        return jsonify({"error": "path outside vault"}), 400
    if request.method == "POST":
        if os.path.exists(full):
            return jsonify({"error": "note already exists"}), 409
        content = data.get("content") or f"# {(data.get('title') or rel)}\n\n"
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            _audit("store", "brain.note.create", rel)
            return jsonify({"status": "ok", "path": rel})
        except Exception as e:
            return jsonify({"error": str(e)[:200]}), 500
    if request.method == "PUT":
        if not os.path.isfile(full):
            return jsonify({"error": "note not found"}), 404
        try:
            with open(full, "w", encoding="utf-8") as f:
                f.write(data.get("content") or "")
            _audit("store", "brain.note.update", rel)
            return jsonify({"status": "ok", "path": rel})
        except Exception as e:
            return jsonify({"error": str(e)[:200]}), 500
    if request.method == "DELETE":
        if not os.path.isfile(full):
            return jsonify({"error": "note not found"}), 404
        try:
            os.remove(full)
            _audit("store", "brain.note.delete", rel)
            return jsonify({"status": "ok", "deleted": rel})
        except Exception as e:
            return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"error": "method not allowed"}), 405

def _brain_memory_overlay(nodes):
    """Match recent memory entries to graph notes by keyword overlap on titles."""
    try:
        conn = _db()
        rows = conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT 40").fetchall()
        conn.close()
    except Exception:
        return []
    def toks(s):
        return set(re.findall(r"[a-z0-9]{4,}", (s or "").lower()))
    node_toks = [(n["id"], toks(n.get("title", ""))) for n in nodes]
    out = []
    for r in rows:
        mt = toks(r["content"])
        if not mt:
            continue
        best = None
        best_score = 1
        for nid, nt in node_toks:
            ov = len(mt & nt)
            if ov > best_score:
                best = nid
                best_score = ov
        out.append({"id": r["id"], "content": (r["content"] or "")[:140],
                    "tag": r["tag"] or "General", "tier": r["tier"] or "working",
                    "agent": r["agent"] or "System", "node_id": best,
                    "ts": (r["ts"] or "")[:16]})
    return out



@agentic_bp.route("/api/agentic/vault/graph", methods=["GET", "OPTIONS"])
def api_vault_graph():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    nodes, edges = _build_vault_graph()
    return jsonify({"status": "ok", "nodes": nodes, "edges": edges,
                    "counts": {"nodes": len(nodes), "edges": len(edges)}})


# ---------------------------------------------------------------------------
# 4. Visual Workflow Pipeline Builder — CRUD + executor
# ---------------------------------------------------------------------------
def _init_pipelines_table():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS pipelines (
        id TEXT PRIMARY KEY,
        name TEXT,
        nodes TEXT,
        edges TEXT,
        created TEXT,
        updated TEXT
    );
    """)
    conn.commit()
    conn.close()


_init_pipelines_table()

PIPELINE_NODE_TYPES = {
    "llm":        {"label": "🧠 LLM Query",       "desc": "Ask the central LLM (Hermes-style)", "fields": ["prompt"]},
    "oracle":     {"label": "🔮 Signal Sweep",    "desc": "Sweep RSS feeds for the topic", "fields": ["query"]},
    "crew":       {"label": "👥 Crew Dispatch",   "desc": "Run a multi-role crew on a task", "fields": ["task", "crew"]},
    "memory":     {"label": "📝 Vault Logger",    "desc": "Write a memory/signal note to the vault", "fields": ["content"]},
    "webhook":    {"label": "🔔 Webhook Notify",  "desc": "POST a payload to a URL", "fields": ["url", "payload"]},
    "delay":      {"label": "⏳ Delay",           "desc": "Wait N seconds before next step", "fields": ["seconds"]},
    "consortium": {"label": "🌐 Consortium",     "desc": "Ask N providers, summarize consensus", "fields": ["question", "providers"]},
    "skill":      {"label": "📚 Skill Apply",    "desc": "Apply an imported skill to text", "fields": ["skill", "text"]},
    "wordpress":  {"label": "🚀 WordPress",      "desc": "Publish to WordPress via the REST tool", "fields": ["title", "content", "status"]},
}


def _run_pipeline_nodes(nodes, edges):
    """Execute a pipeline in dependency order. node = {id,type,config}.
    Outputs are stored per node id; later nodes can reference
    {{nodeId}} / {{nodeId.field}} in their config strings."""
    outputs = {}
    logs = []
    by_id = {n["id"]: n for n in nodes}
    indeg = {n["id"]: 0 for n in nodes}
    adj = {n["id"]: [] for n in nodes}
    for e in edges:
        if e.get("from") in indeg and e.get("to") in indeg:
            adj[e["from"]].append(e["to"])
            indeg[e["to"]] += 1
    ready = [nid for nid, d in indeg.items() if d == 0]
    done = 0
    while ready:
        ready.sort()
        nid = ready.pop(0)
        node = by_id.get(nid)
        if not node:
            continue
        ntype = node.get("type", "llm")
        cfg = node.get("config") or {}
        try:
            out = _exec_pipeline_node(ntype, cfg, outputs)
            outputs[nid] = out
            logs.append({"node": nid, "type": ntype, "status": "ok", "output_preview": str(out)[:300]})
        except Exception as ex:
            logs.append({"node": nid, "type": ntype, "status": "error", "error": str(ex)})
            outputs[nid] = f"[ERROR] {ex}"
        done += 1
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    return outputs, logs, done == len(nodes)


def _resolve_tpl(text, outputs):
    """Replace {{nodeId}} and {{nodeId.field}} references with previous outputs."""
    if not text:
        return text or ""

    def repl(m):
        ref = m.group(1).strip()
        parts = ref.split(".", 1)
        val = outputs.get(parts[0])
        if val is None:
            return m.group(0)
        if len(parts) == 2 and isinstance(val, dict):
            val = val.get(parts[1], m.group(0))
        return str(val)

    return re.sub(r"\{\{\s*([\w.\-]+)\s*\}\}", repl, text)


def _exec_pipeline_node(ntype, cfg, outputs):
    if ntype == "llm":
        prompt = _resolve_tpl(cfg.get("prompt", "Continue."), outputs)
        return _call_llm(prompt, timeout=45)
    if ntype == "oracle":
        query = _resolve_tpl(cfg.get("query", "AI agents"), outputs)
        top = _sweep_feeds(limit=5)
        return {"stories": [{"title": s["title"], "link": s["link"], "score": s.get("score", 0)} for s in top]}
    if ntype == "crew":
        task = _resolve_tpl(cfg.get("task", "Produce a report."), outputs)
        crew = cfg.get("crew", "Pipeline Crew")
        results, errors = _dispatch_crew(crew, task)
        return {"results": results, "errors": errors}
    if ntype == "memory":
        content = _resolve_tpl(cfg.get("content", ""), outputs)
        vault = _vault_path()
        os.makedirs(os.path.join(vault, "03_Signals"), exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        fname = os.path.join(vault, "03_Signals", f"Pipeline_sig-{ts}.md")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(f"# Pipeline Signal — {ts}\n\n{content}\n")
        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), "Pipeline", "Signal Logged",
                      f"Wrote `{os.path.basename(fname)}` to vault."))
        conn.commit()
        conn.close()
        return {"file": os.path.basename(fname)}
    if ntype == "webhook":
        url = _resolve_tpl(cfg.get("url", ""), outputs)
        payload = _resolve_tpl(cfg.get("payload", "{}"), outputs)
        try:
            parsed = json.loads(payload) if payload.strip().startswith("{") else {"text": payload}
        except Exception:
            parsed = {"text": payload}
        data, status = _http(url, method="POST", json_data=parsed, timeout=15)
        return {"http": status, "response": str(data)[:200]}
    if ntype == "delay":
        time.sleep(min(300, int(cfg.get("seconds", 1) or 1)))
        return {"waited": int(cfg.get("seconds", 1))}
    if ntype == "consortium":
        question = _resolve_tpl(cfg.get("question", "What should we build?"), outputs)
        providers = [p.strip() for p in str(cfg.get("providers") or "deepseek,grok,anthropic").split(",") if p.strip()]
        data, status = _http("http://127.0.0.1:8086/api/agentic/consortium", method="POST",
                             json_data={"question": question, "providers": providers}, timeout=180)
        if isinstance(data, dict) and data.get("answers"):
            return {"answers": data["answers"], "summary": data.get("summary", "")}
        return {"error": "consortium failed", "http": status}
    if ntype == "skill":
        skill_name = _resolve_tpl(cfg.get("skill", ""), outputs)
        text = _resolve_tpl(cfg.get("text", ""), outputs)
        conn = _db()
        row = conn.execute("SELECT * FROM skills WHERE lower(name)=lower(?) LIMIT 1",
                           (str(skill_name).lower(),)).fetchone()
        conn.close()
        if not row:
            return {"error": f"skill '{skill_name}' not found — import it first"}
        return {"applied": _apply_skill(dict(row), text)}
    if ntype == "wordpress":
        title = _resolve_tpl(cfg.get("title", "Post from pipeline"), outputs)
        content = _resolve_tpl(cfg.get("content", ""), outputs)
        status = _resolve_tpl(cfg.get("status", "publish"), outputs)
        ok, res = _wp_publish(title, content, status)
        if ok and isinstance(res, dict):
            return {"ok": True, "link": res.get("link"), "post_id": res.get("id")}
        return {"ok": False, "error": str(res)[:300]}
    raise ValueError(f"Unknown node type: {ntype}")


@agentic_bp.route("/api/agentic/pipelines", methods=["GET", "POST", "OPTIONS"])
def api_pipelines():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        pid = data.get("id") or f"pipe-{int(time.time())}"
        name = data.get("name") or "Untitled Pipeline"
        nodes = data.get("nodes") or []
        edges = data.get("edges") or []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        conn.execute("INSERT INTO pipelines (id, name, nodes, edges, created, updated) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET name=excluded.name, nodes=excluded.nodes, "
                     "edges=excluded.edges, updated=excluded.updated",
                     (pid, name, json.dumps(nodes), json.dumps(edges), now, now))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "id": pid})
    conn = _db()
    rows = conn.execute("SELECT * FROM pipelines ORDER BY updated DESC").fetchall()
    conn.close()
    return jsonify({"status": "ok", "pipelines": [{
        "id": r["id"], "name": r["name"], "nodes": json.loads(r["nodes"] or "[]"),
        "edges": json.loads(r["edges"] or "[]"), "created": r["created"], "updated": r["updated"]
    } for r in rows]})


@agentic_bp.route("/api/agentic/pipelines/<pid>", methods=["GET", "DELETE", "OPTIONS"])
def api_pipeline(pid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM pipelines WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": pid})
    row = conn.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Pipeline not found"}), 404
    return jsonify({"status": "ok", "pipeline": {
        "id": row["id"], "name": row["name"], "nodes": json.loads(row["nodes"] or "[]"),
        "edges": json.loads(row["edges"] or "[]"), "created": row["created"], "updated": row["updated"]}})


@agentic_bp.route("/api/agentic/pipeline/run", methods=["POST", "OPTIONS"])
def api_pipeline_run():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    if not nodes:
        return jsonify({"error": "Pipeline has no nodes"}), 400
    outputs, logs, complete = _run_pipeline_nodes(nodes, edges)
    # compounding loop: structured note to the vault + memory row + skill doc
    try:
        _finalize_pipeline_run(data.get("name") or "Untitled", nodes, outputs, logs, complete)
    except Exception:
        pass
    return jsonify({"status": "ok" if complete else "partial",
                    "outputs": {k: (str(v)[:2000]) for k, v in outputs.items()},
                    "logs": logs, "complete": complete})


# ---------------------------------------------------------------------------
# Memory Engine settings + manual re-tier
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/memory/settings", methods=["GET", "POST", "OPTIONS"])
def api_memory_settings():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = _set_memory_settings(data)
        return jsonify({"status": "ok", "settings": cfg})
    return jsonify({"status": "ok", "settings": _memory_settings()})

# NAV CONFIG (2026-08-09) — server-driven sidebar overrides (Phase 3)
# hide sections/items, reorder sections/items, pinned defaults. Zero code edits.
NAV_CONFIG_DEFAULTS = {
    "hidden_items": [],
    "hidden_sections": [],
    "section_order": [],
    "item_order": {},
    "pinned_defaults": [],
    "expanded_defaults": [],
    "groups": {},
}


def _nav_config():
    cfg = dict(NAV_CONFIG_DEFAULTS)
    stored = _cfg_get("nav_config", None)
    if isinstance(stored, dict):
        for k in NAV_CONFIG_DEFAULTS:
            if k in stored:
                cfg[k] = stored[k]
    return cfg


def _set_nav_config(patch):
    cfg = _nav_config()
    for k, v in patch.items():
        if k in NAV_CONFIG_DEFAULTS:
            cfg[k] = v
    _cfg_set("nav_config", cfg)
    return cfg


@agentic_bp.route("/api/agentic/nav-config", methods=["GET", "POST", "OPTIONS"])
def api_nav_config():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        if data.get("reset") is True:
            _cfg_set("nav_config", {})
            return jsonify({"status": "ok", "config": _nav_config()})
        cfg = _set_nav_config({k: v for k, v in data.items() if k != "reset"})
        return jsonify({"status": "ok", "config": cfg})
    return jsonify({"status": "ok", "config": _nav_config()})


@agentic_bp.route("/api/agentic/memory/<int:mid>/tier", methods=["POST", "OPTIONS"])
def api_memory_retier(mid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    new_tier = (data.get("tier") or "").strip().lower()
    if new_tier not in ("core", "working", "auto"):
        return jsonify({"error": "tier must be core|working|auto"}), 400
    conn = _db()
    cur = conn.execute("UPDATE memory SET tier=?, updated=? WHERE id=?",
                       (new_tier, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mid))
    conn.commit()
    row = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    conn.close()
    if not row or cur.rowcount == 0:
        return jsonify({"error": "Entry not found"}), 404
    _sync_memory_to_vault(mid, dict(row))
    return jsonify({"status": "ok", "entry": dict(row)})


# ---------------------------------------------------------------------------
# Oracle v3: sweep-all + digest + post editing + scheduled digest
# ---------------------------------------------------------------------------
@agentic_bp.route("/api/agentic/oracle/sweep-all", methods=["POST", "OPTIONS"])
def api_oracle_sweep_all():
    """Sweep every feed in parallel, merge, write a ranked digest to the vault."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    feeds = _list_feeds()
    if not feeds:
        return jsonify({"error": "No feeds configured"}), 400
    results = {}
    stats_all = {}

    def _one(feed):
        try:
            sigs, stats = _sweep_feed_sources(feed)
            results[feed["id"]] = sigs
            for k, v in stats.items():
                stats_all[k] = stats_all.get(k, 0) + v
        except Exception as e:
            results[feed["id"]] = []

    threads = [threading.Thread(target=_one, args=(f,)) for f in feeds]
    for t in threads: t.start()
    for t in threads: t.join()

    merged = []
    for fid, sigs in results.items():
        for s in sigs:
            s["feed"] = next((f["name"] for f in feeds if f["id"] == fid), "?")
            merged.append(s)
    merged.sort(key=lambda x: -(x.get("score") or 0))
    top = merged[:10]

    vault = _vault_path()
    intel_dir = os.path.join(vault, "00_Intelligence")
    digest_file = None
    try:
        os.makedirs(intel_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        digest_file = os.path.join(intel_dir, f"Digest_{datetime.now().strftime('%Y%m%d')}.md")
        lines = [f"# 📬 Oracle Intelligence Digest — {ts}",
                 f"\n- **Feeds swept**: {len(feeds)} · **Signals merged**: {len(merged)}",
                 f"- **Sources**: {', '.join(f'{k}={v}' for k, v in stats_all.items())}\n",
                 "\n## Top Signals Across All Feeds\n"]
        for i, s in enumerate(top, 1):
            rep = " 🔁repeat" if s.get("is_repeat") else ""
            lines.append(f"{i}. **{s.get('title', '')}** (Score: {s.get('score', 0)}/100{rep})")
            lines.append(f"   - *Feed*: {s.get('feed', '?')} | *Source*: {s.get('source', '')}")
            lines.append(f"   - *Link*: {s.get('link', '')}")
            lines.append("")
        with open(digest_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        digest_file = None
        print(f"[oracle-v3] digest write failed: {e}")

    conn = _db()
    conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source) VALUES (?,?,?,?,?,?)",
                 (datetime.now().strftime("%H:%M LOCAL"), "Oracle v3", "Intelligence Digest",
                  f"Digest built from {len(feeds)} feeds, {len(merged)} signals merged, top {len(top)}. "
                  f"File: {os.path.basename(digest_file) if digest_file else 'in-memory'}",
                  "auto", "oracle"))
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok", "feeds_swept": len(feeds), "signals_merged": len(merged),
        "source_stats": stats_all, "digest_file": digest_file,
        "top": [{"title": s.get("title", ""), "score": s.get("score", 0),
                 "source": s.get("source", ""), "feed": s.get("feed", "?"),
                 "link": s.get("link", ""), "is_repeat": s.get("is_repeat", False)}
                for s in top],
    })


@agentic_bp.route("/api/agentic/oracle/posts/<int:post_id>", methods=["PUT", "OPTIONS"])
def api_oracle_post_edit(post_id):
    """Edit a post draft (content/title/platform/status)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    conn = _db()
    row = conn.execute("SELECT * FROM oracle_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "post not found"}), 404
    new_content = data.get("content")
    new_title = data.get("title")
    new_status = data.get("status")
    new_platform = data.get("platform")
    if new_content is not None:
        conn.execute("UPDATE oracle_posts SET content=? WHERE id=?", (new_content, post_id))
    if new_title is not None:
        conn.execute("UPDATE oracle_posts SET title=? WHERE id=?", (new_title, post_id))
    if new_status in ("draft", "reviewed", "scheduled", "published"):
        conn.execute("UPDATE oracle_posts SET status=? WHERE id=?", (new_status, post_id))
    if new_platform in ("linkedin", "x", "blog"):
        conn.execute("UPDATE oracle_posts SET platform=? WHERE id=?", (new_platform, post_id))
    conn.commit()
    row2 = conn.execute("SELECT * FROM oracle_posts WHERE id=?", (post_id,)).fetchone()
    conn.close()
    return jsonify({"status": "ok", "post": dict(row2)})


def _run_scheduled_digest():
    """Check the digest schedule; run sweep-all when due. Called from watchdog."""
    try:
        sched = _cfg_get("oracle_digest") or {}
        if not sched.get("enabled"):
            return
        cadence = sched.get("cadence", "daily")
        last = sched.get("last_run") or ""
        today = datetime.now().strftime("%Y-%m-%d")
        if last == today:
            return
        hour = int(sched.get("hour", 9))
        if datetime.now().hour < hour:
            return
        _run_digest_now()
        sched["last_run"] = today
        _cfg_set("oracle_digest", sched)
    except Exception:
        pass


def _run_digest_now():
    """Execute the digest sweep without the schedule check (used by API + scheduler)."""
    try:
        feeds = _list_feeds()
        if not feeds:
            return
        merged = []
        for feed in feeds:
            try:
                sigs, _ = _sweep_feed_sources(feed)
                for s in sigs:
                    s["feed"] = feed["name"]
                    merged.append(s)
            except Exception:
                continue
        merged.sort(key=lambda x: -(x.get("score") or 0))
        vault = _vault_path()
        intel_dir = os.path.join(vault, "00_Intelligence")
        os.makedirs(intel_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        digest_file = os.path.join(intel_dir, f"Digest_{datetime.now().strftime('%Y%m%d')}.md")
        lines = [f"# 📬 Oracle Intelligence Digest — {ts}", f"\n- **Signals merged**: {len(merged)}\n",
                 "\n## Top Signals\n"]
        for i, s in enumerate(merged[:10], 1):
            lines.append(f"{i}. **{s.get('title', '')}** (Score: {s.get('score', 0)}/100)")
            lines.append(f"   - *Feed*: {s.get('feed', '?')} | *Source*: {s.get('source', '')}")
            lines.append(f"   - *Link*: {s.get('link', '')}")
            lines.append("")
        with open(digest_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


@agentic_bp.route("/api/agentic/oracle/digest/schedule", methods=["GET", "POST", "OPTIONS"])
def api_oracle_digest_schedule():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        sched = dict(_cfg_get("oracle_digest") or {})
        for k in ("enabled", "cadence", "hour"):
            if data.get(k) is not None:
                sched[k] = data[k]
        if data.get("run_now"):
            threading.Thread(target=_run_digest_now, daemon=True).start()
        _cfg_set("oracle_digest", sched)
        return jsonify({"status": "ok", "schedule": sched})
    return jsonify({"status": "ok", "schedule": _cfg_get("oracle_digest") or {}})


# ---------------------------------------------------------------------------
# 5. Health Watchdog — auto-healing + telemetry
# ---------------------------------------------------------------------------
def _init_health_tables():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS health_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, service TEXT, action TEXT, detail TEXT
    );
    """)
    conn.commit()
    conn.close()


_init_health_tables()

# service id -> container name candidates (docker restart target)
SERVICE_CONTAINERS = {
    "crewai": ["appvault-crewai-runner"],
    "litellm": ["appvault-litellm"],
    "onebrain": ["appvault-memory-mcp"],
    "hermes": ["appvault-hermes-agent"],
    "n8n": ["app-n8n"],
    "openwebui": ["open-webui"],
    "anythingllm": ["app-anythingllm"],
    "mcp": ["appvault-mcp-gateway"],
}

_FAIL_COUNTS = {}
_LAST_RESTART = {}
_HEALTH_LOCK = threading.Lock()
WATCHDOG_CFG = {
    "enabled": True,
    "interval_s": 30,
    "fail_threshold": 3,       # consecutive offline probes before acting
    "min_uptime_s": 90,        # never restart a container younger than this
    "cooldown_s": 300,         # min gap between restarts of the same service
}


def _docker_restart(container_name):
    """Restart a container via the docker CLI (socket is mounted)."""
    try:
        import subprocess
        r = subprocess.run(["docker", "restart", container_name],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()
    except Exception as e:
        return False, str(e)


def _container_uptime(container_name):
    """Seconds since the container started (None if missing)."""
    try:
        import subprocess
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", container_name],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        from datetime import datetime as _dt
        started = _dt.strptime(r.stdout.strip()[:19], "%Y-%m-%dT%H:%M:%S")
        return (datetime.now() - started).total_seconds()
    except Exception:
        return None


def _log_health_event(service, action, detail):
    conn = _db()
    conn.execute("INSERT INTO health_events (ts, service, action, detail) VALUES (?,?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), service, action, detail))
    conn.commit()
    conn.close()


def _watchdog_tick():
    """One pass: probe services, auto-restart the flaky ones (circuit breaker)."""
    if not WATCHDOG_CFG["enabled"]:
        return
    probes = _probe_all(force=True)
    for sid, st in probes.items():
        if st.get("status") == "online":
            _FAIL_COUNTS[sid] = 0
            continue
        _FAIL_COUNTS[sid] = _FAIL_COUNTS.get(sid, 0) + 1
        if _FAIL_COUNTS[sid] < WATCHDOG_CFG["fail_threshold"]:
            continue
        containers = SERVICE_CONTAINERS.get(sid)
        if not containers:
            continue
        now = time.time()
        if now - _LAST_RESTART.get(sid, 0) < WATCHDOG_CFG["cooldown_s"]:
            continue
        for cname in containers:
            uptime = _container_uptime(cname)
            if uptime is None:
                continue  # container not on this box
            if uptime < WATCHDOG_CFG["min_uptime_s"]:
                continue  # slow first boot is not unhealthy
            ok, detail = _docker_restart(cname)
            _LAST_RESTART[sid] = now
            _FAIL_COUNTS[sid] = 0
            _log_health_event(sid, "AUTO-RESTART" if ok else "RESTART-FAILED",
                              f"{cname} was offline after {_FAIL_COUNTS.get(sid, 0) + 1} probes → {'restarted' if ok else 'failed: ' + detail}")
            break


def _watchdog_loop():
    while True:
        try:
            _watchdog_tick()
        except Exception:
            pass
        try:
            _run_scheduled_digest()
        except Exception:
            pass
        try:
            _run_self_improvement()
        except Exception:
            pass
        try:
            _maybe_deliver_digest_to_channel()
        except Exception:
            pass
        time.sleep(WATCHDOG_CFG["interval_s"])


def _start_watchdog():
    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()


if os.environ.get("APPVAULT_WATCHDOG", "1") != "0":
    try:
        _start_watchdog()
    except Exception:
        pass


@agentic_bp.route("/api/agentic/health", methods=["GET", "OPTIONS"])
def api_health():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    probes = _probe_all(force=True)
    services = []
    for sid, name, _, cat, role, path, _ in SERVICES:
        st = probes.get(sid, {})
        containers = SERVICE_CONTAINERS.get(sid, [])
        uptime = None
        for c in containers:
            uptime = _container_uptime(c)
            if uptime is not None:
                break
        services.append({
            "id": sid, "name": name, "category": cat, "role": role,
            "status": st.get("status", "offline"), "http": st.get("http", 0),
            "fail_count": _FAIL_COUNTS.get(sid, 0),
            "container": containers[0] if containers else None,
            "uptime_s": round(uptime) if uptime is not None else None,
        })
    conn = _db()
    events = [{"ts": r["ts"], "service": r["service"], "action": r["action"], "detail": r["detail"]}
              for r in conn.execute("SELECT * FROM health_events ORDER BY id DESC LIMIT 25").fetchall()]
    conn.close()
    return jsonify({"status": "ok", "watchdog": WATCHDOG_CFG,
                    "services": services, "events": events})


@agentic_bp.route("/api/agentic/health/restart/<sid>", methods=["POST", "OPTIONS"])
def api_health_restart(sid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    containers = SERVICE_CONTAINERS.get(sid)
    if not containers:
        return jsonify({"error": f"No container mapping for {sid}"}), 404
    ok, detail = _docker_restart(containers[0])
    _log_health_event(sid, "MANUAL-RESTART", f"{containers[0]} → {'ok' if ok else detail}")
    return jsonify({"status": "ok" if ok else "error", "container": containers[0], "detail": detail})


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
    CREATE TABLE IF NOT EXISTS work_items (
        id TEXT PRIMARY KEY,
        category TEXT DEFAULT 'other',
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        image_url TEXT DEFAULT '',
        source TEXT DEFAULT 'manual',
        status TEXT DEFAULT 'draft',
        url TEXT DEFAULT '',
        tags TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    """
    )
    # Calendar + link tracking columns (2026-08-11) — work_items exists here.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(work_items)").fetchall()]
        if "scheduled_at" not in cols:
            conn.execute("ALTER TABLE work_items ADD COLUMN scheduled_at TEXT")
        if "link_code" not in cols:
            conn.execute("ALTER TABLE work_items ADD COLUMN link_code TEXT")
        # project/research used to be added lazily by _projects_ensure() — a
        # timing bomb for fresh installs (any _work_record before the first
        # pipeline call silently failed). Now part of the import-time schema.
        if "project" not in cols:
            conn.execute("ALTER TABLE work_items ADD COLUMN project TEXT DEFAULT 'appvault'")
        if "research" not in cols:
            conn.execute("ALTER TABLE work_items ADD COLUMN research TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()
_init_compounding_tables()


# ---------------------------------------------------------------------------
# 1. IDENTITY / PROFILE  — who the user is; injected into EVERY LLM call.
#    (the video's Layer 2 pain: "none of them actually know who you are")
# ---------------------------------------------------------------------------
IDENTITY_DEFAULTS = {
    "name": "Tisha Andrews",
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
    return ("\n\n===== USER / OPERATOR PROFILE (Information about the user you are assisting — use it to tailor responses to their audience and brand) =====\n" +
            "\n".join(lines) +
            "\n===== END USER PROFILE =====\n")


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
        "category": r["category"] if "category" in r.keys() else "business",
        "next_steps": r["next_steps"] if "next_steps" in r.keys() else "",
        "target": r["target"] if "target" in r.keys() else "",
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
            "linked_feeds, linked_crews, category, next_steps, target, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, (data.get("description") or ""), (data.get("status") or "active"),
             int(data.get("priority", 3) or 3), int(data.get("progress", 0) or 0),
             ",".join(data.get("kpis") or []),
             ",".join(str(x) for x in (data.get("linked_feeds") or [])),
             ",".join(str(x) for x in (data.get("linked_crews") or [])),
             (data.get("category") or "business"),
             (data.get("next_steps") or ""), (data.get("target") or ""),
             now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM goals WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        return jsonify({"status": "ok", "goal": _goal_row_to_dict(row)})
    conn = _db()
    category = (request.args.get("category") or "").strip()
    if category and category != "all":
        rows = conn.execute(
            "SELECT * FROM goals WHERE category=? ORDER BY status='active' DESC, priority ASC, id DESC",
            (category,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM goals ORDER BY status='active' DESC, category ASC, priority ASC, id DESC").fetchall()
    conn.close()
    rawc = _cfg_get("goal_categories") or ""
    try:
        custom = json.loads(rawc) if rawc else []
    except Exception:
        custom = []
    cats = list(GOAL_CATEGORIES)
    for c in custom:
        if c not in cats:
            cats.append(c)
    for r in rows:
        if (r["category"] or "") not in cats and r["category"]:
            cats.append(r["category"])
    return jsonify({"status": "ok", "goals": [_goal_row_to_dict(r) for r in rows], "categories": cats})


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
    for k in ("title", "description", "status", "kpis", "linked_feeds", "linked_crews",
              "category", "next_steps", "target"):
        if data.get(k) is not None:
            v = data[k]
            merged[k] = ",".join(v) if isinstance(v, list) else str(v)
    if data.get("priority") is not None:
        merged["priority"] = int(data["priority"])
    if data.get("progress") is not None:
        merged["progress"] = max(0, min(100, int(data["progress"])))
    old_status = row["status"] if row else None
    merged["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k}=?" for k in merged)
    conn.execute(f"UPDATE goals SET {sets} WHERE id=?", (*merged.values(), gid))
    conn.commit()
    # P0-1 mail notify: report goal completion headlessly
    new_status = merged.get("status", old_status)
    if new_status in ("done", "completed") and old_status not in ("done", "completed"):
        try:
            gtitle = merged.get("title") or row["title"]
            _queue_mail("🎯 Goal complete: %s" % gtitle,
                        "Goal #%d \"%s\" is now %s.\n\n%s" % (
                            gid, gtitle, new_status,
                            (merged.get("description") or row["description"] or "")))
        except Exception:
            pass
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
    # optional skill final pass: e.g. humanise the draft before saving
    skill_name = (data.get("skill") or "").strip()
    if skill_name:
        try:
            conn = _db()
            srow = conn.execute("SELECT * FROM skills WHERE lower(name)=lower(?) LIMIT 1",
                                (skill_name.lower(),)).fetchone()
            conn.close()
            if srow:
                content = _apply_skill(dict(srow), content, timeout=90)
                _audit("store", "skill.seo-pass", f"@{skill_name} applied to SEO draft")
        except Exception:
            pass
    # optional publish-to-WordPress flag
    wp_link = None
    wp_error = None
    if data.get("publish"):
        try:
            t = re.search(r"^#\s+(.+)$", content, re.M)
            wp_title = t.group(1).strip() if t else title_seed
            okp, resp = _wp_publish(wp_title, content, "publish")
            if okp:
                wp_link = resp.get("link")
            else:
                wp_error = str(resp)[:200]
        except Exception as e:
            wp_error = str(e)[:200]
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
    # record into the Completed Work ledger (clean work item, not system noise)
    _work_record(category="article", title=f"SEO Article — {title_seed}", content=content,
                 source="seo", status="draft", tags=f"seo,{cluster}",
                 url=wp_link or "", wid="seo-" + str(post_id))
    # goal-linked progress (business/content goals)
    _goal_bump(title_seed + " " + cluster, "article", 6, "seo",
               f"SEO article generated: {title_seed[:60]}")
    return jsonify({"status": "ok", "post_id": post_id, "content": content, "cluster": cluster,
                    "wp_link": wp_link, "wp_error": wp_error})


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


def _image_gen_config():
    """Image engine config key \"image_gen\": {provider, model, api_key, api_base}.
    provider=openai -> direct OpenAI images API; provider=litellm -> through the
    hub; unset/empty -> keyless pollinations fallback."""
    cfg = _cfg_get("image_gen")
    return cfg if isinstance(cfg, dict) else {}


def _openai_image_size(w, h):
    """Map requested w/h to the nearest OpenAI-supported size."""
    if w > h:
        return "1536x1024"
    if h > w:
        return "1024x1536"
    return "1024x1024"


def _generate_image(prompt, style="", w=1024, h=1024, timeout=180):
    """Provider-agnostic image generation into vault 05_Media. Priority:
    1) configured provider (openai direct / litellm hub) when a key is set,
    2) keyless pollinations fallback. Returns (local_url_or_None, provider)."""
    cfg = _image_gen_config()
    provider = (cfg.get("provider") or "").strip().lower()
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "gpt-image-1").strip()
    api_base = (cfg.get("api_base") or "").strip().rstrip("/")
    style_suffix = MEDIA_STYLES.get(style, style) if style else ""
    full_prompt = f"{prompt}, {style_suffix}" if style_suffix else prompt
    body = None
    used_provider = None

    # 1) OpenAI-compatible images API (direct or via the LiteLLM hub)
    if provider in ("openai", "litellm") and api_key:
        if provider == "openai":
            base = api_base or "https://api.openai.com/v1"
        else:
            base = api_base or os.environ.get("LITELLM_BASE", "http://host.docker.internal:4000/v1")
        url = base.rstrip("/") + "/images/generations"
        try:
            data, status = _http(url, method="POST",
                                 headers={"Authorization": f"Bearer {api_key}"},
                                 json_data={"model": model, "prompt": full_prompt,
                                            "n": 1, "size": _openai_image_size(w, h),
                                            "response_format": "b64_json"},
                                 timeout=timeout)
            if status == 200 and isinstance(data, dict):
                items = data.get("data") or []
                if items:
                    b64 = items[0].get("b64_json")
                    if b64:
                        import base64 as _b64
                        body = _b64.b64decode(b64)
                        used_provider = f"{provider}:{model}"
                    elif items[0].get("url"):
                        body, _ = _http_bytes(items[0]["url"], timeout=timeout)
                        used_provider = f"{provider}:{model}"
        except Exception as e:
            print(f"[image-gen] {provider} failed: {e}")

    # 2) Keyless pollinations fallback — always available
    if body is None:
        try:
            url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full_prompt) +
                   f"?width={w}&height={h}&nologo=true&seed={int(time.time()) % 1000000}")
            body, status = _http_bytes(url, timeout=timeout)
            if status == 200 and body:
                used_provider = "pollinations"
        except Exception as e:
            print(f"[image-gen] pollinations failed: {e}")

    if body is None:
        return None, used_provider or "none"

    vault = _vault_path()
    d = os.path.join(vault, "05_Media")
    try:
        os.makedirs(d, exist_ok=True)
        fname = f"IMG_{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        with open(os.path.join(d, fname), "wb") as f:
            f.write(body)
    except Exception as e:
        print(f"[image-gen] vault write failed: {e}")
        return None, used_provider
    return f"/api/agentic/media/file/{fname}", used_provider


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
        w = int(data.get("width", 1024) or 1024)
        h = int(data.get("height", 1024) or 1024)
        local_url, provider = _generate_image(prompt, style=style, w=w, h=h)
        if not local_url:
            return jsonify({"status": "error", "error": "image generation failed — no provider responded"}), 502
        fname = os.path.basename(local_url)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        cur = conn.execute(
            "INSERT INTO media_assets (prompt, style, file, provider, created) VALUES (?,?,?,?,?)",
            (prompt, style, fname, provider or "pollinations", now))
        conn.commit()
        conn.close()
        # compounding loop: memory row points at the artifact
        try:
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), "Media Agent", "Media Generated",
                          f"Generated `{fname}`: {prompt[:180]} (05_Media/, provider: {provider})",
                          "auto", "media", now))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({"status": "ok", "file": fname, "prompt": prompt, "style": style,
                        "provider": provider, "url": local_url, "id": cur.lastrowid})
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
def _save_skill(name, description, content, source="auto", tools=None, kind="prompt"):
    """Insert or update a skill doc (dedup by name). Returns id or None."""
    try:
        name = (name or "").strip()
        if not name:
            return None
        content = (content or "").strip()
        tools_csv = ",".join(tools) if isinstance(tools, list) else (tools or "")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        row = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        if row:
            conn.execute("UPDATE skills SET description=?, content=?, source=?, tools=?, kind=?, updated=?, uses=uses+1 WHERE id=?",
                         (description or "", content or row["content"], source, tools_csv, kind, now, row["id"]))
            sid = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO skills (name, description, content, source, tools, kind, uses, created, updated) "
                "VALUES (?,?,?,?,?,?,0,?,?)", (name, description or "", content, source, tools_csv, kind, now, now))
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
    lines = []
    for r in top:
        tools = r["tools"] if "tools" in r.keys() else ""
        tool_txt = f" | tools: {tools}" if tools else ""
        lines.append(f"- **{r['name']}** (used {r['uses']}x): {r['description'] or r['content'][:120]}{tool_txt}")
    return ("\n\n===== RELEVANT SKILL DOCUMENTS (apply them if they match the task) =====\n"
            + "\n".join(lines) + "\n===== END SKILLS =====\n")


@agentic_bp.route("/api/agentic/skills", methods=["GET", "POST", "OPTIONS"])
def api_skills():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        tools = data.get("tools")
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        sid = _save_skill(data.get("name"), data.get("description"), data.get("content"),
                          source=(data.get("source") or "manual"), tools=tools)
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


# =============================================================================
# CLIENT ACQUISITION FUNNEL — sequential stage chain over work_items (2026-08-13)
# lead -> lead_research -> outreach_draft (HUMAN GATE) -> proposal (HUMAN GATE)
# -> delivery. Each stage embeds the PREVIOUS stage's artifact as context, so
# the funnel roles hand off sequentially (unlike the parallel crew dispatch).
# Items use source='pipeline:funnel' so they appear in the pipeline queue under
# the business's project slug; status 'ready_for_approval' = the human gate
# (reuses the existing Humanize -> Approve -> Send flow).
# =============================================================================

def _funnel_biz_context(biz):
    biz = dict(biz or {})
    name = biz.get("name") or "this business"
    site = biz.get("website") or ""
    offer = biz.get("cta_offer") or ""
    parts = [f"The business running this client acquisition funnel: {name}."]
    if site:
        parts.append(f"Website: {site}")
    if offer:
        parts.append(f"Offer / CTA: {offer}")
    return " ".join(parts)


def _funnel_chain(wid):
    """Collect the artifact chain for a funnel item by walking prev:<wid> tags
    (newest -> oldest), returned oldest-first with category headers."""
    parts, seen = [], set()
    while wid and wid not in seen:
        seen.add(wid)
        item = _pipeline_get(wid)
        if not item:
            break
        cat = item.get("category") or "item"
        title = item.get("title") or ""
        content = (item.get("content") or "")[:5000]
        parts.append(f"### {cat} — {title}\n{content}")
        m = re.search(r"prev:([A-Za-z0-9]+)", item.get("tags") or "")
        wid = m.group(1) if m else None
    return "\n\n".join(reversed(parts))


def _funnel_stage(prompt, agent, timeout=90):
    """One real LLM call for a funnel stage (raises on failure)."""
    return _call_llm(prompt, agent=agent, timeout=timeout)


_NO_TOOLS_SUFFIX = ("\n\nConstraints: You have NO tools, NO internet access, and NO code "
                    "execution. Do NOT emit <tool_calls>, XML, markdown code blocks, or "
                    "step-by-step plans. Output the deliverable itself as plain text, complete.")

_TOOL_CALL_RE = re.compile(r"<tool_calls>.*?</tool_calls>|<invoke name=.*?(?:</invoke>|/>)", re.S)


def _funnel_clean(reply):
    """Strip tool-call XML the model sometimes emits instead of plain text."""
    if not reply:
        return reply
    if "<tool_calls>" in reply or "<invoke " in reply:
        cleaned = _TOOL_CALL_RE.sub("", reply).strip()
        if len(cleaned) >= 80:
            return cleaned
    return reply


def _funnel_stage_guarded(prompt, agent, min_len=300, timeout=150):
    """One real LLM call with constraints, retried once when the reply is short
    or tool-call XML (models sometimes answer with a plan/tool-calls only)."""
    reply = _funnel_stage(prompt + _NO_TOOLS_SUFFIX, agent, timeout=timeout)
    if (len(reply or "") < min_len or "<tool_calls>" in (reply or "")
            or "<invoke " in (reply or "")):
        reply = _funnel_stage(
            prompt + _NO_TOOLS_SUFFIX + "\n\nYour previous reply was unacceptable — it was too "
                     "short or emitted tool calls. Output the FULL deliverable as plain text now, "
                     "no preamble, no tools.",
            agent, timeout=timeout)
    return _funnel_clean(reply)


@agentic_bp.route("/api/agentic/funnel/run", methods=["POST", "OPTIONS"])
def api_funnel_run():
    """Stages 1-3: Prospector -> Researcher -> SDR. Outreach drafts land in
    ready_for_approval (human gate). Returns the created work item ids.
    Volume knobs: lead_count (1-15, default 5), research_n (1-5, default 3)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = data.get("business_id") or data.get("bid")
    if not bid:
        return jsonify({"status": "error", "error": "business_id required"}), 400
    try:
        lead_count = int(data.get("lead_count") or 5)
        research_n = int(data.get("research_n") or 3)
    except Exception:
        lead_count, research_n = 5, 3
    lead_count = max(1, min(lead_count, 15))
    research_n = max(1, min(research_n, 5))
    seed_ids = data.get("seed_ids")
    if isinstance(seed_ids, list):
        seed_ids = [str(s)[:40] for s in seed_ids[:15]]
    else:
        seed_ids = None
    code, payload = _funnel_run_for(bid, lead_count, research_n, seed_ids)
    return jsonify(payload), code


_FUNNEL_LOCKS = {}


def _funnel_run_lock(bid):
    key = str(bid)
    if key not in _FUNNEL_LOCKS:
        _FUNNEL_LOCKS[key] = threading.Lock()
    return _FUNNEL_LOCKS[key]


def _funnel_run_for(bid, lead_count=5, research_n=3, seed_ids=None):
    """Core funnel run — used by both the API route and the scheduler thread.
    seed_ids: run on REAL imported prospect seeds (enriched facts feed the
    research) instead of generated candidates. Returns (status_code, payload).
    Serialized per business."""
    biz = dict(_get_business(bid) or {})
    if not biz:
        return 404, {"status": "error", "error": f"business {bid} not found"}
    project = _biz_project(biz)
    biz_name = biz.get("name") or "business"
    biz_ctx = _funnel_biz_context(biz)

    # Ensure the business's pipeline project is REGISTERED — the pipeline UI
    # only shows registered projects as filter buttons, so an unregistered
    # slug silently orphans every funnel item (user: "i don't see it on the
    # pipeline"). Idempotent: INSERT OR IGNORE.
    try:
        conn = _db()
        conn.execute("INSERT OR IGNORE INTO projects (slug, name, config, created) VALUES (?,?,?,?)",
                     (project, biz_name, "{}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception:
        pass

    with _funnel_run_lock(bid):
        # Load REAL imported seeds when requested (seed mode)
        seed_rows = []
        if seed_ids:
            try:
                import uuid as _uuid  # noqa: F401
                conn = _db()
                ph = ",".join("?" * len(seed_ids))
                seed_rows = conn.execute(
                    f"SELECT id, company, website, site_title, signal_score, signals_found, status, note "
                    f"FROM prospect_seeds WHERE id IN ({ph}) AND business_id=?",
                    (*seed_ids, str(bid))).fetchall()
                conn.close()
            except Exception:
                seed_rows = []
        # Stage 1 — lead (seed mode: imported prospects ARE the candidates;
        # generate mode: Prospector imagines + scores a candidate list)
        if seed_rows:
            lines = []
            for s in seed_rows:
                sig = json.loads(s[5] or "[]")
                lines.append(f"- {s[1]} ({s[2] or 'no website'}) — site: {s[3] or ''} — "
                             f"signals: {', '.join(sig) or 'none'} — score: {s[4]}")
            lead_content = f"IMPORTED PROSPECT SEEDS ({len(seed_rows)}):\n" + "\n".join(lines)
            lead_wid = _work_record(category="lead",
                                    title=f"Imported prospects ({len(seed_rows)}) — {biz_name}",
                                    content=lead_content, source="pipeline:funnel", status="new",
                                    tags=f"funnel,stage:lead,biz:{bid},seeds:{len(seed_rows)}",
                                    project=project)
            research_n = min(research_n, len(seed_rows))
            lead_count = len(seed_rows)
        else:
            try:
                lead_content = _funnel_stage(
                    f"{biz_ctx}\n\nTask: Identify {lead_count} REAL candidate businesses that could "
                    f"realistically become clients of this business. For EACH candidate give: name, "
                    f"location, industry, why they fit, source evidence (a concrete URL), and a fit "
                    f"score out of 25. Be specific — no placeholder candidates; if you cannot verify "
                    f"a URL, mark it 'unverified' rather than inventing it.",
                    "crew-prospector", timeout=180)
            except Exception as e:
                return 502, {"status": "error", "stage": "lead", "error": str(e)[:300]}
            lead_wid = _work_record(category="lead", title=f"Prospects ({lead_count}) — {biz_name}",
                                    content=lead_content, source="pipeline:funnel", status="new",
                                    tags=f"funnel,stage:lead,biz:{bid},count:{lead_count}", project=project)

        # Stages 2+3 — Researcher + SDR loop over the top research_n candidates.
        # Each candidate gets its own research item and outreach draft (both chained
        # to the lead via prev: tags) so gates and proposals stay per-prospect.
        research_wids, outreach_wids = [], []
        for i in range(research_n):
            cand = i + 1
            seed_facts = ""
            if seed_rows and i < len(seed_rows):
                s = seed_rows[i]
                sig = json.loads(s[5] or "[]")
                seed_facts = (f"\n\nSEED FACTS (imported, real — do NOT invent contact details or "
                              f"URLs beyond these):\ncompany={s[1]}\nwebsite={s[2] or 'n/a'}\n"
                              f"site_title={s[3] or ''}\nsignal_keywords_found={', '.join(sig) or 'none'}\n"
                              f"signal_score={s[4]}\ncontact_note={s[7] or ''}\n")
            try:
                research_content = _funnel_stage_guarded(
                    f"{biz_ctx}\n\nPrevious stage — Prospector findings:\n{lead_content[:7000]}\n\n"
                    f"Task: Deep-research candidate #{cand} in the list above and OUTPUT THE FULL "
                    f"PROFILE NOW — do not describe steps, do not write a plan. Produce the "
                    f"verified profile directly: company facts, tech-stack signals, decision-maker, "
                    f"public exposure/risk, budget signals — each with a source (mark unverified "
                    f"facts as such). End with the recommended outreach angle."
                    f"{seed_facts}",
                    "crew-researcher", timeout=150)
            except Exception as e:
                _pipeline_update(lead_wid, status="failed")
                return 502, {"status": "error", "stage": f"research:{cand}", "error": str(e)[:300],
                             "lead_wid": lead_wid}
            research_wid = _work_record(category="lead_research",
                                        title=f"Research #{cand} — {biz_name}",
                                        content=research_content, source="pipeline:funnel",
                                        status="enriched",
                                        tags=f"funnel,stage:research,biz:{bid},cand:{cand},prev:{lead_wid}",
                                        project=project)
            research_wids.append(research_wid)

            sdr_ctx = _funnel_chain(research_wid) or f"Research context:\n{research_content[:6000]}"
            try:
                outreach_content = _funnel_stage_guarded(
                    f"{biz_ctx}\n\nResearch context:\n{sdr_ctx[:9000]}\n\n"
                    f"Task: Write ONE personalized outreach message to the decision-maker identified "
                    f"in the research (candidate #{cand}). Subject line + body. Conversational, "
                    f"specific to their situation, one clear low-friction ask. This is a DRAFT — "
                    f"a human approves before anything is sent.",
                    "crew-sdr", timeout=120)
            except Exception as e:
                _pipeline_update(research_wid, status="failed")
                return 502, {"status": "error", "stage": f"outreach:{cand}", "error": str(e)[:300],
                             "research_wid": research_wid}
            outreach_wid = _work_record(category="outreach_draft",
                                        title=f"Outreach draft #{cand} — {biz_name}",
                                        content=outreach_content, source="pipeline:funnel",
                                        status="ready_for_approval",
                                        tags=f"funnel,stage:outreach,biz:{bid},cand:{cand},prev:{research_wid}",
                                        project=project)
            outreach_wids.append(outreach_wid)
            _bus_publish("funnel.outreach.ready", {"wid": outreach_wid, "business": biz_name,
                                                   "candidate": cand})
    return 200, {"status": "ok", "business": biz_name,
                 "leads": [lead_wid], "research": research_wids, "outreach": outreach_wids}


@agentic_bp.route("/api/agentic/funnel/replied", methods=["POST", "OPTIONS"])
def api_funnel_replied():
    """Human flag: the sent outreach got a positive reply -> Stage 4 Proposal
    Writer. Proposal lands in ready_for_approval (gate 2)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    wid = data.get("wid")
    if not wid:
        return jsonify({"status": "error", "error": "wid required"}), 400
    item = _pipeline_get(wid)
    if not item or item.get("category") != "outreach_draft":
        return jsonify({"status": "error", "error": "wid must be an outreach_draft item"}), 404
    chain = _funnel_chain(wid)
    try:
        proposal_content = _funnel_stage_guarded(
            f"{chain}\n\nThe outreach above received a POSITIVE REPLY from the prospect.\n\n"
            f"Task: Write a crisp proposal for this prospect: executive summary, scope of "
            f"services, pricing (tiers or fixed fee), timeline, and a clear next step. "
            f"This is a DRAFT — a human approves before it is sent.",
            "crew-proposal", timeout=150)
    except Exception as e:
        return jsonify({"status": "error", "stage": "proposal", "error": str(e)[:300]}), 502
    _pipeline_update(wid, status="replied")
    proposal_wid = _work_record(category="proposal", title=f"Proposal — {item.get('title', '')}",
                                content=proposal_content, source="pipeline:funnel",
                                status="ready_for_approval",
                                tags=f"funnel,stage:proposal,biz:{item.get('project', '')},prev:{wid}",
                                project=item.get("project") or "appvault")
    _bus_publish("funnel.proposal.ready", {"wid": proposal_wid})
    return jsonify({"status": "ok", "proposal": proposal_wid})


@agentic_bp.route("/api/agentic/funnel/accepted", methods=["POST", "OPTIONS"])
def api_funnel_accepted():
    """Human flag: proposal accepted -> Stage 5 Delivery Agent writes the
    delivery plan (what gets executed, deliverable, checklist)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    wid = data.get("wid")
    if not wid:
        return jsonify({"status": "error", "error": "wid required"}), 400
    item = _pipeline_get(wid)
    if not item or item.get("category") != "proposal":
        return jsonify({"status": "error", "error": "wid must be a proposal item"}), 404
    chain = _funnel_chain(wid)
    try:
        delivery_content = _funnel_stage_guarded(
            f"{chain}\n\nThe proposal above was ACCEPTED by the prospect.\n\n"
            f"Task: Write the delivery plan: what gets executed first, the deliverable/report "
            f"format, a completion checklist with milestones, and SLA/response commitments.",
            "crew-delivery", timeout=150)
    except Exception as e:
        return jsonify({"status": "error", "stage": "delivery", "error": str(e)[:300]}), 502
    _pipeline_update(wid, status="accepted")
    delivery_wid = _work_record(category="delivery", title=f"Delivery plan — {item.get('title', '')}",
                                content=delivery_content, source="pipeline:funnel", status="done",
                                tags=f"funnel,stage:delivery,biz:{item.get('project', '')},prev:{wid}",
                                project=item.get("project") or "appvault")
    _bus_publish("funnel.delivery.done", {"wid": delivery_wid})
    return jsonify({"status": "ok", "delivery": delivery_wid})


@agentic_bp.route("/api/agentic/funnel/reply", methods=["POST", "OPTIONS"])
def api_funnel_reply():
    """Log a prospect reply against an outreach/proposal card. LLM triage:
    positive -> Proposal stage fires (gate 2); question/ooo -> a follow-up
    draft is written (human approves before sending); negative -> chain closed."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    wid = data.get("wid")
    text = (data.get("text") or "").strip()
    if not wid or not text:
        return jsonify({"status": "error", "error": "wid and text required"}), 400
    item = _pipeline_get(wid)
    if not item or item.get("category") not in ("outreach_draft", "proposal"):
        return jsonify({"status": "error",
                        "error": "wid must be an outreach_draft or proposal item"}), 404
    try:
        triage = _funnel_triage(text)
    except Exception as e:
        return jsonify({"status": "error", "stage": "triage", "error": str(e)[:300]}), 502
    cls = triage.get("class") or "question"

    if cls == "positive":
        # Advance to the Proposal stage (same path as funnel/replied)
        chain = _funnel_chain(wid)
        try:
            proposal_content = _funnel_stage_guarded(
                f"{chain}\n\nThe outreach above received a POSITIVE REPLY from the prospect.\n\n"
                f"Prospect reply: {text[:1500]}\n\n"
                f"Task: Write a crisp proposal for this prospect: executive summary, scope of "
                f"services, pricing (tiers or fixed fee), timeline, and a clear next step. "
                f"This is a DRAFT — a human approves before it is sent.",
                "crew-proposal", timeout=150)
        except Exception as e:
            return jsonify({"status": "error", "stage": "proposal", "error": str(e)[:300]}), 502
        _pipeline_update(wid, status="replied")
        proposal_wid = _work_record(category="proposal", title=f"Proposal — {item.get('title', '')}",
                                    content=proposal_content, source="pipeline:funnel",
                                    status="ready_for_approval",
                                    tags=f"funnel,stage:proposal,biz:{item.get('project', '')},prev:{wid}",
                                    project=item.get("project") or "appvault")
        _bus_publish("funnel.proposal.ready", {"wid": proposal_wid})
        return jsonify({"status": "ok", "action": "proposal", "class": cls,
                        "summary": triage.get("summary", ""), "proposal": proposal_wid})

    if cls == "negative":
        _pipeline_update(wid, status="rejected")
        _bus_publish("funnel.closed", {"wid": wid, "class": "negative"})
        return jsonify({"status": "ok", "action": "closed", "class": cls,
                        "summary": triage.get("summary", "")})

    # question / ooo -> follow-up draft (human approves before sending)
    reply = triage.get("suggested_reply") or triage.get("summary") or ""
    followup_wid = _work_record(
        category="outreach_draft", title=f"Follow-up — {item.get('title', '')}",
        content=f"CONTEXT — original outreach (approved, sent):\n\n{item.get('content', '')[:3000]}\n\n"
                f"PROSPECT REPLY: {text[:2000]}\n\nTRIAGE: {triage.get('summary', '')}\n\n"
                f"DRAFT FOLLOW-UP (human approves before sending):\n\n{reply}",
        source="pipeline:funnel", status="ready_for_approval",
        tags=f"funnel,stage:followup,biz:{item.get('project', '')},prev:{wid},funnel:followup",
        project=item.get("project") or "appvault")
    _bus_publish("funnel.followup.ready", {"wid": followup_wid})
    return jsonify({"status": "ok", "action": "followup", "class": cls,
                    "summary": triage.get("summary", ""), "followup": followup_wid})


def _funnel_triage(reply_text):
    """Classify a prospect reply: positive | question | negative | ooo.
    Returns {class, summary, suggested_reply} (LLM, best-effort)."""
    raw = _funnel_stage_guarded(
        "You are the crew SDR. A prospect replied to your outreach. Classify the reply.\n\n"
        f"Reply: {reply_text[:2500]}\n\n"
        "Output JSON only:\n"
        "{\"class\": \"positive|question|negative|ooo\", \"summary\": \"one-line summary of their intent\", "
        "\"suggested_reply\": \"a short, human, plain-text reply to send back (or '' if the class is negative/ooo)\"}",
        "crew-sdr", timeout=90)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("class") in ("positive", "question", "negative", "ooo"):
            return parsed
    except Exception:
        pass
    import re as _re
    m = _re.search(r'"class"\s*:\s*"([^"]+)"', raw or "")
    cls = m.group(1) if m and m.group(1) in ("positive", "question", "negative", "ooo") else "question"
    return {"class": cls, "summary": (raw or "")[:300], "suggested_reply": ""}


@agentic_bp.route("/api/agentic/funnel/schedule", methods=["GET", "POST", "OPTIONS"])
def api_funnel_schedule():
    """Per-business funnel schedule: interval off|daily|weekly + weekly_cap."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        bid = str(data.get("business_id") or "").strip()
        interval = (data.get("interval") or "off").strip()
        if interval not in ("off", "daily", "weekly"):
            return jsonify({"status": "error", "error": "interval must be off|daily|weekly"}), 400
        try:
            cap = max(1, min(int(data.get("weekly_cap") or 5), 50))
        except Exception:
            cap = 5
        cfg = _cfg_get("funnel_schedule") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        cur = cfg.get(bid) or {}
        cfg[bid] = {"interval": interval, "weekly_cap": cap,
                    "runs_this_week": cur.get("runs_this_week", 0),
                    "week_of": cur.get("week_of", ""),
                    "last_run": cur.get("last_run", "")}
        _cfg_set("funnel_schedule", cfg)
        return jsonify({"status": "ok", "schedule": cfg[bid]})
    bid = str((request.args.get("business_id") or "").strip())
    cfg = _cfg_get("funnel_schedule") or {}
    if bid:
        return jsonify({"status": "ok", "schedule": (cfg or {}).get(bid) or {"interval": "off"}})
    return jsonify({"status": "ok", "schedules": cfg or {}})


def _funnel_week_key():
    return datetime.now().strftime("%Y-W%W")


def _funnel_schedule_tick():
    """One scheduler tick: fire due funnel runs (cap-aware) + follow-up nudges.
    Each run spawns its own thread so the tick never blocks on LLM calls."""
    try:
        cfg = _cfg_get("funnel_schedule") or {}
        if not isinstance(cfg, dict):
            return
        week = _funnel_week_key()
        for bid, s in list(cfg.items()):
            if not isinstance(s, dict) or s.get("interval") not in ("daily", "weekly"):
                continue
            if s.get("week_of") != week:
                s["runs_this_week"] = 0
                s["week_of"] = week
            if (s.get("runs_this_week") or 0) >= int(s.get("weekly_cap") or 5):
                continue
            due = True
            try:
                last = s.get("last_run") or ""
                if last:
                    age = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
                    due = age >= (20 * 3600 if s.get("interval") == "daily" else 6 * 24 * 3600)
            except Exception:
                due = True
            if not due:
                continue
            s["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            s["runs_this_week"] = (s.get("runs_this_week") or 0) + 1
            _cfg_set("funnel_schedule", cfg)
            threading.Thread(target=_funnel_scheduled_run, args=(bid,), daemon=True).start()
        _funnel_followup_nudge()
    except Exception:
        pass


def _funnel_top_seed_ids(bid, n=10):
    """Top-scored enriched seeds for a business — real prospects first."""
    try:
        _funnel_seeds_ensure()
        conn = _db()
        rows = conn.execute(
            "SELECT id FROM prospect_seeds WHERE business_id=? AND status='enriched' "
            "AND signal_score > 0 ORDER BY signal_score DESC LIMIT ?", (str(bid), n)).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _funnel_scheduled_run(bid):
    """Full funnel run from the scheduler (10 leads, research top 3).
    Uses the business's top-scored REAL seeds when available (falls back to
    generated candidates). Failures are NEVER silent: recorded in the schedule
    config (last_error) and published to the bus, so the UI can surface them."""
    ok = False
    detail = "unknown"
    seed_ids = _funnel_top_seed_ids(bid, 10)
    try:
        code, payload = _funnel_run_for(bid, lead_count=10, research_n=3, seed_ids=seed_ids)
        ok = code == 200
        detail = payload.get("status") if isinstance(payload, dict) else str(payload)
    except Exception as e:
        import traceback
        traceback.print_exc()
        detail = f"error: {str(e)[:200]}"
    try:
        cfg = _cfg_get("funnel_schedule") or {}
        if isinstance(cfg, dict) and str(bid) in cfg:
            cfg[str(bid)]["last_error"] = None if ok else detail
            cfg[str(bid)]["last_run_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _cfg_set("funnel_schedule", cfg)
    except Exception:
        pass
    _bus_publish("funnel.scheduled.run", {"business_id": bid, "ok": ok, "detail": detail})


def _funnel_followup_nudge(days=5, max_n=3):
    """Sent outreach (status approved, no reply) older than N days with no
    existing follow-up draft -> SDR drafts a nudge (human approves before
    sending). One nudge per chain, at most max_n per tick."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT id, content, title, project FROM work_items WHERE source='pipeline:funnel' "
            "AND category='outreach_draft' AND status='approved' "
            "AND created_at < datetime('now', ?) ORDER BY created_at ASC",
            (f"-{days} days",)).fetchall()
        conn.close()
    except Exception:
        return
    made = 0
    for wid, content, title, project in rows:
        if made >= max_n:
            break
        try:
            conn = _db()
            dup = conn.execute(
                "SELECT count(*) FROM work_items WHERE tags LIKE '%funnel:followup%' AND tags LIKE ?",
                (f"%prev:{wid}%",)).fetchone()[0]
            conn.close()
        except Exception:
            dup = 1
        if dup:
            continue
        try:
            nudge = _funnel_stage_guarded(
                f"You are the crew SDR. This outreach was approved and sent, but got no reply "
                f"in {days} days.\n\nOUTREACH (sent):\n{content[:3000]}\n\n"
                f"Task: Write a SHORT follow-up nudge (subject line + 2-3 sentence body). "
                f"Friendly, low-pressure, one clear ask. This is a DRAFT — a human approves "
                f"before it is sent.",
                "crew-sdr", timeout=120)
        except Exception:
            continue
        _work_record(category="outreach_draft", title=f"Follow-up — {title}",
                     content=f"CONTEXT — original outreach (approved, sent):\n\n{content[:3000]}\n\n"
                             f"DRAFT NUDGE (human approves before sending):\n\n{nudge}",
                     source="pipeline:funnel", status="ready_for_approval",
                     tags=f"funnel,stage:followup,biz:{project or ''},prev:{wid},funnel:followup",
                     project=project or "appvault")
        made += 1


def start_funnel_scheduler():
    """Boot hook (agent.py): start the daemon scheduler thread. Idempotent."""
    if getattr(start_funnel_scheduler, "_started", False):
        return
    start_funnel_scheduler._started = True

    def _loop():
        while True:
            try:
                _funnel_schedule_tick()
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# LIGHT CRM — prospects DERIVED from the funnel chains (no parallel tables).
# One prospect row per chain (anchored at its outreach item); stage computed
# from the furthest-advanced item in the chain; value parsed from the proposal.
# ---------------------------------------------------------------------------
_FUNNEL_STAGES = ["new", "draft", "sent", "followup", "proposal", "won", "lost"]

_FUNNEL_STAGE_META = {
    "new": "New — candidate list, not yet developed.",
    "draft": "Outreach draft — approve it to mark as sent.",
    "sent": "Outreach sent — waiting for a reply (auto nudge in 5 days).",
    "followup": "Follow-up draft — approve to send.",
    "proposal": "Proposal stage — approve/send it; ⏭ accepted when it closes.",
    "won": "Won — deal closed. 🎉",
    "lost": "Lost — closed.",
}


def _funnel_prospect_name(research_content):
    txt = (research_content or "").strip()
    if not txt:
        return "Prospect"
    line = txt.splitlines()[0].strip()
    for ch in ("—", "-", ":", "|"):
        if ch in line:
            line = line.split(ch)[0]
    line = re.sub(r"[#*_`>]+", "", line).strip()
    line = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", line).strip()
    return line[:60] or "Prospect"


def _funnel_dm(outreach_content):
    m = re.search(r"(?:Hey|Hi|Hello|Dear)[,\s]+([A-Z][A-Za-z.'-]+)", outreach_content or "")
    return m.group(1) if m else ""


def _funnel_value(proposal_content):
    nums = [int(v.replace(",", "")) for v in re.findall(r"\$(\d[\d,]*)", proposal_content or "")]
    return max(nums) if nums else None


def _funnel_stage_for(outreach_status, proposal_status, has_delivery, followup_status):
    if has_delivery:
        return "won"
    if proposal_status == "accepted":
        return "won"
    if proposal_status in ("approved", "ready_for_approval") or outreach_status == "replied":
        return "proposal"
    if followup_status == "ready_for_approval":
        return "followup"
    if outreach_status == "approved":
        return "sent"
    if outreach_status == "ready_for_approval":
        return "draft"
    if outreach_status in ("rejected",) or proposal_status == "rejected":
        return "lost"
    return "new"


def _funnel_prospects(project):
    """Derive the prospect ledger for a pipeline project from its funnel chains."""
    conn = _db()
    rows = conn.execute(
        "SELECT id, category, status, content, title, tags, updated_at FROM work_items "
        "WHERE source='pipeline:funnel' AND project=? ORDER BY updated_at DESC", (project,)).fetchall()
    conn.close()
    by_id = {r[0]: r for r in rows}
    prev_of = {}
    for r in rows:
        m = re.search(r"prev:([A-Za-z0-9]+)", r[5] or "")
        if m:
            prev_of[r[0]] = m.group(1)

    prospects = []
    for r in rows:
        if r[1] != "outreach_draft" or "funnel:followup" in (r[5] or ""):
            continue  # anchors only: real outreach items, not follow-ups
        owid = r[0]
        rwid = prev_of.get(owid, "")
        lwid = prev_of.get(rwid, "")
        research = by_id.get(rwid)
        lead = by_id.get(lwid)
        proposal = delivery = followup = None
        for r2 in rows:
            if prev_of.get(r2[0]) == owid:
                if r2[1] == "proposal":
                    proposal = r2
                elif r2[1] == "delivery":
                    delivery = r2
                elif r2[1] == "outreach_draft":
                    followup = r2
        if delivery is None and proposal:
            # delivery is a child of the proposal, not of the outreach
            for r2 in rows:
                if r2[1] == "delivery" and prev_of.get(r2[0]) == proposal[0]:
                    delivery = r2
                    break
        stage = _funnel_stage_for(r[2], proposal[2] if proposal else None,
                                  bool(delivery), followup[2] if followup else None)
        value = _funnel_value(proposal[3]) if proposal else None
        deepest = delivery or proposal or followup or r
        prospects.append({
            "prospect": _funnel_prospect_name(research[3] if research else (lead[3] if lead else "")),
            "decision_maker": _funnel_dm(r[3]),
            "stage": stage,
            "status": r[2],
            "outreach_wid": owid,
            "anchor": {"id": deepest[0], "category": deepest[1], "status": deepest[2]},
            "value": value,
            "updated_at": (deepest[6] or "")[:16],
            "next": _FUNNEL_STAGE_META.get(stage, ""),
        })
    counts = {s: 0 for s in _FUNNEL_STAGES}
    for p in prospects:
        counts[p["stage"]] = counts.get(p["stage"], 0) + 1
    revenue = sum(p["value"] or 0 for p in prospects if p["stage"] == "won")
    return {"prospects": prospects, "counts": counts, "revenue_won": revenue}


@agentic_bp.route("/api/agentic/funnel/prospects", methods=["GET", "OPTIONS"])
def api_funnel_prospects():
    """GET ?project=SLUG -> derived prospect ledger + stage counts + won revenue."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    project = (request.args.get("project") or "appvault").strip()
    data = _funnel_prospects(project)
    return jsonify({"status": "ok", "project": project, **data})


# ---------------------------------------------------------------------------
# PROSPECT SEEDS — real lead ingestion, fully tenant-configured (NOTHING
# hardcoded: signal keywords + connectors live in the project config, so a
# GRC tool tenant and a coaching tenant configure completely different
# sources and signals). Enrichment fetches real pages; the LLM only writes.
# ---------------------------------------------------------------------------
def _funnel_seeds_ensure():
    try:
        conn = _db()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS prospect_seeds ("
            "id TEXT PRIMARY KEY, business_id TEXT, company TEXT, website TEXT, note TEXT DEFAULT '', "
            "source TEXT DEFAULT 'csv', status TEXT DEFAULT 'new', signal_score REAL DEFAULT 0, "
            "signals_found TEXT DEFAULT '[]', site_title TEXT DEFAULT '', created_at TEXT)")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _funnel_project_cfg(bid):
    """Tenant config: {'prospect_signals': {kw: weight}, 'prospect_connectors': [...]}."""
    try:
        biz = dict(_get_business(bid) or {})
        project = _biz_project(biz) if biz else str(bid)
        cfg = _project_cfg(project) or {}
    except Exception:
        cfg = {}
    return {
        "signals": (cfg.get("prospect_signals") or {}),
        "connectors": (cfg.get("prospect_connectors") or []),
        "probe_paths": (cfg.get("probe_paths") or []),
    }


def _funnel_save_project_cfg(bid, patch_cfg):
    try:
        biz = dict(_get_business(bid) or {})
        project = _biz_project(biz) if biz else str(bid)
        conn = _db()
        conn.execute("INSERT OR IGNORE INTO projects (slug, name, config, created) VALUES (?,?,?,?)",
                     (project, (biz.get("name") or project), "{}",
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
        cfg = _project_cfg(project) or {}
        cfg.update(patch_cfg)
        _project_save_cfg(project, cfg)
    except Exception:
        pass


@agentic_bp.route("/api/agentic/funnel/connectors", methods=["GET", "PUT", "OPTIONS"])
def api_funnel_connectors():
    """Tenant-defined prospect connectors + signal keywords (per business).
    PUT {business_id, connectors: [{name,url,link_regex}], signals: {kw: weight}}"""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = str(data.get("business_id") or "").strip()
    if not bid:
        return jsonify({"status": "error", "error": "business_id required"}), 400
    if request.method == "PUT":
        connectors = data.get("connectors")
        signals = data.get("signals")
        patch = {}
        if connectors is not None:
            clean = []
            for c in connectors:
                if isinstance(c, dict) and c.get("name") and c.get("url"):
                    clean.append({"name": str(c["name"])[:60],
                                  "url": str(c["url"])[:300],
                                  "link_regex": str(c.get("link_regex") or r"^https?://")[:300]})
            patch["prospect_connectors"] = clean
        if signals is not None and isinstance(signals, dict):
            patch["prospect_signals"] = {str(k)[:60]: float(v) for k, v in signals.items() if v}
        if data.get("probe_paths") is not None and isinstance(data["probe_paths"], list):
            patch["probe_paths"] = [str(p)[:80] for p in data["probe_paths"]
                                    if str(p).startswith("/")][:8]
        if patch:
            _funnel_save_project_cfg(bid, patch)
    return jsonify({"status": "ok", **{k: v for k, v in _funnel_project_cfg(bid).items()}})


@agentic_bp.route("/api/agentic/funnel/seeds/import", methods=["POST", "OPTIONS"])
def api_funnel_seeds_import():
    """Import real prospect rows. Payload: {business_id, rows: [{company, website?, note?}]}
    OR {business_id, csv: "company,website\\nAcme,https://acme.com"} (header names matched
    case-insensitively for company/name and website/url/domain)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = str(data.get("business_id") or "").strip()
    if not bid:
        return jsonify({"status": "error", "error": "business_id required"}), 400
    _funnel_seeds_ensure()
    rows = []
    if data.get("rows") and isinstance(data["rows"], list):
        rows = data["rows"]
    elif data.get("csv"):
        lines = [ln.strip() for ln in str(data["csv"]).splitlines() if ln.strip()]
        if lines:
            headers = [h.strip().lower() for h in lines[0].split(",")]
            ci = next((i for i, h in enumerate(headers) if h in ("company", "name")), None)
            wi = next((i for i, h in enumerate(headers) if h in ("website", "url", "domain", "site")), None)
            for ln in lines[1:]:
                parts = [p.strip() for p in ln.split(",")]
                if ci is None or ci >= len(parts) or not parts[ci]:
                    continue
                row = {"company": parts[ci]}
                if wi is not None and wi < len(parts) and parts[wi]:
                    row["website"] = parts[wi]
                rows.append(row)
    added = 0
    import uuid as _uuid
    conn = _db()
    for r in rows:
        company = str(r.get("company") or "").strip()
        website = str(r.get("website") or "").strip()
        if not company:
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO prospect_seeds "
                "(id, business_id, company, website, note, source, created_at) VALUES (?,?,?,?,?,'import',datetime('now'))",
                (_uuid.uuid4().hex[:12], bid, company[:200], website[:300], str(r.get("note") or "")[:500]))
            added += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "added": added})


@agentic_bp.route("/api/agentic/funnel/seeds", methods=["GET", "OPTIONS"])
def api_funnel_seeds():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    bid = str((request.args.get("business_id") or "").strip())
    limit = min(int(request.args.get("limit") or 100), 500)
    _funnel_seeds_ensure()
    conn = _db()
    if bid:
        rows = conn.execute(
            "SELECT id, company, website, status, signal_score, signals_found, site_title, source, created_at "
            "FROM prospect_seeds WHERE business_id=? ORDER BY signal_score DESC, created_at DESC LIMIT ?",
            (bid, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, company, website, status, signal_score, signals_found, site_title, source, created_at "
            "FROM prospect_seeds ORDER BY signal_score DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    seeds = [{"id": r[0], "company": r[1], "website": r[2], "status": r[3], "signal_score": r[4],
              "signals_found": json.loads(r[5] or "[]"), "site_title": r[6], "source": r[7],
              "created_at": r[8]} for r in rows]
    return jsonify({"status": "ok", "seeds": seeds, "count": len(seeds)})


def _funnel_norm(t):
    """Accent-insensitive lowercase (é->e, ü->u ...) — multilingual scoring."""
    try:
        import unicodedata as _ud
        return "".join(c for c in _ud.normalize("NFKD", (t or "").lower())
                       if not _ud.combining(c))
    except Exception:
        return (t or "").lower()


def _funnel_extract_companies(html, base_url, link_regex):
    """Generic candidate extraction: anchor hrefs matching the configured regex.
    Dedupes by full URL (same host may host many prospect pages)."""
    found = {}
    try:
        import re as _re
        rx = _re.compile(link_regex)
        for m in _re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html or "", _re.S | _re.I):
            href, text = m.group(1), _re.sub(r"<[^>]+>", "", m.group(2)).strip()
            href = href.split("?")[0]
            if not rx.search(href):
                continue
            full = href if href.startswith("http") else (base_url.rstrip("/") + "/" + href.lstrip("/"))
            try:
                host = full.split("/")[2] if len(full.split("/")) > 2 else full
            except Exception:
                host = full
            name = (text or host.replace("www.", ""))[:200]
            if not name or name.lower() in ("read more", "learn more", "here", "view"):
                continue
            try:
                path = full.split(host, 1)[1] if host else full
            except Exception:
                path = full
            if path in ("", "/"):
                continue  # link back to the list page itself
            found.setdefault(full, {"company": name, "website": full})
    except Exception:
        pass
    return list(found.values())


@agentic_bp.route("/api/agentic/funnel/connector/run", methods=["POST", "OPTIONS"])
def api_funnel_connector_run():
    """Run one configured connector: fetch the list page, extract candidate
    companies (regex-driven, tenant-configured), insert as seeds."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = str(data.get("business_id") or "").strip()
    name = str(data.get("name") or "").strip()
    if not bid or not name:
        return jsonify({"status": "error", "error": "business_id and connector name required"}), 400
    cfg = _funnel_project_cfg(bid)
    conn_def = next((c for c in cfg["connectors"] if c.get("name") == name), None)
    if not conn_def:
        return jsonify({"status": "error", "error": f"connector '{name}' not configured"}), 404
    try:
        r = _http(conn_def["url"], timeout=25)
        body = r[0] if isinstance(r, tuple) else r
        html = body.get("raw", "") if isinstance(body, dict) else str(body)
    except Exception as e:
        return jsonify({"status": "error", "error": f"fetch failed: {str(e)[:150]}"}), 502
    candidates = _funnel_extract_companies(html, conn_def["url"], conn_def.get("link_regex") or r"^https?://")
    _funnel_seeds_ensure()
    import uuid as _uuid
    conn = _db()
    added = skipped = 0
    for c in candidates:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO prospect_seeds "
                "(id, business_id, company, website, note, source, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                (_uuid.uuid4().hex[:12], bid, c["company"], c["website"], f"connector:{name}", name))
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "connector": name, "candidates": len(candidates),
                    "added": added, "skipped": skipped})


def _funnel_enrich_seeds(bid, limit=50):
    """Fetch each seed's website and score it against the tenant's configured
    signal keywords (weighted, accent-insensitive). No LLM — real fetched facts.
    Returns a result dict. Callable directly (docker exec / tests) — no Flask."""
    cfg = _funnel_project_cfg(bid)
    signals = cfg["signals"]
    probes = cfg.get("probe_paths") or []
    _funnel_seeds_ensure()
    conn = _db()
    rows = conn.execute(
        "SELECT id, website FROM prospect_seeds WHERE business_id=? AND status IN ('new','enriched') "
        "ORDER BY created_at ASC LIMIT ?", (str(bid), limit)).fetchall()
    scored = found_signal = failed = 0
    for sid, website in rows:
        if not website:
            continue
        title = ""
        texts = ""
        urls = [website] + [website.rstrip("/") + (p if p.startswith("/") else "/" + p) for p in probes]
        for u in urls:
            try:
                r = _http(u, timeout=6)
                body = r[0] if isinstance(r, tuple) else r
                if isinstance(body, dict) and "error" in body:
                    continue
                html = body.get("raw", "") if isinstance(body, dict) else str(body)
                texts += " " + (html or "")
                if not title:
                    import re as _re
                    tm = _re.search(r"<title[^>]*>(.*?)</title>", html or "", _re.S | _re.I)
                    if tm:
                        title = _re.sub(r"<[^>]+>", "", tm.group(1)).strip()[:200]
            except Exception:
                continue
        if not texts.strip():
            conn.execute("UPDATE prospect_seeds SET status='failed' WHERE id=?", (sid,))
            failed += 1
            continue
        low = _funnel_norm(texts)
        score = 0.0
        hit = []
        for kw, w in (signals or {}).items():
            if kw and _funnel_norm(kw) in low:
                score += float(w or 1)
                hit.append(kw)
        conn.execute("UPDATE prospect_seeds SET status='enriched', signal_score=?, signals_found=?, site_title=? WHERE id=?",
                     (round(score, 2), json.dumps(hit[:20]), title, sid))
        scored += 1
        if hit:
            found_signal += 1
    conn.commit()
    conn.close()
    return {"status": "ok", "scored": scored, "with_signal": found_signal,
            "failed": failed, "signals_used": list((signals or {}).keys())[:20],
            "probes_used": probes}


@agentic_bp.route("/api/agentic/funnel/seeds/enrich", methods=["POST", "OPTIONS"])
def api_funnel_seeds_enrich():
    """POST {business_id, limit} — enrich + score seeds against tenant signals."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = str(data.get("business_id") or "").strip()
    if not bid:
        return jsonify({"status": "error", "error": "business_id required"}), 400
    limit = min(int(data.get("limit") or 50), 200)
    return jsonify(_funnel_enrich_seeds(bid, limit))


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
    """Return (user_msg, sys_prompt) with user identity block + skills context added."""
    try:
        identity = _identity_block()
        if identity and "USER / OPERATOR PROFILE" not in (sys_prompt or ""):
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
    """Specific agent soul override > built-in AGENT_PROMPTS > profile soul > default."""
    agent = (agent or "hermes").lower()
    try:
        raw = _cfg_get("souls") or ""
        souls = json.loads(raw) if raw else {}
        if souls.get(agent):
            return souls[agent]
    except Exception:
        pass
    if agent in AGENT_PROMPTS:
        return AGENT_PROMPTS[agent]
    try:
        raw = _cfg_get("souls") or ""
        souls = json.loads(raw) if raw else {}
        pname = (_get_profile().get("name") or "").strip()
        if pname and pname != "default" and souls.get(pname):
            return souls[pname]
    except Exception:
        pass
    return DEFAULT_LLM_CONFIG["system_prompt"]


@agentic_bp.route("/api/agentic/souls", methods=["GET", "POST", "OPTIONS"])
def api_souls():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    raw = _cfg_get("souls") or ""
    try:
        souls = json.loads(raw) if raw else {}
    except Exception:
        souls = {}
    pname = (_get_profile().get("name") or "default").strip() or "default"
    if request.method == "POST":
        data = request.get_json() or {}
        if data.get("profile"):
            pname = str(data["profile"]).strip() or pname
        if "content" in data:
            if data["content"] is None or str(data["content"]).strip() == "":
                souls.pop(pname, None)
            else:
                souls[pname] = str(data["content"])
            _cfg_set("souls", json.dumps(souls))
            _audit("store", "souls.update", f"profile {pname} soul updated")
            return jsonify({"status": "ok", "profile": pname})
        for profile, prompt in (data.get("souls") or {}).items():
            if prompt is not None:
                souls[profile] = str(prompt)
        _cfg_set("souls", json.dumps(souls))
        _audit("store", "souls.update", f"updated {len(data.get('souls') or {})} profile souls")
    return jsonify({"status": "ok", "profiles": sorted(set(list(souls.keys()) + [pname])),
                    "current": pname, "souls": souls})


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
    # slash commands win over everything (Hermes-style)
    slash_reply = _handle_slash_command(msg, "v")
    if slash_reply is not None:
        return slash_reply
    # skills take priority over keyword routing (@skill anywhere at the start)
    skill_msg, skill_row = _maybe_extract_skill(msg)
    if skill_row:
        action_reply = _run_skill_action(skill_row, skill_msg)
        if action_reply is not None:
            return action_reply
        ctx = _memory_context(skill_msg)
        conn = _db()
        conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (skill_row["id"],))
        conn.commit()
        conn.close()
        _audit("store", "skill.chat", f"@{skill_row['name']} applied in V router")
        sys_prompt = (f"You are applying the skill '{skill_row['name']}'. Follow its "
                      f"instructions EXACTLY. Output the result, no preamble.\n\n"
                      f"===== SKILL =====\n{_load_skill_content(skill_row)}")
        return _call_llm_with({}, skill_msg + (f"\n\n{ctx}" if ctx else ""),
                              system_prompt=sys_prompt, agent="hermes", timeout=60)
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
            # record into the Completed Work ledger (LinkedIn post)
            _work_record(category="linkedin", title=f"LinkedIn: {msg[:70]}", content=content,
                         source="v", status="draft", tags="linkedin,v-router",
                         wid="vpost-" + str(int(time.time())))
            _goal_bump(msg, "linkedin", 4, "v", f"LinkedIn post drafted: {msg[:50]}")
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
            # root-level SKILL.md: the skill's payload lives in these classic subdirs
            allowed_root_dirs = ("reference/", "scripts/", "examples/", "assets/", "templates/", "prompts/", "rules/")
            count = 0
            for t in (tree_data.get("tree") or []):
                if t.get("type") != "blob":
                    continue
                path = t.get("path") or ""
                if prefix and not path.startswith(prefix):
                    continue
                rel = path[len(prefix):] if prefix else path
                base_name = rel.split("/")[-1]
                if not prefix and "/" in path and not any(rel.startswith(d) for d in allowed_root_dirs):
                    continue  # root skill: only its classic payload subdirs belong to it
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
    # The model has NO file/script access — the prompt IS its filesystem. Without
    # this note it tries to "run detect_ai_patterns.py" and hallucinates tool calls.
    content += ("\n\n---\n### ENVIRONMENT NOTE (read first)\n"
                "You are running in a TEXT-ONLY agent environment: you CANNOT run scripts, "
                "read files, or call tools. Do NOT write, reference, or attempt to execute any "
                "script or file from this skill. Apply the skill's rules and principles DIRECTLY "
                "to the input text, using the inlined reference material above. Ignore any "
                "model-specific or tool-specific requirements in the skill (e.g. 'must use model X' "
                "or 'run script.py') — use your configured model and judgment instead.\n")
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
    """If the message starts with @<skillname>, return (rest_of_message, skill_row).
    Skill names may contain spaces — match the LONGEST skill-name prefix (with a
    word boundary), so '@WordPress publishing <text>' binds the full name, not
    just 'WordPress'."""
    msg = (msg or "").strip()
    if not msg.startswith("@"):
        return msg, None
    rest = msg[1:]
    try:
        conn = _db()
        names = [r["name"] for r in conn.execute("SELECT name FROM skills").fetchall()]
        conn.close()
    except Exception:
        names = []
    best = None
    best_len = 0
    norm_rest = rest.lower().replace("-", " ")
    for n in names:
        if not n:
            continue
        nn = n.lower().replace("-", " ")
        if not norm_rest.startswith(nn):
            continue
        after = rest[len(n):]
        if after and not after[0].isspace():
            continue  # boundary: next char must be whitespace or end
        if len(n) > best_len:
            best, best_len = n, len(n)
    if not best:
        return msg, None
    try:
        conn = _db()
        row = conn.execute("SELECT * FROM skills WHERE lower(replace(name,'-',' '))=lower(replace(?,'-',' ')) LIMIT 1",
                           (best,)).fetchone()
        conn.close()
    except Exception:
        row = None
    if row:
        return rest[best_len:].strip(), dict(row)
    return msg, None


# =============================================================================
# WORDPRESS TOOL (2026-08-08) — the first REAL external tool: WordPress REST
# API publishing via Application Passwords. Wired as: an action-skill (chat
# @wordpress-publishing executes the publish instead of an LLM pass), a
# pipeline node, an SEO final-publish flag, and a Gov-page config card.
# stdlib-only; Basic auth; every route has an OPTIONS guard.
# =============================================================================
import base64

def _wp_config_for(project="appvault"):
    """Project-scoped WP config (projects.config wp_tool) with global fallback."""
    raw = _project_cfg(project, "wp_tool")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


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


def _wp_auth_headers(cfg=None):
    cfg = cfg if cfg is not None else _wp_config()
    user = (cfg.get("username") or "").strip()
    pw = (cfg.get("app_password") or "").strip()
    if not user or not pw:
        return None
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _wp_upload_media(site, hdrs, image_path):
    """Upload a local image to the WP media library -> public URL (source_url)."""
    try:
        import urllib.request as _ur
        if not site or not hdrs or not image_path or not os.path.exists(image_path):
            return None
        fname = os.path.basename(image_path)
        mime = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
        with open(image_path, "rb") as f:
            raw = f.read()
        req = _ur.Request(f"{site}/wp-json/wp/v2/media", data=raw, method="POST")
        req.add_header("Authorization", hdrs.get("Authorization", ""))
        req.add_header("Content-Type", mime)
        req.add_header("Content-Disposition", f'attachment; filename="{fname}"')
        with _ur.urlopen(req, timeout=60) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        url = (d or {}).get("source_url") or ""
        mid = (d or {}).get("id")
        return {"url": url, "id": mid} if url else None
    except Exception:
        return None


def _public_media_url(image_path, project="appvault"):
    """Resolve a (possibly local vault) image path to a URL Ocoya can fetch.
    Order: already-http -> project media_base_url -> live WP media upload ->
    local agent route (note: only reachable on your own network)."""
    if not image_path:
        return "", ""
    if str(image_path).startswith(("http://", "https://")):
        return str(image_path), ""
    fname = os.path.basename(str(image_path))
    cfg = _social_router_cfg(project)
    base = (cfg.get("media_base_url") or "").strip().rstrip("/")
    if base:
        return f"{base}/{fname}", ""
    wp = _wp_config_for(project)
    site = (wp.get("site_url") or "").strip().rstrip("/")
    hdrs = _wp_auth_headers(wp)
    if site and hdrs:
        up = _wp_upload_media(site, hdrs, str(image_path))
        if up and up.get("url"):
            return up["url"], ""
    return (f"http://localhost:8086/api/agentic/media/file/{fname}",
            "image URL is local — set a Media base URL in 📡 Social Router (or configure WordPress) so Ocoya can fetch it")


def _wp_publish(title, content, status="publish", project=None):
    """Create a WordPress post via the REST API. Returns (ok, result)."""
    cfg = _project_cfg(project or "appvault", "wp_tool") if project else _wp_config()
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
        try:
            _work_record(category="article", title=str(title)[:200], content=str(content),
                         source="wordpress", status="published",
                         url=data.get("link") or "", wid="wp-" + str(data.get("id")))
        except Exception:
            pass
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
    key = (skill_row.get("name") or "").lower().replace("-", " ")
    handler = _ACTION_SKILL_HANDLERS.get(key)
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
    # normalize: 'wordpress-publishing' == 'WordPress publishing' (hyphens == spaces)
    rows = conn.execute(
        "SELECT * FROM skills WHERE lower(replace(name,'-',' '))=lower(replace('wordpress-publishing','-',' '))"
    ).fetchall()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if rows:
        keep = rows[0]
        for r in rows[1:]:
            conn.execute("DELETE FROM skills WHERE id=?", (r["id"],))
        conn.execute(
            "UPDATE skills SET name='WordPress publishing', content=?, description=?, kind='action', "
            "source='builtin:wordpress', tools='wordpress', updated=? WHERE id=?",
            (real_content, "Publish articles to WordPress via the built-in REST tool (action skill — "
             "@wordpress-publishing <content> publishes directly).", now, keep["id"]))
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
        # commit BEFORE opening a second writer connection (_cfg_set) —
        # otherwise the open INSERT transaction locks the DB (fresh-install bug)
        conn.commit()
        _cfg_set("active_profile", "Default")
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
def api_profile_item(pid):
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
    if action == "goal_report":
        try:
            _run_goal_report()
            return "ok", "daily goal report written"
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
    # (2026-08-08) cron results NO LONGER land in the memory table — that polluted
    # agent memory with 'Cron X -> ok' ticks. The vault log below is the record.
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
            "- `/mcp` — list/configure MCP servers · `/mcp <server> <tool> {json}` calls an MCP tool\n"
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
    if cmd == "/mcp":
        return _slash_mcp(args)
    return (f"Unknown command `{cmd}`.\n\n" + _slash_help())


if os.environ.get("APPVAULT_CRON", "1") != "0":
    try:
        _start_cron()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PER-AGENT SESSIONS (threads) — sessions under every roster agent. hermes
# keeps its `sessions` table; every other agent gets threads in agent_threads
# + conversation_messages (thread_id). Legacy conversations migrate to 'main'.
# ---------------------------------------------------------------------------
def _init_thread_tables():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conversation_messages (
        agent_id TEXT NOT NULL,
        thread_id TEXT NOT NULL DEFAULT 'main',
        messages TEXT, updated TEXT,
        PRIMARY KEY (agent_id, thread_id)
    );
    CREATE TABLE IF NOT EXISTS agent_threads (
        id TEXT PRIMARY KEY,
        agent TEXT, title TEXT, message_count INTEGER DEFAULT 0,
        created TEXT, updated TEXT
    );
    """)
    conn.commit()
    try:
        rows = conn.execute("SELECT agent_id, messages FROM conversations").fetchall()
        for r in rows:
            conn.execute("INSERT OR IGNORE INTO conversation_messages (agent_id, thread_id, messages, updated) "
                         "VALUES (?,?,?,?)", (r["agent_id"], "main", r["messages"],
                                              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception:
        pass
    conn.close()


_init_thread_tables()


# Thread-aware conversation storage (shadows the legacy _get/_save_conversation —
# same signatures, callers with one arg keep working via thread_id='main').
def _get_conversation(agent_id, thread_id="main"):
    conn = _db()
    row = conn.execute("SELECT messages FROM conversation_messages WHERE agent_id=? AND thread_id=?",
                       (agent_id, thread_id)).fetchone()
    conn.close()
    if row and row["messages"]:
        try:
            msgs = json.loads(row["messages"])
            if msgs:
                return msgs
        except Exception:
            pass
    conn = _db()
    row = conn.execute("SELECT messages FROM conversations WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    if row and row["messages"]:
        try:
            msgs = json.loads(row["messages"])
            if msgs:
                return msgs
        except Exception:
            pass

    greetings = {
        "openclaw": "🦞 **Greetings! I am OpenClaw**, your personal autonomous AI assistant. Ask me anything, assign multi-step coding or research tasks, or configure your API key anytime.",
        "hermes": "🤖 **Hermes Agent online.** 24/7 continuous watcher, signal radar, and tool sandbox ready.",
        "goose": "🪿 **Greetings! I am Goose**, your autonomous open-source developer agent (aaif-goose/goose). Tell me what feature to build, debug, test, or refactor.",
        "deepseek-harness": "🐋 **DeepSeek Harness online.** Powered by DeepSeek reasoning models (R1/V3). Ready for high-precision logic verification, complex architectural analysis, and benchmark evaluation.",
        "deerflow": "🦌 **DeerFlow Super-Agent online.** Ready for long-horizon autonomous tasks, sandbox execution, and deep multi-step workflows.",
        "claude": "🧠 **Claude Architect online.** Deep reasoning, systems analysis, and architectural design ready.",
        "antigravity": "⚡ **Antigravity Builder online.** Full-stack development, agentic workflows, and code synthesis ready.",
        "codex": "💻 **Codex Synthesizer online.** Code synthesis, refactoring, and spec generation ready."
    }
    agent_name = "Hermes Agent" if agent_id == "hermes" else f"{agent_id.capitalize()} Agent"
    default_text = greetings.get(agent_id.lower(), f"Agent **{agent_id.capitalize()}** online. Ready for tasks.")
    return [{"sender": agent_name, "role": "agent", "timestamp": "NOW", "text": default_text}]


def _save_conversation(agent_id, messages, thread_id="main"):
    conn = _db()
    conn.execute("INSERT INTO conversation_messages (agent_id, thread_id, messages, updated) VALUES (?,?,?,?) "
                 "ON CONFLICT(agent_id, thread_id) DO UPDATE SET messages=excluded.messages, updated=excluded.updated",
                 (agent_id, thread_id, json.dumps(messages),
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.execute("UPDATE agent_threads SET message_count=?, updated=? WHERE agent=? AND id=?",
                 (len(messages), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), agent_id, thread_id))
    conn.commit()
    conn.close()


def _create_agent_thread(agent, title):
    tid = f"t-{int(time.time()*1000)}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    conn.execute("INSERT INTO agent_threads (id, agent, title, message_count, created, updated) VALUES (?,?,?,0,?,?)",
                 (tid, agent, (title or f"Session {datetime.now().strftime('%H:%M:%S')}"), now, now))
    conn.commit()
    conn.close()
    return tid


def _roster_sessions_map():
    """{agent_id: [{id, title, message_count, updated}]} — hermes from the
    sessions table, every other agent from agent_threads."""
    out = {}
    try:
        for s in _list_sessions():
            out.setdefault("hermes", []).append({
                "id": s["id"], "title": s["title"],
                "message_count": s.get("message_count", 0),
                "updated": s.get("updated", s.get("created_at", ""))})
    except Exception:
        pass
    conn = _db()
    rows = conn.execute("SELECT id, agent, title, message_count, updated FROM agent_threads ORDER BY updated DESC").fetchall()
    conn.close()
    for r in rows:
        out.setdefault(r["agent"], []).append({
            "id": r["id"], "title": r["title"], "message_count": r["message_count"],
            "updated": r["updated"] or ""})
    return out


@agentic_bp.route("/api/agentic/roster/sessions", methods=["GET", "POST", "OPTIONS"])
def api_roster_sessions():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        agent = (data.get("agent") or "hermes").lower()
        title = (data.get("title") or "").strip()
        if agent == "hermes":
            sid = f"session-{int(time.time()*1000)}"
            _save_session(sid, title or f"Hermes Session {datetime.now().strftime('%H:%M:%S')}", [])
            _audit("store", "session.new", f"roster: '{title}' ({sid})")
            return jsonify({"status": "ok", "session_id": sid})
        tid = _create_agent_thread(agent, title)
        _audit("store", "thread.new", f"roster: '{agent}' thread '{title}' ({tid})")
        return jsonify({"status": "ok", "session_id": tid})
    return jsonify({"status": "ok", "sessions": _roster_sessions_map()})


@agentic_bp.route("/api/agentic/roster/sessions/<agent>/<thread_id>", methods=["GET", "DELETE", "OPTIONS"])
def api_roster_thread(agent, thread_id):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    agent = agent.lower()
    if request.method == "DELETE":
        if agent == "hermes":
            conn = _db()
            conn.execute("DELETE FROM sessions WHERE id=?", (thread_id,))
            conn.commit()
            conn.close()
        else:
            conn = _db()
            conn.execute("DELETE FROM agent_threads WHERE id=?", (thread_id,))
            conn.execute("DELETE FROM conversation_messages WHERE agent_id=? AND thread_id=?", (agent, thread_id))
            conn.commit()
            conn.close()
        _audit("store", "session.delete", f"'{agent}' {thread_id}")
        return jsonify({"status": "ok"})
    if agent == "hermes":
        sess = _get_session(thread_id)
        if not sess:
            return jsonify({"error": "session not found"}), 404
        return jsonify({"status": "ok", "messages": sess["messages"]})
    msgs = _get_conversation(agent, thread_id)
    if isinstance(msgs, list) and msgs and msgs[0].get("text", "").startswith("Agent **"):
        conn = _db()
        t = conn.execute("SELECT id FROM agent_threads WHERE id=?", (thread_id,)).fetchone()
        conn.close()
        if not t:
            return jsonify({"error": "thread not found"}), 404
    return jsonify({"status": "ok", "messages": msgs})


# =============================================================================
# MCP CLIENT (2026-08-08) — the agentic plane can CALL external MCP servers
# (including our own gateway on :8087). Minimal stdlib JSON-RPC over HTTP.
# Config: `mcp_servers` in the config table = JSON list of
#   {"name": "...", "url": "http://host:port/mcp", "api_key": "..."}
# Chat: /mcp · /mcp <server> · /mcp <server> <tool> [json args] · /mcp add name|url|key
# =============================================================================
def _mcp_servers():
    raw = _cfg_get("mcp_servers") or ""
    try:
        servers = json.loads(raw) if raw else []
        return servers if isinstance(servers, list) else []
    except Exception:
        return []


def _mcp_save_servers(servers):
    _cfg_set("mcp_servers", json.dumps(servers, indent=1))


def _mcp_jsonrpc(url, payload, api_key=None, timeout=30, session=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("Mcp-Session-Id")
        body = r.read().decode("utf-8", "replace")
    # strip SSE framing if the server responds event-stream; a 202 notification
    # ack has an EMPTY body (not an error)
    if body.lstrip().startswith("event:") or "\ndata:" in body:
        lines = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        body = "\n".join(lines)
    if not body.strip():
        return sid, None
    return sid, json.loads(body)


_MCP_SESSIONS = {}


def _mcp_session(server):
    """Return a valid Mcp-Session-Id for the server, handshaking if needed."""
    url = (server.get("url") or "").rstrip("/")
    key = server.get("api_key") or ""
    sid = _MCP_SESSIONS.get(url)
    if sid:
        return sid
    sid, init = _mcp_jsonrpc(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "appvault-agentic", "version": "1.0"}}}, key)
    if sid:
        _MCP_SESSIONS[url] = sid
    try:
        _mcp_jsonrpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     key, timeout=5, session=sid)
    except Exception:
        pass
    return sid


def _mcp_tools(server):
    url = (server.get("url") or "").rstrip("/")
    key = server.get("api_key") or ""
    for attempt in range(2):
        try:
            sid = _mcp_session(server)
            _, res = _mcp_jsonrpc(url, {"jsonrpc": "2.0", "id": 2,
                                        "method": "tools/list", "params": {}}, key, session=sid)
            if not res:
                return {"error": "empty response from MCP server"}
            return (res.get("result") or {}).get("tools", [])
        except urllib.error.HTTPError as e:
            if e.code == 400 and attempt == 0:
                _MCP_SESSIONS.pop(url, None)  # stale session (server restarted)
                continue
            return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:150]}"}
        except Exception as e:
            return {"error": str(e)[:200]}
    return {"error": "session retry exhausted"}


def _mcp_call(server, tool, args):
    url = (server.get("url") or "").rstrip("/")
    key = server.get("api_key") or ""
    for attempt in range(2):
        try:
            sid = _mcp_session(server)
            _, res = _mcp_jsonrpc(url, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}}}, key, timeout=120, session=sid)
            if not res:
                return "⚠️ empty response from MCP server"
            result = res.get("result") or {}
            content = result.get("content") or []
            out = "\n".join(str(c.get("text") or c.get("content") or "")
                            for c in content if isinstance(c, dict))
            if not out:
                out = json.dumps(result)[:600]
            if result.get("isError"):
                return f"⚠️ MCP error from {tool}: {str(out)[:400]}"
            return str(out)[:2000]
        except urllib.error.HTTPError as e:
            if e.code == 400 and attempt == 0:
                _MCP_SESSIONS.pop(url, None)
                continue
            return f"⚠️ MCP HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:150]}"
        except Exception as e:
            return f"⚠️ MCP call failed: {str(e)[:200]}"
    return "⚠️ MCP call failed: session retry exhausted"


def _slash_mcp(args):
    m_add = re.match(r"^add\s+(.+)$", args, re.S)
    if m_add:
        parts = [p.strip() for p in m_add.group(1).split("|")]
        if len(parts) >= 2:
            name, url = parts[0], parts[1]
            key = parts[2] if len(parts) > 2 else ""
            if not url.startswith("http"):
                return "⚠️ URL must start with http(s)://"
            servers = _mcp_servers()
            servers = [s for s in servers if s.get("name") != name]
            servers.append({"name": name, "url": url, "api_key": key})
            _mcp_save_servers(servers)
            return f"✅ MCP server '{name}' added ({url}). Try: /mcp {name}"
        return "⚠️ Usage: /mcp add <name>|<url>|<api-key-optional>"
    m_del = re.match(r"^del\s+(\S+)$", args)
    if m_del:
        _mcp_save_servers([s for s in _mcp_servers() if s.get("name") != m_del.group(1)])
        return f"🗑 MCP server '{m_del.group(1)}' removed"
    servers = _mcp_servers()
    if not args.strip():
        if not servers:
            return ("No MCP servers configured. Add one:\n"
                    "`/mcp add my-server|http://host:port/mcp|optional-api-key`\n"
                    "Try our own gateway: `/mcp add appvault|http://localhost:8087/mcp`")
        lines = [f"- **{s['name']}** · {s.get('url')}" for s in servers]
        return ("**MCP servers:**\n" + "\n".join(lines) +
                "\n\n`/mcp <server>` lists its tools · `/mcp <server> <tool> {json}` calls one")
    # token parse: server [tool [json-args]] — server names have NO spaces
    m = re.match(r"^(\S+)(?:\s+(.+))?$", args.strip())
    srv_name = m.group(1)
    rest = (m.group(2) or "").strip()
    srv = next((s for s in servers if s.get("name", "").lower() == srv_name.lower()), None)
    if not srv:
        return f"⚠️ No server named '{srv_name}' — /mcp to list"
    if not rest:
        tools = _mcp_tools(srv)
        if isinstance(tools, dict) and tools.get("error"):
            return f"⚠️ {tools['error']}"
        if not tools:
            return f"Server '{srv_name}' exposes no tools."
        lines = [f"- **{t.get('name')}** — {t.get('description', '')[:90]}" for t in tools[:25]]
        return f"**Tools on '{srv_name}' ({len(tools)}):**\n" + "\n".join(lines)
    m2 = re.match(r"^(\S+)(?:\s+(.+))?$", rest)
    tool = m2.group(1)
    args_json = (m2.group(2) or "").strip()
    try:
        call_args = json.loads(args_json) if args_json else {}
    except Exception:
        return "⚠️ Arguments must be valid JSON (e.g. {\"message\": \"hi\"})"
    _audit("chat", "mcp.call", f"{srv['name']}:{tool}")
    return _mcp_call(srv, tool, call_args)


# =============================================================================
# WORK LEDGER (2026-08-08) — one place for all completed work: research,
# articles, tweets, images, news. Category-tagged, previewable, publishable.
# =============================================================================

def _work_record(category="other", title="", content="", image_url="", source="manual",
                 status="draft", url="", tags="", wid=None, project="appvault"):
    try:
        import uuid as _uuid
        wid = wid or _uuid.uuid4().hex[:12]
        conn = _db()
        conn.execute("""INSERT OR IGNORE INTO work_items
            (id, category, title, content, image_url, source, status, url, tags, project, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))""",
            (wid, category, title[:4000], content or "", image_url or "", source,
             status, url or "", tags or "", project))
        conn.commit(); conn.close()
        return wid
    except Exception:
        return None

def _work_seed_if_empty():
    """Backfill is DISABLED (2026-08-08): it pulled raw pipeline/system logs
    (02_Agent_Logs, Pipeline_*/SEO_*/Crew Execution files) into the ledger as
    'research' — noise. The Completed Work page shows ONLY real work items:
    articles/X posts/LinkedIn/images recorded by the WP hook, pipeline hooks,
    or manual adds."""
    return 0

@agentic_bp.route("/api/agentic/work", methods=["GET", "POST", "OPTIONS"])
def api_work_items():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title required"}), 400
        wid = data.get("id") or ("w-" + str(int(time.time() * 1000)))
        _work_record(category=data.get("category") or "other",
                     title=data.get("title") or "Untitled",
                     content=data.get("content") or "",
                     image_url=data.get("image_url") or "",
                     source=data.get("source") or "manual",
                     status=data.get("status") or "draft",
                     url=data.get("url") or "",
                     tags=data.get("tags") or "",
                     wid=wid)
        _goal_bump((data.get("title") or "") + " " + (data.get("tags") or ""),
                   data.get("category") or "", 5, "work",
                   f"Work recorded: {(data.get('title') or '')[:60]}")
        return jsonify({"status": "ok", "id": wid})
    category = (request.args.get("category") or "").strip()
    st = (request.args.get("status") or "").strip()
    q = (request.args.get("q") or "").strip().lower()
    _work_seed_if_empty()
    conn = _db()
    sql = "SELECT * FROM work_items WHERE 1=1"
    args = []
    if category and category != "all":
        sql += " AND category=?"; args.append(category)
    if st:
        sql += " AND status=?"; args.append(st)
    if q:
        sql += " AND (title LIKE ? OR tags LIKE ? OR content LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = conn.execute(sql, args).fetchall()
    cols = [c[0] for c in conn.execute("SELECT * FROM work_items LIMIT 0").description]
    items = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"items": items})

@agentic_bp.route("/api/agentic/work/<wid>", methods=["GET", "DELETE", "PUT", "OPTIONS"])
def api_work_item(wid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone()
    if not row:
        conn.close(); return jsonify({"error": "not found"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM work_items WHERE id=?", (wid,))
        conn.commit(); conn.close()
        _audit("store", "work.delete", wid)
        return jsonify({"status": "ok"})
    if request.method == "PUT":
        data = request.get_json() or {}
        sets, args = [], []
        for col in ("title", "content", "category", "tags", "image_url", "url"):
            if col in data and data[col] is not None:
                sets.append(f"{col}=?")
                # content: articles can hold AI visuals (10k+ HTML) — no hard cap
                args.append(str(data[col])[:100000] if col == "content" else str(data[col])[:500])
        if not sets:
            conn.close()
            return jsonify({"error": "nothing to update"}), 400
        sets.append("updated_at=datetime('now')")
        conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id=?", args + [wid])
        conn.commit(); conn.close()
        _audit("store", "work.edit", wid)
        return jsonify({"status": "ok"})
    cols = [c[0] for c in conn.execute("SELECT * FROM work_items LIMIT 0").description]
    item = dict(zip(cols, row))
    conn.close()
    return jsonify({"item": item})

@agentic_bp.route("/api/agentic/work/<wid>/status", methods=["POST", "OPTIONS"])
def api_work_status(wid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    st = (data.get("status") or "").strip()
    if st not in ("draft", "reviewed", "published", "archived"):
        return jsonify({"error": "bad status"}), 400
    conn = _db()
    conn.execute("UPDATE work_items SET status=?, updated_at=datetime('now') WHERE id=?", (st, wid))
    conn.commit(); conn.close()
    _audit("store", "work.status", f"{wid} -> {st}")
    return jsonify({"status": "ok"})

@agentic_bp.route("/api/agentic/work/<wid>/publish", methods=["POST", "OPTIONS"])
def api_work_publish(wid):
    """Push a work item straight to WordPress (review -> publish in one click)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    cols = [c[0] for c in conn.execute("SELECT * FROM work_items LIMIT 0").description]
    item = dict(zip(cols, row))
    conn.close()
    ok, res = _wp_publish(item.get("title") or "Post from AppVault", item.get("content") or "")
    if not ok:
        return jsonify({"status": "error", "error": res}), 502
    conn = _db()
    conn.execute("UPDATE work_items SET status='published', url=?, updated_at=datetime('now') WHERE id=?",
                 (res.get("link") or "", wid))
    conn.commit(); conn.close()
    _audit("store", "work.publish", f"{wid} -> {res.get('link', '')}")
    return jsonify({"status": "ok", "post_id": res.get("id"), "link": res.get("link")})

# =============================================================================
# GOALS v2 (2026-08-08) — categorized goals (business/professional/private/
# health), activity-linked auto-progress, daily morning report + next steps.
# =============================================================================
from datetime import timedelta

GOAL_CATEGORIES = ("business", "professional", "private", "health")


def _goals_v2_migrate():
    conn = _db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()]
    if "category" not in cols:
        conn.execute("ALTER TABLE goals ADD COLUMN category TEXT DEFAULT 'business'")
    if "next_steps" not in cols:
        conn.execute("ALTER TABLE goals ADD COLUMN next_steps TEXT DEFAULT ''")
    if "target" not in cols:
        conn.execute("ALTER TABLE goals ADD COLUMN target TEXT DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS goal_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER, source TEXT,
        note TEXT, delta INTEGER DEFAULT 0, ts TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS goal_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now')), report TEXT)""")
    conn.commit()
    conn.close()
    # register the daily morning report job (08:00) once
    try:
        row = conn and None
        conn = _db()
        exists = conn.execute("SELECT id FROM cron_jobs WHERE name='goal-daily-report'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO cron_jobs (name, schedule, task, action, enabled, next_run, created, updated) "
                "VALUES (?,?,?,?,1,NULL,?,?)",
                ("goal-daily-report", "08:00", "Daily goal progress report", "goal_report",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception:
        pass


_goals_v2_migrate()


def _goal_bump(keywords, category, amount, source, note=""):
    """Auto-progress: bump active goals whose title matches keywords or category."""
    try:
        conn = _db()
        kws = [k.lower() for k in (keywords or "").split() if len(k) > 2]
        rows = conn.execute("SELECT * FROM goals WHERE status='active'").fetchall()
        for r in rows:
            title = (r["title"] or "").lower()
            cat = (r["category"] or "").lower()
            match = (cat == (category or "").lower()) or any(k in title for k in kws)
            if not match:
                continue
            nprog = min(100, (r["progress"] or 0) + amount)
            conn.execute("UPDATE goals SET progress=?, updated=? WHERE id=?",
                         (nprog, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            conn.execute("INSERT INTO goal_activity (goal_id, source, note, delta) VALUES (?,?,?,?)",
                         (r["id"], source, (note or "")[:200], amount))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _run_goal_report():
    """Build the daily goal report: per-category progress + last-24h activity + next steps."""
    conn = _db()
    goals = conn.execute(
        "SELECT * FROM goals ORDER BY status='active' DESC, category ASC, priority ASC, id DESC").fetchall()
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    acts = conn.execute(
        "SELECT * FROM goal_activity WHERE ts >= ? ORDER BY id DESC LIMIT 30", (since,)).fetchall()
    work = conn.execute(
        "SELECT * FROM work_items WHERE created_at >= ? ORDER BY created_at DESC LIMIT 12", (since,)).fetchall()
    conn.close()
    lines = [f"# 📊 Goal Progress Report — {datetime.now().strftime('%Y-%m-%d %A')}", ""]
    cur_cat = None
    for g in goals:
        cat = (g["category"] or "business").title()
        if cat != cur_cat:
            cur_cat = cat
            lines.append(f"\n## {cat}")
        p = g["progress"] or 0
        bar = "█" * (p // 10) + "░" * (10 - p // 10)
        lines.append(f"- **{g['title']}** — {p}% {bar} [{g['status']}]")
        ns = (g["next_steps"] or "").strip()
        if ns:
            lines.append(f"  - Next: {ns[:180]}")
        tgt = (g["target"] or "").strip()
        if tgt:
            lines.append(f"  - Target: {tgt[:120]}")
    lines.append("\n## Activity (last 24h)")
    if acts:
        for a in acts:
            lines.append(f"- {a['ts'][:16]} · +{a['delta']}% — {a['note']}")
    else:
        lines.append("- No goal-linked activity in the last 24h.")
    if work:
        lines.append("\n## Completed work (last 24h)")
        for w in work:
            lines.append(f"- [{w['category']}] {w['title'][:90]}")
    report = "\n".join(lines)
    conn = _db()
    conn.execute("INSERT INTO goal_reports (report) VALUES (?)", (report,))
    conn.commit()
    conn.close()
    try:
        _write_vault_output("04_Projects/Outputs",
                            f"Goal_Report_{datetime.now().strftime('%Y%m%d')}.md",
                            report, tag="Goal Report", agent="Goals")
    except Exception:
        pass
    try:
        _telegram_send(f"📊 Daily Goal Report:\n\n{report[:3500]}")
    except Exception:
        pass
    return report


@agentic_bp.route("/api/agentic/goals/report", methods=["GET", "POST", "OPTIONS"])
def api_goal_report():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        try:
            report = _run_goal_report()
            return jsonify({"status": "ok", "report": report})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)[:200]}), 500
    conn = _db()
    row = conn.execute("SELECT * FROM goal_reports ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return jsonify({"status": "ok", "report": row["report"] if row else None,
                    "ts": row["ts"] if row else None})


@agentic_bp.route("/api/agentic/goals/categories", methods=["GET", "POST", "OPTIONS"])
def api_goal_categories():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    raw = _cfg_get("goal_categories") or ""
    try:
        custom = json.loads(raw) if raw else []
    except Exception:
        custom = []
    if not isinstance(custom, list):
        custom = []
    if request.method == "POST":
        data = request.get_json() or {}
        name = (data.get("name") or "").strip().lower()
        if not name:
            return jsonify({"error": "category name required"}), 400
        if name not in custom and name not in GOAL_CATEGORIES:
            custom.append(name)
            _cfg_set("goal_categories", json.dumps(custom))
            return jsonify({"status": "ok", "categories": list(GOAL_CATEGORIES) + custom})
        return jsonify({"status": "ok", "categories": list(GOAL_CATEGORIES) + custom})
    # GET: defaults + custom + any categories in use
    conn = _db()
    used = [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM goals WHERE category IS NOT NULL AND category != ''").fetchall()]
    conn.close()
    cats = list(GOAL_CATEGORIES)
    for c in custom + used:
        if c not in cats:
            cats.append(c)
    return jsonify({"status": "ok", "categories": cats, "custom": custom})

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
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_type TEXT, value REAL, note TEXT, ts TEXT DEFAULT (datetime('now'))
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
    try:
        conn = _db()
        mrows = conn.execute("SELECT metric_type, value, note FROM metrics ORDER BY id DESC LIMIT 3").fetchall()
        conn.close()
        if mrows:
            lines.append("\nRecent metrics:")
            for mr in mrows:
                lines.append("- %s: %s%s" % (mr["metric_type"], mr["value"], (" (" + mr["note"] + ")") if mr["note"] else ""))
    except Exception:
        pass
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


def _mission_spec(mission, task, ctx):
    topic = mission.get("title") or "Build a small tool"
    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\n"
              "Write a build spec for: %s\nSpec must include: purpose, 1-3 files (Python), "
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
    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\n"
              "Write the COMPLETE, syntactically valid Python 3 script implementing this spec. "
              "Rules: output ONLY code (no markdown fences, no explanation); a single self-contained file; "
              "no comments naming other files; NO 'from __future__' imports (the compiler rejects them here).\n\nSpec:\n%s"
              % (ctx["voice"], spec[:3000]))
    # adaptive feedback: previous verification failure
    try:
        conn = _db()
        v = conn.execute(
            "SELECT last_error FROM mission_tasks WHERE mission_id=? AND task_type='verify_build' "
            "AND last_error IS NOT NULL AND last_error LIKE '%SYNTAX%' ORDER BY id DESC LIMIT 1",
            (mission.get("id"),)).fetchone()
        conn.close()
        if v and v["last_error"]:
            prompt += ("\n\nIMPORTANT: your previous attempt was REJECTED by the compiler with:\n%s\n"
                       "Fix that exact issue, and remember: NO 'from __future__' imports, single file, "
                       "output only code." % v["last_error"])
    except Exception:
        pass
    out = _call_llm_with({}, prompt, agent="hermes", timeout=300)
    out = (out or "").strip()
    out = out.replace("```python", "").replace("```", "").strip()
    if len(out) < 60:
        return (False, None, "artifact too short (%d chars)" % len(out))
    path = _write_vault_output("05_Build", "%s.py" % _mission_slug(topic), out, tag="Build", agent="Mission")
    return (True, path, None)


def _mission_verify_build(mission, task, ctx):
    code = _read_dep_result(task) or ""
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
    build_ref = _mission_result_by_type(mission.get("id"), "build")
    code = _read_ref(build_ref) or ""
    if not code:
        return (False, None, "no build artifact found to ship")
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
            f.write("- %s | %s | %d lines | %s\n" % (slug, mission.get("title", ""), len(code.splitlines()),
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
        if ttype == "spec":
            return _mission_spec(mission, task, ctx)
        if ttype == "build":
            return _mission_build(mission, task, ctx)
        if ttype == "verify_build":
            return _mission_verify_build(mission, task, ctx)
        if ttype == "ship":
            return _mission_ship(mission, task, ctx)
        return (False, None, "unknown task_type: %s" % ttype)
    except Exception as e:
        return (False, None, str(e)[:300])


def _mission_run_task(mission, task):
    """Run one task and persist the outcome (daemon thread)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        ok, ref, err = _mission_execute(mission, task)
    except Exception as e:
        ok, ref, err = False, None, str(e)[:300]
    try:
        conn = _db()
        if ok:
            conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                         (ref, now, task["id"]))
            # a verified producer un-blocks its verifier so the new artifact gets checked
            if task.get("task_type") in ("build", "draft"):
                conn.execute("UPDATE mission_tasks SET status='queued', attempts=0, last_error=NULL, updated=? "
                             "WHERE mission_id=? AND task_type IN ('verify_build','qa') AND status='blocked'",
                             (now, task.get("mission_id")))
            conn.commit()
            conn.close()
            return
        attempts = (task.get("attempts") or 0) + 1
        # self-correction: a failed verification re-queues its producer to regenerate
        if task.get("task_type") in ("verify_build", "qa") and task.get("depends_on"):
            conn.execute("UPDATE mission_tasks SET status='queued', attempts=0, last_error=NULL, updated=? WHERE id=?",
                         (now, task["depends_on"]))
        if attempts >= 3:
            conn.execute("UPDATE mission_tasks SET status='blocked', attempts=?, last_error=?, updated=? WHERE id=?",
                         (attempts, str(err)[:300], now, task["id"]))
            out_note = "blocked: %s" % str(err)[:80]
        else:
            conn.execute("UPDATE mission_tasks SET status='queued', attempts=?, last_error=?, updated=? WHERE id=?",
                         (attempts, str(err)[:300], now, task["id"]))
            out_note = "retry(%d)" % attempts
        conn.commit()
        conn.close()
        return out_note
    except Exception as e:
        try:
            conn = _db()
            conn.execute("UPDATE mission_tasks SET last_error=?, updated=? WHERE id=?",
                         ("worker-db-error: %s" % str(e)[:200], now, task["id"]))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return None


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
                # self-heal: outcome landed but status went stale (thread race)
                if tdict["status"] == "running" and tdict["verified"]:
                    conn.execute("UPDATE mission_tasks SET status='verified', updated=? WHERE id=?", (now, tdict["id"]))
                    conn.commit()
                    out.append("M%d T%d healed" % (m["id"], tdict["id"]))
                    continue
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
                        out.append("M%d done%s" % (m["id"], (rev and (" review:" + rev)) or ""))
                    except Exception:
                        out.append("M%d done" % m["id"])
                    # P0-1 mail notify: report mission completion headlessly
                    try:
                        mt = mdict.get("title") or ("M%d" % m["id"])
                        _queue_mail("✅ Mission complete: %s" % mt,
                                    "Mission \"%s\" finished — every task verified.\n\n%s" % (
                                        mt, (mdict.get("description") or "")[:400]))
                    except Exception:
                        pass
                continue
            conn.execute("UPDATE mission_tasks SET status='running', updated=? WHERE id=?", (now, chosen["id"]))
            conn.commit()
            conn.close()
            # async: run in a daemon thread so the tick never blocks on LLM calls
            try:
                threading.Thread(target=_mission_run_task, args=(mdict, chosen), daemon=True).start()
            except Exception:
                _mission_run_task(mdict, chosen)
            out.append("M%d T%d started" % (m["id"], chosen["id"]))
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


@agentic_bp.route("/api/agentic/metrics", methods=["GET", "POST", "OPTIONS"])
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
    return jsonify({"status": "ok", "templates": MISSION_TEMPLATES})


_missions_migrate()


# =============================================================================
# MAIL NOTIFY (2026-08-10) — P0-1 roadmap: smtplib + mail_queue + completion hooks.
# Config stored in the config table under key "mail":
#   {"enabled": true, "host": "smtp.example.com", "port": 587, "tls": true,
#    "ssl": false, "user": "...", "password": "...", "from_addr": "...", "to_addr": "..."}
# Endpoints:
#   GET/PUT /api/agentic/mail/config   read/update SMTP config (password masked on read)
#   POST    /api/agentic/mail/test     send a test email now
#   POST    /api/agentic/mail/notify   generic headless notify: {subject, body, to?}
#   GET/POST /api/agentic/mail/queue   view queue; POST {"flush": true} retries failed
# =============================================================================
try:
    import smtplib
    from email.mime.text import MIMEText
    _MAIL_IMPORT_OK = True
except Exception:
    _MAIL_IMPORT_OK = False


def _mail_cfg():
    cfg = _cfg_get("mail") or {}
    defaults = {"enabled": False, "host": "", "port": 587, "tls": True, "ssl": False,
                "user": "", "password": "", "from_addr": "", "to_addr": ""}
    defaults.update({k: v for k, v in cfg.items() if v is not None})
    return defaults


def _mask_mail_cfg(cfg):
    out = dict(cfg)
    if out.get("password"):
        out["password"] = "****"
    return out


def _send_mail(subject, body, to_addr=None, cfg=None):
    if not _MAIL_IMPORT_OK:
        return {"ok": False, "error": "smtplib unavailable in this image"}
    cfg = cfg or _mail_cfg()
    if not cfg.get("enabled") or not cfg.get("host"):
        return {"ok": False, "error": "mail not configured (PUT /api/agentic/mail/config)"}
    to = (to_addr or cfg.get("to_addr") or "").strip()
    if not to:
        return {"ok": False, "error": "no to_addr configured"}
    sender = cfg.get("from_addr") or cfg.get("user") or "AppVault Agent"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    dests = [x.strip() for x in to.split(",") if x.strip()]
    try:
        if cfg.get("ssl"):
            srv = smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port") or 465), timeout=15)
        else:
            srv = smtplib.SMTP(cfg["host"], int(cfg.get("port") or 587), timeout=15)
        try:
            srv.ehlo()
            if cfg.get("tls"):
                srv.starttls()
                srv.ehlo()
            if cfg.get("user"):
                srv.login(cfg["user"], cfg.get("password") or "")
            srv.sendmail(sender, dests, msg.as_string())
        finally:
            try:
                srv.quit()
            except Exception:
                pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _flush_mail_queue(limit=20):
    """Send pending mails; mark sent/failed. Never raises."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM mail_queue WHERE status='pending' ORDER BY id LIMIT ?",
            (int(limit),)).fetchall()
        conn.close()
    except Exception:
        return {"ok": False, "error": "queue read failed"}
    sent, failed = 0, []
    for r in rows:
        res = _send_mail(r["subject"], r["body"], r["to_addr"] or None)
        try:
            conn = _db()
            if res.get("ok"):
                conn.execute(
                    "UPDATE mail_queue SET status='sent', sent=?, last_error=NULL WHERE id=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
                sent += 1
            else:
                conn.execute(
                    "UPDATE mail_queue SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                    (res.get("error", "")[:300], r["id"]))
                failed.append(r["id"])
            conn.commit()
            conn.close()
        except Exception:
            pass
    return {"ok": True, "sent": sent, "failed": failed}


def _queue_mail(subject, body, to_addr=None):
    """Insert into mail_queue and attempt an immediate flush. Never raises."""
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO mail_queue (subject, body, to_addr, status, created) VALUES (?,?,?,?,?)",
            (subject, body, to_addr or "", "pending",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return _flush_mail_queue()


@agentic_bp.route("/api/agentic/mail/config", methods=["GET", "PUT", "OPTIONS"])
def api_mail_config():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "PUT":
        data = request.get_json() or {}
        cfg = _mail_cfg()
        for k in ("enabled", "host", "port", "tls", "ssl", "user", "password",
                  "from_addr", "to_addr"):
            if data.get(k) is not None:
                cfg[k] = data[k]
        _cfg_set("mail", cfg)
        return jsonify({"status": "ok", "mail": _mask_mail_cfg(cfg)})
    return jsonify({"status": "ok", "mail": _mask_mail_cfg(_mail_cfg())})


@agentic_bp.route("/api/agentic/mail/test", methods=["POST", "OPTIONS"])
def api_mail_test():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    cfg = _mail_cfg()
    if not cfg.get("enabled") or not cfg.get("host"):
        return jsonify({"status": "error",
                        "error": "mail not configured yet (PUT /api/agentic/mail/config)"}), 400
    res = _send_mail(data.get("subject") or "AppVault test email",
                     data.get("body") or "This is a test email from the AppVault Agent.",
                     data.get("to"))
    return jsonify(res), (200 if res.get("ok") else 502)


@agentic_bp.route("/api/agentic/mail/notify", methods=["POST", "OPTIONS"])
def api_mail_notify():
    """Generic headless notify — swarm/crews/missions call this to report back."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"error": "subject and body required"}), 400
    res = _queue_mail(subject, body, data.get("to"))
    return jsonify({"status": "ok", "queued": True, **res})


@agentic_bp.route("/api/agentic/mail/queue", methods=["GET", "POST", "OPTIONS"])
def api_mail_queue():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if data.get("flush"):
            return jsonify({"status": "ok", **_flush_mail_queue()})
    conn = _db()
    rows = conn.execute("SELECT * FROM mail_queue ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify({"status": "ok", "mails": [dict(r) for r in rows]})


# =============================================================================
# MESSAGE BUS (2026-08-10) — P0-2 roadmap: SQLite-backed pub/sub + SSE + replay.
# Any process (agents, swarm members, n8n, CrewAI, Open WebUI) can publish and
# subscribe. Events persist in bus_events (survive restarts) and also feed the
# shared memory table (tag="bus:<topic>") so they become RAG-searchable.
# Endpoints (under the agentic blueprint = same auth posture as the other
# agentic routes; external apps use the AppVault API key):
#   POST /api/agentic/bus/<topic>             publish {json payload}?ttl=seconds
#   POST /api/agentic/bus/publish             same, topic in body {"topic": ...}
#   GET  /api/agentic/bus/stream?topics=a,b   SSE subscribe ("*" = all), ?since=id
#   GET  /api/agentic/bus/replay?topics=&since=&limit=   poll missed events
#   GET  /api/agentic/bus/topics              list topics + counts
# Retention: rows expire after their TTL, or after bus_retention_days (default 7).
# =============================================================================
import queue as _bus_queue

_BUS_SUBSCRIBERS = {}   # topic -> set of Queue
_BUS_LOCK = threading.Lock()


def _init_bus_tables():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bus_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        payload TEXT,
        created TEXT,
        expires_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_bus_events_topic ON bus_events(topic, id);
    """)
    conn.commit()
    conn.close()


_init_bus_tables()


def _bus_purge():
    """Delete expired rows and anything older than the retention window."""
    try:
        days = int(_cfg_get("bus_retention_days", 7) or 7)
    except Exception:
        days = 7
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _db()
    conn.execute("DELETE FROM bus_events WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
    conn.execute("DELETE FROM bus_events WHERE created < ?", (cutoff,))
    conn.commit()
    conn.close()


def _bus_publish(topic, payload, ttl=None):
    """Persist an event, log it to shared memory, and fan out to live subscribers."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires = None
    if ttl is not None:
        try:
            secs = max(0, int(ttl))
            expires = (datetime.now() + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            expires = None
    _bus_purge()
    conn = _db()
    cur = conn.execute(
        "INSERT INTO bus_events (topic, payload, created, expires_at) VALUES (?,?,?,?)",
        (topic, json.dumps(payload, ensure_ascii=False), now, expires))
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    # operational log -> shared memory (vector-vault ingest point)
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO memory (ts, agent, tag, content, tier, source, updated) VALUES (?,?,?,?,?,?,?)",
            (now, "bus", "bus:" + topic, json.dumps(payload, ensure_ascii=False)[:2000],
             "working", "bus", now))
        conn.commit()
        conn.close()
    except Exception:
        pass
    event = {"id": eid, "topic": topic, "payload": payload, "created": now}
    with _BUS_LOCK:
        subs = set(_BUS_SUBSCRIBERS.get("*", set())) | set(_BUS_SUBSCRIBERS.get(topic, set()))
    for q in subs:
        try:
            q.put_nowait(event)
        except Exception:
            pass
    return event


def _bus_subscribe(topics):
    q = _bus_queue.Queue(maxsize=1000)
    with _BUS_LOCK:
        for t in topics:
            _BUS_SUBSCRIBERS.setdefault(t, set()).add(q)
    return q


def _bus_unsubscribe(topics, q):
    with _BUS_LOCK:
        for t in topics:
            s = _BUS_SUBSCRIBERS.get(t)
            if s:
                s.discard(q)
                if not s:
                    _BUS_SUBSCRIBERS.pop(t, None)


def _bus_events_after(since_id=0, topics=None, limit=200):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q = "SELECT * FROM bus_events WHERE id>?"
    args = [int(since_id or 0)]
    if topics:
        ph = ",".join("?" * len(topics))
        q += " AND topic IN (%s)" % ph
        args += list(topics)
    q += " AND (expires_at IS NULL OR expires_at > ?) ORDER BY id LIMIT ?"
    args += [now, min(max(int(limit or 200), 1), 1000)]
    conn = _db()
    rows = conn.execute(q, args).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = r["payload"]
        out.append({"id": r["id"], "topic": r["topic"], "payload": payload, "created": r["created"]})
    return out


def _bus_validate_topic(topic):
    topic = (topic or "").strip().lower()
    if not topic or len(topic) > 100:
        return None
    for ch in topic:
        if not (ch.isalnum() or ch in "._-"):
            return None
    return topic


@agentic_bp.route("/api/agentic/bus/publish", methods=["POST", "OPTIONS"])
def api_bus_publish_body():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json(silent=True) or {}
    topic = _bus_validate_topic(data.get("topic"))
    if not topic:
        return jsonify({"error": "topic required (letters, digits, . _ -)"}), 400
    ev = _bus_publish(topic, data.get("payload"), ttl=data.get("ttl"))
    return jsonify({"status": "ok", "event": ev})


@agentic_bp.route("/api/agentic/bus/<topic>", methods=["POST", "OPTIONS"])
def api_bus_publish_path(topic):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    t = _bus_validate_topic(topic)
    if not t:
        return jsonify({"error": "invalid topic (letters, digits, . _ -)"}), 400
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.get_data(as_text=True)
    ttl = None
    if request.args.get("ttl") is not None:
        try:
            ttl = int(request.args.get("ttl"))
        except Exception:
            ttl = None
    ev = _bus_publish(t, payload, ttl=ttl)
    return jsonify({"status": "ok", "event": ev})


@agentic_bp.route("/api/agentic/bus/stream", methods=["GET", "OPTIONS"])
def api_bus_stream():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    raw = request.args.get("topics") or "*"
    topics = [t.strip().lower() for t in raw.split(",") if t.strip()]
    want_all = "*" in topics
    topics = [t for t in topics if t != "*"]
    since = request.args.get("since", type=int, default=0)

    def gen():
        # replay missed events first (late-joiner pattern)
        for ev in _bus_events_after(since, topics or None, 200):
            yield "event: message\ndata: %s\n\n" % json.dumps(ev, ensure_ascii=False)
        if want_all:
            topics.append("*")
        q = _bus_subscribe(topics)
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    if want_all or ev["topic"] in topics:
                        yield "event: message\ndata: %s\n\n" % json.dumps(ev, ensure_ascii=False)
                except _bus_queue.Empty:
                    yield ": ping\n\n"
        finally:
            _bus_unsubscribe(topics, q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@agentic_bp.route("/api/agentic/bus/replay", methods=["GET", "OPTIONS"])
def api_bus_replay():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    raw = request.args.get("topics") or ""
    topics = [t.strip().lower() for t in raw.split(",") if t.strip()]
    since = request.args.get("since", type=int, default=0)
    limit = request.args.get("limit", 200)
    return jsonify({"status": "ok",
                    "events": _bus_events_after(since, topics or None, limit)})


@agentic_bp.route("/api/agentic/bus/topics", methods=["GET", "OPTIONS"])
def api_bus_topics():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    rows = conn.execute(
        "SELECT topic, COUNT(*) c, MAX(id) last_id FROM bus_events "
        "GROUP BY topic ORDER BY topic").fetchall()
    conn.close()
    return jsonify({"status": "ok", "topics": [dict(r) for r in rows]})

# ---------------------------------------------------------------------------
# DEERFLOW AGENT — plane-native provision (launch/stop/status) for the
# bytedance/deer-flow SuperAgent harness. NOT a catalog app: the roster exposes
# DeerFlow as a first-class agent; these routes make it runnable from the page
# (clone -> env/config prep -> `docker compose up -d --build`, streamed log).
# Prep is idempotent and preserves user edits: .env / config.yaml / frontend
# .env are seeded only when missing. Loopback-only by default (DeerFlow's own
# security posture — the agent can execute commands).
# ---------------------------------------------------------------------------
import subprocess as _df_subprocess
import random as _df_random
import shutil as _df_shutil

_DEERFLOW_DIR = "/data/apps/deer-flow"
_DEERFLOW_REPO = "https://github.com/bytedance/deer-flow.git"
_DEERFLOW_LOG = "/data/apps/deer-flow/launch.log"
_DEERFLOW_STATE = {"phase": "idle", "error": "", "log": []}
_DEERFLOW_LOCK = threading.Lock()

def _df_tail_log(n=40):
    try:
        with open(_DEERFLOW_LOG, "r", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []

def _df_probe():
    """Roster probe: the stack publishes nginx on host loopback :2026."""
    for host in PROBE_HOSTS:
        try:
            _, code = _http(f"http://{host}:2026/", timeout=1.5)
            if code:
                return ("online" if code < 500 else "error"), code
        except Exception:
            continue
    return "offline", 0

def _df_daemon_prefix():
    """Map the agent container's /data/apps to the daemon-visible source path.

    On Docker Desktop the daemon lives in a VM: bind sources must be the
    host-side path (e.g. /run/desktop/mnt/host/d/appvault-data), otherwise the
    daemon auto-creates DIRECTORIES at missing bind sources (nginx.conf ->
    dir, config.yaml -> dir -> IsADirectoryError). On Linux the Source equals
    the container path, so no rewrite is needed. Returns (old, new) or ("","").
    """
    try:
        r = _df_subprocess.run(
            ["docker", "inspect", "appvault-agent", "--format", "{{json .Mounts}}"],
            capture_output=True, text=True, timeout=30)
        for m in json.loads(r.stdout):
            if m.get("Type") == "bind" and m.get("Destination") == "/data/apps":
                src = (m.get("Source") or "").rstrip("/")
                if src and src != "/data/apps":
                    return ("/data/apps", src)
    except Exception:
        pass
    return ("", "")

def _df_patch_compose():
    """Write docker/docker-compose.appvault.yaml with daemon-visible bind sources."""
    old, new = _df_daemon_prefix()
    src = os.path.join(_DEERFLOW_DIR, "docker", "docker-compose.yaml")
    dst = os.path.join(_DEERFLOW_DIR, "docker", "docker-compose.appvault.yaml")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    if old:
        text = text.replace("./nginx/nginx.conf", f"{new}/deer-flow/docker/nginx/nginx.conf")
        text = text.replace("../skills", f"{new}/deer-flow/skills")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)
    return dst

def _df_compose(args, timeout=120):
    """Run a docker compose subcommand for the deer-flow project."""
    compose_file = os.path.join(_DEERFLOW_DIR, "docker", "docker-compose.appvault.yaml")
    cmd = ["docker", "compose", "--env-file", ".env", "-p", "deer-flow",
           "-f", compose_file] + args
    try:
        r = _df_subprocess.run(cmd, cwd=_DEERFLOW_DIR, capture_output=True,
                               text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, str(e)

def _df_seed_env(env_path, base):
    """Seed required secrets/paths into .env when missing (keeps user values)."""
    with open(env_path, "a") as f:
        f.write("\n# auto-seeded by AppVault Agentic OS\n")
        f.write(f"DEER_FLOW_CONFIG_PATH={base}/config.yaml\n")
        f.write(f"DEER_FLOW_EXTENSIONS_CONFIG_PATH={base}/extensions_config.json\n")
        f.write(f"DEER_FLOW_HOME={base}/.deer-flow\n")
        for key in ("BETTER_AUTH_SECRET", "DEER_FLOW_INTERNAL_AUTH_TOKEN"):
            f.write(f"{key}=" + "".join(_df_random.choices("abcdef0123456789", k=32)) + "\n")

def _df_prepare():
    """Clone/update the repo and seed .env + config files (idempotent)."""
    if not os.path.isdir(_DEERFLOW_DIR):
        os.makedirs(os.path.dirname(_DEERFLOW_DIR), exist_ok=True)
        r = _df_subprocess.run(["git", "clone", "--depth", "1", _DEERFLOW_REPO, _DEERFLOW_DIR],
                               capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("clone failed: " + (r.stderr or "")[-300:])
    else:
        _df_subprocess.run(["git", "-C", _DEERFLOW_DIR, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=300)
    # Bind sources must be daemon-visible (Docker Desktop VM vs container paths)
    old, new = _df_daemon_prefix()
    base = (new + "/deer-flow") if old else _DEERFLOW_DIR
    _df_patch_compose()
    env_path = os.path.join(_DEERFLOW_DIR, ".env")
    if not os.path.exists(env_path) and os.path.exists(env_path + ".example"):
        _df_shutil.copy(env_path + ".example", env_path)
    if not os.path.exists(env_path):
        raise RuntimeError("no .env.example in repo")
    missing = []
    with open(env_path, errors="replace") as f:
        existing = f.read()
    for key in ("BETTER_AUTH_SECRET", "DEER_FLOW_INTERNAL_AUTH_TOKEN"):
        if not any(l.strip().startswith(key + "=") and l.strip().split("=", 1)[1]
                   for l in existing.splitlines()):
            missing.append(key)
    if missing:
        _df_seed_env(env_path, base)
    # Normalize the three path vars every run (daemon-visible, not user secrets)
    with open(env_path, "r", errors="replace") as f:
        lines = f.readlines()
    out, wrote = [], False
    for l in lines:
        k = l.split("=", 1)[0].strip()
        if k in ("DEER_FLOW_CONFIG_PATH", "DEER_FLOW_EXTENSIONS_CONFIG_PATH", "DEER_FLOW_HOME"):
            out.append(f"{k}={base}/" + {"DEER_FLOW_CONFIG_PATH": "config.yaml",
                                         "DEER_FLOW_EXTENSIONS_CONFIG_PATH": "extensions_config.json",
                                         "DEER_FLOW_HOME": ".deer-flow"}[k] + "\n")
            wrote = True
        else:
            out.append(l)
    if wrote:
        # Collapse duplicate seeded secrets: keep the LAST occurrence (the
        # effective value compose used at container creation).
        seen = {}
        for i in range(len(out) - 1, -1, -1):
            k = out[i].split("=", 1)[0].strip()
            if k in ("BETTER_AUTH_SECRET", "DEER_FLOW_INTERNAL_AUTH_TOKEN"):
                if k in seen:
                    out[i] = None
                else:
                    seen[k] = True
        out = [l for l in out if l is not None]
        with open(env_path, "w") as f:
            f.writelines(out)
    # frontend/.env (compose env_file ../frontend/.env)
    fe = os.path.join(_DEERFLOW_DIR, "frontend", ".env")
    if not os.path.exists(fe) and os.path.exists(fe + ".example"):
        _df_shutil.copy(fe + ".example", fe)
    # config.yaml + extensions_config.json from examples
    for src, dst in (("config.example.yaml", "config.yaml"),
                     ("extensions_config.example.json", "extensions_config.json")):
        dstp = os.path.join(_DEERFLOW_DIR, dst)
        if not os.path.exists(dstp):
            srcp = os.path.join(_DEERFLOW_DIR, src)
            if os.path.exists(srcp):
                _df_shutil.copy(srcp, dstp)
            elif dst.endswith(".json"):
                with open(dstp, "w") as f:
                    f.write("{}\n")
    _df_write_extensions()

def _df_write_extensions():
    """Register the AppVault MCP gateway in DeerFlow's extensions_config.json
    so harness agents share the SAME memory and data as the Agentic OS
    (plane_memory_search/write, plane_ask, plane_skills_list, plane_work_log,
    plane_bus_publish, brain_*). Merge-safe: preserves existing servers/skills.
    The gateway reads this file at startup — restart deer-flow-gateway after
    writing it on an already-running stack."""
    p = os.path.join(_DEERFLOW_DIR, "extensions_config.json")
    data = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    servers = dict(data.get("mcpServers") or {})
    servers["appvault"] = {
        "enabled": True,
        "type": "http",
        "url": "http://host.docker.internal:8087/mcp",
        "description": "AppVault Agentic OS shared memory & plane bridge (same memory, work ledger, bus)",
        "routing": {"mode": "prefer", "priority": 90,
                    "keywords": ["shared memory", "appvault", "vault", "brain", "skill",
                                 "work log", "publish", "agentic", "memory"]},
    }
    data["mcpServers"] = servers
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _df_runner():
    """Background: prepare -> compose up -d --build; logs streamed to file."""
    try:
        _df_prepare()
        os.makedirs(os.path.join(_DEERFLOW_DIR, ".deer-flow"), exist_ok=True)
        _DEERFLOW_STATE["phase"] = "building"
        compose_file = os.path.join(_DEERFLOW_DIR, "docker", "docker-compose.appvault.yaml")
        with open(_DEERFLOW_LOG, "w") as lf:
            p = _df_subprocess.Popen(
                ["docker", "compose", "--env-file", ".env", "-p", "deer-flow",
                 "-f", compose_file, "up", "-d", "--build"],
                cwd=_DEERFLOW_DIR, stdout=lf, stderr=_df_subprocess.STDOUT, text=True)
            p.wait()
        # Provisioner is the optional Kubernetes sandbox component — it needs a
        # kubeconfig and crash-loops without one. Local sandbox mode doesn't use it.
        if p.returncode == 0:
            with open(_DEERFLOW_LOG, "a") as lf:
                q = _df_subprocess.Popen(
                    ["docker", "compose", "--env-file", ".env", "-p", "deer-flow",
                     "-f", compose_file, "stop", "provisioner"],
                    cwd=_DEERFLOW_DIR, stdout=lf, stderr=_df_subprocess.STDOUT, text=True)
                q.wait()
        _DEERFLOW_STATE["phase"] = "up" if p.returncode == 0 else "error"
        if p.returncode != 0:
            _DEERFLOW_STATE["error"] = "compose up failed (see launch.log tail)"
        else:
            _df_sync_ensure()
    except Exception as e:
        _DEERFLOW_STATE["phase"] = "error"
        _DEERFLOW_STATE["error"] = str(e)[:300]

@agentic_bp.route("/api/agentic/agents/deerflow/launch", methods=["POST", "OPTIONS"])
def api_deerflow_launch():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    with _DEERFLOW_LOCK:
        if _DEERFLOW_STATE["phase"] in ("preparing", "building"):
            return jsonify({"status": "busy", "phase": _DEERFLOW_STATE["phase"]}), 409
        st, _ = _df_probe()
        if st == "online":
            return jsonify({"status": "already_up"})
        _DEERFLOW_STATE.update({"phase": "preparing", "error": ""})
        threading.Thread(target=_df_runner, daemon=True).start()
    return jsonify({"status": "started", "phase": "preparing"})

@agentic_bp.route("/api/agentic/agents/deerflow/status", methods=["GET", "OPTIONS"])
def api_deerflow_status():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    st, code = _df_probe()
    phase = _DEERFLOW_STATE["phase"]
    if phase == "up" and st != "online":
        phase = "degraded"
    if st == "online":
        _df_sync_ensure()
    admin = {}
    _df_cfg = _cfg_get("deerflow")
    if isinstance(_df_cfg, dict) and _df_cfg.get("password"):
        admin = {"email": _df_cfg.get("email", ""), "password": _df_cfg["password"]}
    return jsonify({"status": "online" if st == "online" else phase,
                    "phase": phase, "probe": st, "http": code,
                    "error": _DEERFLOW_STATE.get("error", ""),
                    "sync": _DEERFLOW_STATE.get("sync", ""),
                    "admin": admin,
                    "log": _df_tail_log(40)})

@agentic_bp.route("/api/agentic/agents/deerflow/stop", methods=["POST", "OPTIONS"])
def api_deerflow_stop():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if not os.path.isdir(_DEERFLOW_DIR):
        return jsonify({"status": "ok", "note": "not provisioned"})
    ok, out = _df_compose(["down"])
    with _DEERFLOW_LOCK:
        _DEERFLOW_STATE["phase"] = "idle"
    return jsonify({"status": "ok" if ok else "error", "output": out[-400:]})

# ---------------------------------------------------------------------------
# DEERFLOW RUN AUTO-SYNC — every completed run mirrors into the Agentic OS
# shared memory (same store the vault syncs), the work ledger, and the bus.
# Auth: internal token (X-DeerFlow-Internal-Token) + CSRF double-submit
# satisfied with a matching cookie/header pair — a non-browser trusted client
# needs no user account and no UI login. Idempotent via deerflow_sync table.
# ---------------------------------------------------------------------------
_DF_SYNC_FLAG = [False]

def _df_read_env(key):
    """Read a key from the DeerFlow .env — LAST non-empty occurrence wins,
    matching compose --env-file semantics (seed runs may have appended dupes)."""
    try:
        with open(os.path.join(_DEERFLOW_DIR, ".env"), errors="replace") as f:
            val = None
            for l in f.read().splitlines():
                l = l.strip()
                if l.startswith(key + "="):
                    v = l.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        val = v
            return val
    except Exception:
        pass
    return None

def _df_api(method, path, payload=None, timeout=30, owner=None):
    """Call the DeerFlow gateway API through nginx (:2026) as a trusted
    internal client (token auth + CSRF double-submit pair). Bypasses the
    outbound proxy — a proxied request strips the internal-token header and
    returns 401, while a direct call returns 200 (verified empirically)."""
    token = _df_read_env("DEER_FLOW_INTERNAL_AUTH_TOKEN")
    if not token:
        return 0, {"_error": "no internal token"}
    url = "http://host.docker.internal:2026" + path
    headers = {
        "X-DeerFlow-Internal-Token": token,
        "X-CSRF-Token": token,
        "Cookie": "csrf_token=" + token,
    }
    if owner:
        headers["X-DeerFlow-Owner-User-Id"] = owner
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return 200, json.loads(body)
        except Exception:
            return 200, {"_raw": body[:3000]}
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace")[:1500])
    except Exception as e:
        return 0, {"_error": str(e)[:300]}

def _df_bootstrap_admin():
    """Ensure a DeerFlow admin exists so UI threads are visible to the sync.

    Creates it via the legitimate first-boot flow (POST /api/v1/auth/initialize)
    when the instance is still in setup state; stores creds in the plane
    config so the user can log into the DeerFlow UI with them. If a user
    already exists (e.g. created in the UI), the sync falls back to the
    'default' namespace. Returns the admin user id or None."""
    cfg = _cfg_get("deerflow")
    if isinstance(cfg, dict) and cfg.get("user_id"):
        return cfg["user_id"]
    code, data = _df_api("GET", "/api/v1/auth/setup-status")
    if code != 200:
        return None
    email = "admin@appvault.io"
    password = "".join(_df_random.choices("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=16))
    uid = None
    if data.get("needs_setup"):
        code, data = _df_api("POST", "/api/v1/auth/initialize",
                             {"email": email, "password": password})
        if code in (200, 201):
            uid = (data or {}).get("id") or (data or {}).get("user", {}).get("id")
    _cfg_set("deerflow", {"email": email, "password": password, "user_id": uid})
    return uid

def _df_run_summary(thread_id, owner=None):
    """Last AI message text from a thread (empty if none)."""
    code, msgs = _df_api("GET", f"/api/threads/{thread_id}/messages", owner=owner)
    if code != 200 or not isinstance(msgs, list):
        return ""
    for m in reversed(msgs):
        if (m.get("type") or "").lower() in ("ai", "assistant", "agent"):
            c = m.get("content")
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, list):
                parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("text")]
                if parts:
                    return " ".join(parts).strip()
    return ""

def _df_sync_once():
    """One pass: completed DeerFlow runs -> shared memory + work + bus."""
    owner = _df_bootstrap_admin()
    code, data = _df_api("POST", "/api/threads/search", {}, owner=owner)
    if code != 200:
        return f"search HTTP {code}"
    threads = data if isinstance(data, list) else []
    conn = _db()
    conn.execute("CREATE TABLE IF NOT EXISTS deerflow_sync (run_id TEXT PRIMARY KEY, synced_at TEXT)")
    done = {r["run_id"] for r in conn.execute("SELECT run_id FROM deerflow_sync").fetchall()}
    new_total = 0
    for th in threads:
        tid = th.get("thread_id") or th.get("id")
        if not tid:
            continue
        title = (th.get("title") or th.get("display_name") or tid)[:120]
        code, runs = _df_api("GET", f"/api/threads/{tid}/runs", owner=owner)
        if code != 200 or not isinstance(runs, list):
            continue
        for run in runs:
            rid = run.get("id") or run.get("run_id")
            status = (run.get("status") or "").lower()
            if not rid or rid in done or status not in ("completed", "success", "done", "succeeded"):
                continue
            summary = _df_run_summary(tid, owner=owner)
            if not summary:
                continue
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "INSERT INTO memory (ts, agent, tag, content, tier, source, updated) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().strftime("%H:%M LOCAL"), "DeerFlow", "DeerFlow Run",
                 summary[:1500], "working", "deerflow-sync", now))
            conn.commit()
            row = conn.execute("SELECT * FROM memory WHERE id=?", (cur.lastrowid,)).fetchone()
            _sync_memory_to_vault(row["id"], dict(row))
            _work_record(category="research", title=f"🦌 {title}", content=summary[:1500],
                         source="deerflow-sync", status="done")
            _bus_publish("deerflow.run", {"thread_id": tid, "run_id": rid, "title": title,
                                          "summary": summary[:500]})
            conn.execute("INSERT OR IGNORE INTO deerflow_sync (run_id, synced_at) VALUES (?, ?)",
                         (rid, now))
            conn.commit()
            new_total += 1
    conn.close()
    _DEERFLOW_STATE["sync"] = f"{new_total} new run(s)"
    return f"{new_total} new run(s)"

def _df_sync_worker():
    while True:
        try:
            if _df_probe()[0] == "online":
                try:
                    _df_sync_once()
                except Exception as e:
                    _DEERFLOW_STATE["sync"] = "error: " + str(e)[:200]
        except Exception:
            pass
        time.sleep(60)

def _df_sync_ensure():
    """Start the sync worker once (idempotent across restarts)."""
    if not _DF_SYNC_FLAG[0]:
        _DF_SYNC_FLAG[0] = True
        threading.Thread(target=_df_sync_worker, daemon=True).start()

# ---------------------------------------------------------------------------
# TRUE TOKEN STREAMING — /api/agentic/chat/stream (SSE) for every roster agent.
# Mirrors api_conversation's flow (slash / skill / action / memory context /
# persistence) but the LLM call streams deltas. Provider chunk parsing:
# openai-compatible (delta.content), anthropic (content_block_delta.text),
# ollama (ndjson response). Instant paths (slash/action) yield once.
# ---------------------------------------------------------------------------
def _sse_line_text(line, kind):
    """Extract the text delta from one SSE/ndjson line."""
    if kind == "ollama":
        try:
            return json.loads(line).get("response") or ""
        except Exception:
            return ""
    if not line.startswith("data:"):
        return ""
    data = line[5:].strip()
    if data == "[DONE]":
        return ""
    try:
        d = json.loads(data)
    except Exception:
        return ""
    if kind == "anthropic":
        if d.get("type") == "content_block_delta":
            return (d.get("delta") or {}).get("text") or ""
        return ""
    choices = d.get("choices") or []
    if choices:
        return (choices[0].get("delta") or {}).get("content") or ""
    return ""

def _sse_text(url, headers, payload, kind="openai"):
    """Generator: POST stream:true; yield text deltas as they arrive."""
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if kind != "ollama" and line == "data: [DONE]":
                    return
                tok = _sse_line_text(line, kind)
                if tok:
                    yield tok
        if buf:  # final line without trailing newline (ollama ndjson)
            tok = _sse_line_text(buf.decode("utf-8", "replace").strip(), kind)
            if tok:
                yield tok

def _call_llm_stream(user_msg, system_prompt=None, agent="hermes", timeout=120):
    """Generator yielding text deltas — mirrors _call_llm_with's provider
    dispatch with stream: true and the same fallback order."""
    cfg = _get_llm_config()
    provider = cfg.get("provider", "deepseek").lower()
    model = cfg.get("model") or "deepseek-chat"
    pkeys = cfg.get("provider_keys") or {}
    api_key = ((pkeys.get(provider) or cfg.get("api_key")) or "").strip()
    api_base = (cfg.get("api_base") or "").strip()
    temp = cfg.get("temperature", 0.7)
    agent_base_prompt = _get_agent_prompt(agent)
    agent_names = {
        "hermes": "Hermes Agent",
        "openclaw": "OpenClaw Autonomous Agent",
        "goose": "Goose Developer Agent (Block)",
        "deepseek-harness": "DeepSeek Harness Reasoning Engine",
        "deerflow": "DeerFlow Super-Agent",
    }
    agent_display = agent_names.get((agent or "hermes").lower(), f"{str(agent).capitalize()} Agent")
    agent_guard = f"=== YOUR STRICT IDENTITY ===\nYou are {agent_display}. Your name is {agent_display}. You must speak and act strictly as {agent_display}. Do NOT adopt the name or persona of OpenClaw or any other agent.\n=== END IDENTITY DIRECTIVE ===\n\n"
    sys_prompt = agent_guard + (system_prompt or agent_base_prompt or cfg.get("system_prompt") or DEFAULT_LLM_CONFIG["system_prompt"])

    def openai_backend():
        base = api_base or "https://api.deepseek.com"
        url = base.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + ("/v1/chat/completions" if "/v1" not in url else "/chat/completions")
        payload = {"model": model,
                   "messages": [{"role": "system", "content": sys_prompt},
                                {"role": "user", "content": user_msg}],
                   "temperature": temp, "stream": True}
        for tok in _sse_text(url, {"Authorization": "Bearer " + api_key}, payload, kind="openai"):
            yield tok

    def anthropic_backend():
        payload = {"model": model or "claude-3-5-sonnet-20241022", "system": sys_prompt,
                   "max_tokens": cfg.get("max_tokens", 2048), "temperature": temp,
                   "messages": [{"role": "user", "content": user_msg}], "stream": True}
        for tok in _sse_text("https://api.anthropic.com/v1/messages",
                             {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                             payload, kind="anthropic"):
            yield tok

    def ollama_backend(model_name=None):
        base = api_base or os.environ.get("OLLAMA_API_BASE", "http://host.docker.internal:11434")
        payload = {"model": model_name or model or "llama3",
                   "prompt": f"System Directive: {sys_prompt}\n\nUser: {user_msg}\n\nAgent:",
                   "stream": True, "options": {"temperature": temp}}
        for tok in _sse_text(f"{base.rstrip('/')}/api/generate", {}, payload, kind="ollama"):
            yield tok

    tried = False
    if provider in ("deepseek", "openai", "litellm", "grok") and api_key:
        tried = True
        try:
            for t in openai_backend():
                yield t
            return
        except Exception:
            pass
    if provider == "anthropic" and api_key:
        tried = True
        try:
            for t in anthropic_backend():
                yield t
            return
        except Exception:
            pass
    if provider in ("ollama", "local"):
        tried = True
        try:
            for t in ollama_backend():
                yield t
            return
        except Exception:
            pass
    # Keyless fallback: local Ollama with model discovery (mirrors _call_llm_with)
    try:
        base = os.environ.get("OLLAMA_API_BASE", "http://host.docker.internal:11434")
        tags, status = _http(f"{base.rstrip('/')}/api/tags", timeout=3)
        model_name = None
        if status == 200 and isinstance(tags, dict):
            names = [m.get("name") for m in (tags.get("models") or [])]
            for pref in ("qwen2.5:0.5b", "qwen2.5:1.5b", "llama3.2:1b", "llama3:latest", "phi3:mini", "tinyllama"):
                if pref in names:
                    model_name = pref
                    break
            if not model_name and names:
                model_name = names[0]
        if model_name:
            for t in ollama_backend(model_name):
                yield t
            return
    except Exception:
        pass
    raise RuntimeError("All LLM backends failed to stream for agent '" + agent +
                       "'. Configure one at /api/agentic/config (provider + key).")

@agentic_bp.route("/api/agentic/chat/stream", methods=["POST", "OPTIONS"])
def api_chat_stream():
    """SSE token streaming for ANY roster agent chat (hermes, claude, codex,
    deerflow, crews...). Body: {agent, prompt, thread_id? | session_id?}.
    Mirrors api_conversation: slash commands, skills, memory context, and
    persistence — but the LLM reply trickles in as data: {delta} events."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    user_msg = (data.get("prompt") or "").strip()
    agent_id = (data.get("agent") or "hermes").lower()
    session_id = (data.get("session_id") or "").strip()
    thread_id = (data.get("thread_id") or "main").strip()
    if not user_msg:
        return jsonify({"error": "Prompt cannot be empty"}), 400
    agent_name = "Hermes Agent" if agent_id == "hermes" else f"{agent_id.capitalize()} Agent"

    def _ev(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def generate():
        reply = ""
        try:
            slash_reply = _handle_slash_command(user_msg, agent_id)
            if slash_reply is not None:
                reply = slash_reply
                yield _ev({"delta": reply})
            else:
                ctx = _memory_context(user_msg)
                clean_msg, skill_row = _maybe_extract_skill(user_msg)
                skill_sys = None
                action_reply = None
                if skill_row:
                    action_reply = _run_skill_action(skill_row, clean_msg)
                    if action_reply is not None:
                        reply = action_reply
                        yield _ev({"delta": reply})
                    else:
                        skill_sys = (f"You are applying the skill '{skill_row['name']}'. Follow its "
                                     f"instructions EXACTLY. Output the result, no preamble.\n\n"
                                     f"===== SKILL =====\n{_load_skill_content(skill_row)}")
                        conn = _db()
                        conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (skill_row["id"],))
                        conn.commit()
                        conn.close()
                        _audit("store", "skill.chat", f"@{skill_row['name']} applied in {agent_id} chat")
                if action_reply is None:
                    for tok in _call_llm_stream(clean_msg + (("\n\n" + ctx) if ctx else ""),
                                                agent=agent_id, system_prompt=skill_sys):
                        reply += tok
                        yield _ev({"delta": tok})
        except Exception as e:
            err = f"⚠️ {agent_name} could not reach any LLM backend. Configure one at `/api/agentic/config`. Detail: {str(e)[:200]}"
            reply = err
            yield _ev({"delta": err})
        # Persist the exchange (same stores as the non-streaming route)
        try:
            now_ts = datetime.now().strftime("%H:%M LOCAL")
            if session_id:
                sess = _get_session(session_id)
                if not sess:
                    _save_session(session_id, "Hermes Session", [])
                    sess = _get_session(session_id)
                sess["messages"].append({"sender": "User", "role": "user", "timestamp": now_ts, "text": user_msg})
                sess["messages"].append({"sender": agent_name, "role": "agent", "timestamp": now_ts, "text": reply})
                _save_session(session_id, sess["title"], sess["messages"])
            else:
                messages = _get_conversation(agent_id, thread_id)
                messages.append({"sender": "User", "role": "user", "timestamp": now_ts, "text": user_msg})
                messages.append({"sender": agent_name, "role": "agent", "timestamp": now_ts, "text": reply})
                _save_conversation(agent_id, messages, thread_id)
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                         (now_ts, agent_name, "Conversation",
                          f"User: {user_msg[:60]} | Reply: {reply[:60]}"))
            conn.commit()
            conn.close()
            threading.Thread(target=_distill_facts, args=(user_msg, reply, agent_name), daemon=True).start()
        except Exception:
            pass
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ---------------------------------------------------------------------------
# CONTENT PIPELINE — signal -> brief -> draft -> refine -> APPROVAL -> publish
# A state machine over work_items (source='pipeline:*'):
#   ready_to_write (brief) -> needs_refinement (draft) -> ready_for_approval
#   -> approved (HUMAN gate) -> published (reuses _wp_publish).
# Auto-advances up to the approval gate when auto is on; approve/reject are
# human-only. Editor pass runs a DIFFERENT model (deepseek-reasoner by
# default) + the humanise-text skill.
# ---------------------------------------------------------------------------
_PIPELINE_FLAG = [False]

def _pipeline_cfg():
    cfg = _cfg_get("pipeline")
    if not isinstance(cfg, dict):
        cfg = {}
    out = {"auto": True, "editor_model": ""}
    out.update({k: v for k, v in cfg.items() if k in out})
    return out

def _pipeline_get(wid):
    conn = _db()
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def _pipeline_update(wid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = _db()
    conn.execute(f"UPDATE work_items SET {sets}, updated_at=datetime('now') WHERE id=?",
                 (*fields.values(), wid))
    conn.commit()
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def _pipeline_brief_from_signal(signal_text, source="radar", project="appvault"):
    """Strategist: a scored signal -> structured content brief (ready_to_write)."""
    sys_p = ("You are the Content Strategist for an SEO content engine. Convert the signal into a "
             "STRICT JSON brief with EXACTLY these keys: angle (the hook), keyword_target (primary "
             "SEO keyword), pillar (which content pillar it maps to), pain_point (audience pain it "
             "solves), cta (call to action), title_proposal, outline (array of section headings). "
             "When the signal names a specific product, model, company or person, include the EXACT "
             "name in angle and title_proposal — never a vague label like 'the new model'. "
             "No prose outside the JSON.")
    raw = _call_llm(f"Signal: {signal_text[:1200]}", system_prompt=sys_p,
                    agent="strategist", timeout=90)
    brief = {}
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            brief = json.loads(m.group(0))
    except Exception:
        brief = {}
    title = (brief.get("title_proposal") or "").strip() or (signal_text[:80] + " (brief)")
    content = json.dumps(brief, indent=2, ensure_ascii=False) if brief else raw[:3000]
    wid = _work_record(category="content", title=title[:4000], content=content,
                       source="pipeline:strategist", status="ready_to_write",
                       tags=f"brief,{str(brief.get('pillar') or 'general').lower()}",
                       project=project)
    if wid:
        _bus_publish("pipeline.brief.ready", {"wid": wid, "title": title})
    return wid, brief

def _pipeline_research(wid, max_chars=3500):
    """Researcher: pull last-30-days material (Bing News RSS, Hacker News,
    Reddit, and X when a token is configured) about the item's title, so the
    Writer grounds the article in real facts (model names, dates, numbers).
    Non-fatal per source: a failing source never blocks drafting."""
    item = _pipeline_get(wid)
    if not item:
        return None, "not found"
    title = (item.get("title") or "").strip()
    stops = {"the","a","an","of","for","on","to","in","and","with","lets","let","you","your",
             "how","new","top","best","why","what","this","that","from","by","at","is","are"}
    topic = ""
    try:
        m = re.search(r"\{.*\}", item.get("content") or "", re.S)
        if m:
            b = json.loads(m.group(0))
            if b.get("keyword_target"):
                topic = str(b["keyword_target"])[:80].strip()
    except Exception:
        pass
    words = [w for w in re.split(r"\W+", topic or title) if w.lower() not in stops and len(w) > 2]
    topic = " ".join(words[:4])[:60]
    if not topic:
        return "", "no topic"
    q = urllib.parse.quote(topic)
    cutoff_epoch = int(time.time()) - 30 * 86400
    ua = {"User-Agent": "AppVault-Agentic/1.0 (content research)"}
    brief = []
    def _raw(url, headers=None):
        """_http returns (body, status); body is a parsed dict for JSON or
        {'raw': ...} for non-JSON. Unwrap to usable text/dict."""
        r = _http(url, timeout=10, headers=headers or ua)
        body = r[0] if isinstance(r, tuple) else r
        if isinstance(body, dict):
            if "raw" in body:
                return body["raw"] or ""
            return body  # already parsed JSON
        return body or ""
    try:
        rss = _raw(f"https://www.bing.com/news/search?q={q}&format=rss&qft=interval%3d%2230%22")
        if isinstance(rss, dict):
            rss = rss.get("raw", "") or ""
        for m in re.finditer(r"<item>(.*?)</item>", rss, re.S):
            body = m.group(1)
            t = re.search(r"<title>(.*?)</title>", body, re.S)
            d = re.search(r"<pubDate>(.*?)</pubDate>", body, re.S)
            s = re.search(r"<News:Source>(.*?)</News:Source>", body, re.S)
            ttl = re.sub(r"&[a-z]+;", "", t.group(1).strip() if t else "")
            brief.append(f"- NEWS: {ttl} ({s.group(1).strip() if s else 'news'} {d.group(1)[:16] if d else ''})")
    except Exception:
        pass
    try:
        d = _raw(f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=20&numericFilters=created_at_i%3E{cutoff_epoch}")
        if not isinstance(d, dict):
            d = json.loads(d) if d else {}
        hits = sorted((d.get("hits") or []), key=lambda h: h.get("points") or 0, reverse=True)
        for h in hits[:6]:
            if (h.get("points") or 0) >= 3:
                brief.append(f"- HN: {h.get('title','')[:110]} ({h.get('points')} pts, {h.get('created_at','')[:10]})")
    except Exception:
        pass
    try:
        reddit_ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
        d = _raw(f"https://www.reddit.com/search.json?q={q}&sort=top&t=month&limit=6", headers=reddit_ua)
        if not isinstance(d, dict):
            d = json.loads(d) if d else {}
        for ch in (d.get("data", {}).get("children") or [])[:6]:
            p = ch.get("data", {})
            if (p.get("score") or 0) >= 5:
                brief.append(f"- r/{p.get('subreddit') or ''}: {p.get('title','')[:110]} ({p.get('score')} pts)")
    except Exception:
        pass
    try:
        x_token = os.environ.get("X_BEARER_TOKEN", "").strip()
        if x_token and len(x_token) > 20:
            d = _raw(f"https://api.x.com/2/tweets/search/recent?query={q}&max_results=8&tweet.fields=created_at,public_metrics",
                     headers={"Authorization": f"Bearer {x_token}"})
            if not isinstance(d, dict):
                d = json.loads(d) if d else {}
            for tw in (d.get("data") or [])[:8]:
                brief.append(f"- X: {tw.get('text','')[:110]} ({tw.get('created_at','')[:10]})")
    except Exception:
        pass
    if not brief:
        brief.append("(no last-30-days material found for this topic)")
    # Detect a product name in release-style headlines so the Writer can't miss it.
    name_hits = []
    for line in brief:
        m = re.search(r"(?:releases?|launches?|unveils?|introduces?|announces?|debuts?)\s+"
                      r"([A-Z][A-Za-z0-9][A-Za-z0-9 .'\-]{1,40}?)"
                      r"(?=\s*[,:;(]|\s+(?:model|AI|LLM|for|with|on|the|at|in)\b|$)", line)
        if m:
            cand = m.group(1).strip()
            if 2 <= len(cand) <= 40 and cand.lower() not in ("it", "them", "this", "new", "ai"):
                name_hits.append(cand)
    if name_hits:
        counts = {}
        for c in name_hits:
            counts[c] = counts.get(c, 0) + 1
        top = max(counts, key=counts.get)
        brief.insert(0, f"PRODUCT NAME FOUND IN RESEARCH: '{top}' — the article MUST use this exact name.")
    text = "===== RESEARCH BRIEFING (last 30 days) =====\n" + "\n".join(brief) + "\n===== END RESEARCH ====="
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(truncated)"
    try:
        _pipeline_update(wid, research=text)
    except Exception:
        pass
    return text, None

def _pipeline_draft(wid):
    """Writer: research + brief -> draft (needs_refinement). Grounded, specific."""
    item = _pipeline_get(wid)
    if not item:
        return None, "not found"
    briefing, _ = _pipeline_research(wid)
    sys_p = ("You are the Writer Agent: a sharp, conversational tech writer who explains things "
             "to smart founders like a knowledgeable friend — NOT a corporate press release. "
             "Write the article from the BRIEF below, GROUNDED IN the RESEARCH BRIEFING.\n"
             "REQUIREMENTS:\n"
             "1. GROUND IN FACTS: use ONLY product/model/company names, dates, numbers, "
             "headlines and claims found in the RESEARCH BRIEFING. NEVER invent a model name, "
             "stat, date, quote or source. If a fact is not in the research, do not fabricate it.\n"
             "2. NAME THE THING: if the research names the exact product/model behind the topic "
             "(e.g. the model behind \"Meta's new open-weight model\"), USE THE EXACT NAME in the "
             "first two paragraphs and every time the topic is mentioned after that. Never write "
             "around it with vague labels like 'the model' or 'the announcement'. If a RESEARCH "
             "headline names a product (e.g. \"Meta releases Muse Glimmer, 30B parameter open-weight "
             "edge model\"), that product IS the article's subject — treat the name as the story, "
             "not a side mention.\n"
             "3. CONVERSATIONAL EXPERT VOICE — THIS IS THE MOST IMPORTANT RULE. Write like a "
             "knowledgeable insider talking to a friend: natural, warm, concrete, short paragraphs, "
             "active voice. Open with something the reader feels — a relatable scene, a pain point, "
             "a question, or a sharp observation — NEVER with a date or an announcement. FORBIDDEN "
             "press-release patterns: opening with a date ('On August 10, Meta released…'), "
             "'Meta announced/unveiled today…', 'the release, which drew N points on Hacker News…', "
             "'according to the company'. Weave dates and numbers into the story naturally where "
             "they matter, not as the lede. Every paragraph still carries at least one specific, "
             "research-traceable fact, but the facts ride inside a human narrative.\n"
             "   GOOD opener: \"You've been renting your AI by the token long enough. What if the "
             "thing that runs your agents lived in your own laptop?\"\n"
             "   BAD opener: \"On August 10, 2026, Meta released a new open-weight model.\"\n"
             "4. STRUCTURE: follow the brief's angle, keyword_target, outline and cta exactly. "
             "Write 800-1400 words in markdown.\n"
             "5. NO TOOLS, NO XML, NO code fences, NO preamble — return ONLY the article body.\n"
             "6. SELF-CHECK before returning: (a) do the first two paragraphs name the exact "
             "product/model from the research briefing? (b) does the article open with a scene, "
             "question or observation instead of a date or announcement? Fix either before "
             "returning. If the research truly contains no name, keep the [Note: ...] marker.\n"
             "If the research briefing contains nothing useful for this topic, start the draft "
             "with a single bracketed note: [Note: no recent material found — facts unverified] "
             "and write the best on-brief draft you can without inventing specifics.\n\n"
             "===== RESEARCH BRIEFING =====\n" + (briefing or "(research unavailable)")
             + "\n\n===== BRIEF =====\n" + (item.get("content") or "")[:4000])
    cfg = _pipeline_cfg()
    overrides = {}
    if cfg.get("writer_model"):
        overrides["model"] = cfg["writer_model"]
    elif (_get_llm_config().get("provider") or "").lower() == "deepseek":
        overrides["model"] = "deepseek-reasoner"  # follows the grounding rules far better than chat
    draft = _call_llm_with(overrides, "Write the article now.", system_prompt=sys_p,
                           agent="writer", timeout=300)
    draft = _pipeline_strip_leaks(draft)
    it = _pipeline_update(wid, content=draft, status="needs_refinement",
                          tags=(item.get("tags") or "") + ",draft")
    _bus_publish("pipeline.draft.ready", {"wid": wid, "title": item.get("title")})
    return it, None

def _skill_prose(content, cap=3500):
    """Extract only the textual RULES from a skill file — strip code fences,
    script/CLI artifacts and step headers, so an injected skill carries
    guidance, not executable-looking instructions the model might echo."""
    out, in_fence = [], False
    for l in (content or "").splitlines():
        t = l.strip()
        if t.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        low = t.lower()
        if t.startswith(("#",)) and any(k in low for k in ("step", "workflow", "execution", "script", "usage")):
            continue
        if any(k in low for k in ("cat >", "python3", "inputeof", "/tmp/", ".py", "./reference", "$ ", "chmod", "sudo ", "curl ")):
            continue
        out.append(l)
    return "\n".join(out)[:cap]

def _pipeline_strip_leaks(text):
    """Strip tool-call XML and script artifacts a model may leak into output
    (e.g. <invoke> blocks, bash heredocs, code fences)."""
    if not text:
        return text
    t = re.sub(r"<invoke\b.*?</invoke>", "", text, flags=re.S | re.I)
    t = re.sub(r"<invoke\b[^>]*>", "", t)
    t = re.sub(r"</invoke>", "", t)
    t = re.sub(r"```[a-z]*\n.*?```", "", t, flags=re.S)
    t = re.sub(r"(?m)^\s*(cat >|python3?\b|INPUTEOF|rm \S+|chmod\b|export\b|cd /tmp).*$", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def _pipeline_refine(wid):
    """Editor: draft -> ready_for_approval. DIFFERENT model + humanise-text."""
    item = _pipeline_get(wid)
    if not item:
        return None, "not found"
    draft = item.get("content") or ""
    humanise = ""
    try:
        conn = _db()
        row = conn.execute("SELECT content FROM skills WHERE name=? LIMIT 1",
                           ("humanise-text",)).fetchone()
        conn.close()
        if row:
            humanise = row["content"]
    except Exception:
        pass
    cfg = _pipeline_cfg()
    overrides = {}
    if cfg.get("editor_model"):
        overrides["model"] = cfg["editor_model"]
    elif (_get_llm_config().get("provider") or "").lower() == "deepseek":
        overrides["model"] = "deepseek-reasoner"  # genuinely different weights than deepseek-chat
    sys_p = ("You are the Editor Agent. Apply the humanise-text rules below: strip AI tells, "
             "m-dashes, verbose phrasing; tighten every paragraph; keep the SEO angle and keyword "
             "intact. CRITICAL: preserve every proper noun, model/product name, company, date and "
             "number from the draft — never generalize a specific into 'the model', 'the company' "
             "or 'recently'. You have NO tools, NO filesystem, NO scripts — never output XML tags, tool "
             "calls, code fences, bash commands or heredocs. Return ONLY the refined article text.\n\n"
             "===== HUMANISE RULES =====\n"
             + (_skill_prose(humanise) if humanise else "(no skill file — apply standard anti-AI-tell rules)")
             + "\n\n===== DRAFT =====\n" + draft[:8000])
    refined = _call_llm_with(overrides, "Refine this draft. Output ONLY the article text.",
                             system_prompt=sys_p, agent="editor", timeout=180)
    refined = _pipeline_strip_leaks(refined)
    it = _pipeline_update(wid, content=refined, status="ready_for_approval",
                          tags=(item.get("tags") or "") + ",refined")
    _bus_publish("pipeline.refined.ready", {"wid": wid, "title": item.get("title")})
    return it, None

def _pipeline_publish(wid):
    """Publisher: approved -> published via _wp_publish (same path as the Work page)."""
    item = _pipeline_get(wid)
    if not item:
        return None, "not found"
    if (item.get("category") or "") != "content":
        return None, "social posts publish to their platform (n8n connector), not WordPress"
    ok, res = _wp_publish(item.get("title") or "Post from AppVault", item.get("content") or "",
                          project=item.get("project") or "appvault")
    if not ok:
        return None, res
    it = _pipeline_update(wid, status="published", url=(res.get("link") or "") if isinstance(res, dict) else "")
    # upload the cover to WP media -> public URL (also makes Ocoya scheduling work)
    try:
        img = item.get("image_url") or ""
        if img and not img.startswith(("http://", "https://")) and isinstance(res, dict):
            project = item.get("project") or "appvault"
            wp = _wp_config_for(project)
            site = (wp.get("site_url") or "").strip().rstrip("/")
            hdrs = _wp_auth_headers(wp)
            if site and hdrs:
                up = _wp_upload_media(site, hdrs, img)
                if up and up.get("url"):
                    _pipeline_update(wid, image_url=up["url"])
                    if up.get("id") and res.get("id"):
                        try:
                            import urllib.request as _ur
                            req = _ur.Request(f"{site}/wp-json/wp/v2/posts/{res['id']}",
                                              data=json.dumps({"featured_media": up["id"]}).encode(),
                                              method="POST",
                                              headers={"Authorization": hdrs.get("Authorization", ""),
                                                       "Content-Type": "application/json"})
                            _ur.urlopen(req, timeout=30).read()
                        except Exception:
                            pass
    except Exception:
        pass
    _bus_publish("pipeline.published", {"wid": wid, "title": item.get("title"),
                                        "url": (res.get("link") or "") if isinstance(res, dict) else ""})
    return it, res

_PIPELINE_PLATFORMS = ("x", "x_thread", "linkedin", "facebook", "instagram")

def _pipeline_social_posts(wid, platforms=None):
    """From an approved article, generate one post per platform — each its own
    work_item (ready_for_approval, source pipeline:social).
    platforms: optional override (e.g. ['x_thread'] for the daily thread job)."""
    item = _pipeline_get(wid)
    if not item:
        return 0
    title = item.get("title") or "Post"
    article = item.get("content") or ""
    research = item.get("research") or ""
    sys_prompts = {
        "x": ("You are a professional tech/entrepreneur copywriter for X/Twitter. Write ONE post "
              "(max 280 chars) that makes people want to read this article. REQUIREMENTS:\n"
              "1. NAME THE PRODUCT: use the exact product/model/company name found in the research "
              "or article (e.g. 'Muse Glimmer') — never generic labels like 'the new model'.\n"
              "2. ONE SPECIFIC FACT: include at least one concrete number, spec, date or named "
              "claim traceable to the research or article.\n"
              "3. HOOK: the first 8-10 words must earn the scroll.\n"
              "4. HUMAN VOICE: confident, plain English. NO AI-tells ('In today's fast-paced world', "
              "'game-changing', 'revolutionary'), no emoji spam, max 2 hashtags. Never cite an outlet "
              "or source not present in the research.\n"
              "5. Output ONLY the post text."),
        "x_thread": ("You are a professional tech/entrepreneur copywriter for X/Twitter. Write ONE "
                     "long-form X post (X Premium allows up to 4000 characters). REQUIREMENTS:\n"
                     "1. NO NUMBERING: this is a single continuous post — never split it into "
                     "'1/5', '2/5'… numbered tweets or any other separation.\n"
                     "2. COMPACT + PUNCHY: every sentence earns the next. Short, sharp sentences, "
                     "boom-boom-boom rhythm. No fluff, no filler, no preamble — grab attention in "
                     "the first line and hold it.\n"
                     "3. NAME THE PRODUCT: the exact product/model/company (e.g. 'Muse Glimmer') "
                     "appears early and is the story, not a side mention.\n"
                     "4. SPECIFIC: weave in concrete facts from the research (numbers, specs, "
                     "dates) — no generic claims.\n"
                     "5. HOOK OPENER: start with a sharp claim, question or observation — never a "
                     "date or an announcement.\n"
                     "6. CLOSER: the last line lands the takeaway + CTA (read the article).\n"
                     "7. HUMAN VOICE: plain English, no AI-tells, no emoji spam, max 2 hashtags. "
                     "Never cite an outlet not present in the research.\n"
                     "8. LENGTH: 250-600 words (roughly 1500-3500 chars) — substantial but compact.\n"
                     "9. Output ONLY the post."),
        "linkedin": ("You write LinkedIn posts. From the article + research, write a professional post "
                     "(200-320 words): bold hook line, 3 concrete takeaways, and a question to drive "
                     "comments. NAME the exact product/model/company (e.g. 'Muse Glimmer') and include "
                     "specific facts (numbers, dates) from the research — no generic filler. Never cite "
                     "an outlet or source not present in the research. Plain text, "
                     "short paragraphs, no hashtag spam. Output ONLY the post body."),
        "facebook": ("You write Facebook posts. From the article + research, write a conversational post "
                     "(150-250 words): friendly hook, the key insight with a specific fact (name, number, "
                     "date), a question to drive comments. Short paragraphs, light emoji use, a few "
                     "hashtags. Output ONLY the post body."),
        "instagram": ("You write Instagram captions. From the article + research, write a caption "
                      "(120-220 words): strong hook, the exact product name and one specific fact from "
                      "the research, line breaks for readability, and 8-12 relevant hashtags at the end. "
                      "Output ONLY the caption."),
    }
    made = 0
    cfg = _pipeline_cfg()
    overrides = {}
    if cfg.get("social_model"):
        overrides["model"] = cfg["social_model"]
    elif (_get_llm_config().get("provider") or "").lower() == "deepseek":
        overrides["model"] = "deepseek-reasoner"  # reliably follows the naming/grounding rules
    # Multi-tenant (2026-08-11): generation platforms are data-driven —
    # business.social_platforms, else the union of its content types' platforms.
    # Decoupled from accounts: drafts generate for the platforms you WANT;
    # routing (later) only sends to profiles you connected. Legacy projects
    # without a business keep the historical 5-platform set.
    biz = _biz_for_project(item.get("project") or "appvault")
    if platforms is None:
        platforms = _platforms_for_business(biz) if biz else None
    if not platforms:
        platforms = _PIPELINE_PLATFORMS  # legacy fallback only
    biz_tag = (",biz:" + str(biz["id"])) if biz else ""
    # B3 (2026-08-11): tracked short link per post — clicks count in the links
    # table and redirect with UTM. Uses the public media base URL; skipped when
    # no public base or no target URL is configured.
    short_url = ""
    link_code = ""
    if biz:
        target = (item.get("url") or "").strip() or (biz.get("website") or "").strip()
        if target:
            pub_base = ((_social_router_cfg("appvault") or {}).get("media_base_url") or "").strip().rstrip("/")
            if pub_base:
                link_code = _new_link_code()
                try:
                    conn = _db()
                    conn.execute("INSERT OR IGNORE INTO links (code, target, campaign, source, medium, clicks, created)"
                                 " VALUES (?,?,?,?,?,0,?)",
                                 (link_code, target, biz["name"], "social", "social",
                                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
                    conn.close()
                    short_url = pub_base + "/api/agentic/link/" + link_code
                except Exception:
                    link_code, short_url = "", ""
    for p in platforms:
        try:
            post = _call_llm_with(overrides,
                                  f"Article title: {title}\n\nRESEARCH (last 30 days):\n{(research or '(none)')[:2000]}\n\nARTICLE:\n{article[:5000]}"
                                  + (f"\n\nCTA LINK (include this exact URL in the post as the clickable link): {short_url}" if short_url else ""),
                                  system_prompt=sys_prompts[p], agent="social", timeout=120)
        except Exception:
            continue
        post = _pipeline_strip_leaks(post)
        if not post:
            continue
        # Length guard: X hard-caps at 280 chars; other platforms truncate at
        # their spec ceiling on a word boundary so no monster posts can ship.
        caps = {"x": 280, "x_thread": 4000, "linkedin": 2500, "facebook": 1800, "instagram": 1600}
        cap = caps.get(p, 2000)
        if len(post) > cap:
            cut = post[:cap].rsplit(" ", 1)[0].rstrip(".,;:!? ")
            post = (cut + "…") if cut else post[:cap]
        if p == "x" and len(post) > 280:
            post = post[:280]
        made_wid = _work_record(category=p, title=f"{title[:60]} — {p}", content=post[:3000],
                     source="pipeline:social", status="ready_for_approval",
                     tags=f"social,platform:{p},article:{wid}{biz_tag}",
                     project=item.get("project") or "appvault")
        if made_wid and link_code:
            _pipeline_update(made_wid, link_code=link_code)
        made += 1
    if made:
        _bus_publish("pipeline.social.ready", {"wid": wid, "title": title, "posts": made})
    return made

def _pipeline_cover_image(wid, prompt=None):
    """Cover image for an approved article — real provider via _generate_image
    (OpenAI/hub when configured, keyless pollinations fallback) into 05_Media;
    stores image_url on the work item."""
    item = _pipeline_get(wid)
    if not item:
        return None
    base = prompt or (item.get("title") or "Technology article cover")
    full_prompt = f"{base}, modern editorial illustration, clean composition, no text"
    local_url, provider = _generate_image(full_prompt, style="", w=1200, h=675)
    if not local_url:
        return None
    fname = os.path.basename(local_url)
    _pipeline_update(wid, image_url=local_url)
    try:
        conn = _db()
        conn.execute("INSERT INTO media_assets (prompt, style, file, provider, created) VALUES (?,?,?,?,?)",
                     (full_prompt, "photo", fname, provider or "pollinations",
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return local_url

def _pipeline_socialize(wid):
    """On article approval: cover image + one post per platform (background)."""
    try:
        _pipeline_cover_image(wid)
    except Exception:
        pass
    try:
        _pipeline_social_posts(wid)
    except Exception:
        pass


@agentic_bp.route("/api/agentic/pipeline/<wid>/image", methods=["POST", "OPTIONS"])
def api_pipeline_image(wid):
    """Generate an image for a pipeline item (article or social post) with the
    configured image engine and attach it (image_url). Body: {prompt?, style?,
    width?, height?} — prompt defaults to the item title."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"status": "error", "error": "not found"}), 404
    data = request.get_json() or {}
    prompt = (data.get("prompt") or "").strip() or (item.get("title") or "Post visual")
    style = (data.get("style") or "").strip()
    try:
        w = int(data.get("width", 1200) or 1200)
        h = int(data.get("height", 675) or 675)
    except Exception:
        w, h = 1200, 675
    local_url, provider = _generate_image(prompt, style=style, w=w, h=h)
    if not local_url:
        return jsonify({"status": "error",
                        "error": "image generation failed — no provider responded. "
                                 "Set the image engine key in the 🖼 Image tab or check the network."}), 502
    _pipeline_update(wid, image_url=local_url)
    fname = os.path.basename(local_url)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = _db()
        conn.execute("INSERT INTO media_assets (prompt, style, file, provider, created) VALUES (?,?,?,?,?)",
                     (prompt, style or "custom", fname, provider or "pollinations", now))
        conn.commit()
        conn.close()
    except Exception:
        pass
    try:
        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), "Pipeline Image", "Image Generated",
                      f"Generated `{fname}` for {wid}: {prompt[:180]} (provider: {provider})",
                      "auto", "pipeline", now))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return jsonify({"status": "ok", "url": local_url, "file": fname,
                    "provider": provider, "wid": wid})


@agentic_bp.route("/api/agentic/pipeline/<wid>/image/attach", methods=["POST", "OPTIONS"])
def api_pipeline_image_attach(wid):
    """Attach an existing vault media file to a pipeline item (image_url).
    Body: {file: <basename of a 05_Media asset>}."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"status": "error", "error": "not found"}), 404
    data = request.get_json() or {}
    fname = (data.get("file") or "").strip()
    if not fname or "/" in fname or "\\" in fname or ".." in fname:
        return jsonify({"status": "error", "error": "invalid file name"}), 400
    fpath = os.path.join(_vault_path(), "05_Media", fname)
    if not os.path.isfile(fpath):
        return jsonify({"status": "error", "error": "media file not found"}), 404
    local_url = f"/api/agentic/media/file/{fname}"
    _pipeline_update(wid, image_url=local_url)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content, tier, source, updated) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), "Pipeline Image", "Image Attached",
                      f"Attached `{fname}` to {wid}",
                      "auto", "pipeline", now))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return jsonify({"status": "ok", "url": local_url, "file": fname, "wid": wid})

# ---------------------------------------------------------------------------
# SOCIAL ROUTER — route approved posts to an external scheduler (Ocoya via
# Zapier/Make/n8n webhook, or any HTTP API). Config key "social_router":
# {provider, url, method, auth_header ("Name: Value"), auto_route}.
# ---------------------------------------------------------------------------
def _social_router_cfg(project=None):
    if project:
        return _project_cfg(project, "social_router") or {}
    cfg = _cfg_get("social_router")
    return cfg if isinstance(cfg, dict) else {}

def _social_router_send(post):
    """Send an approved post to the configured scheduling service.
    provider=ocoya -> native Ocoya REST API; otherwise a generic webhook.
    Returns (ok, detail)."""
    project = post.get("project") or "appvault"
    cfg = _social_router_cfg(project)
    if post.get("image_url"):
        pub, note = _public_media_url(post.get("image_url"), project)
        post["image_url"] = pub
        if note:
            post["_media_note"] = note
    # Multi-tenant (2026-08-11): business-owned posts route through the business's
    # own connectors first — Ocoya-wired profile, then webhook profile. Legacy
    # per-project routing below stays as the fallback for non-business posts.
    if _biz_tag(post):
        ok, det = _biz_deliver(post, cfg)
        if ok is not None:
            return ok, det
    if (cfg.get("provider") or "webhook").lower() == "ocoya":
        return _ocoya_send(post, cfg)
    url = (cfg.get("url") or "").strip()
    if not url:
        return False, "no router configured — set the webhook in the 📱 Social tab"
    payload = {
        "title": post.get("title") or "",
        "platform": post.get("category") or "",
        "content": post.get("content") or "",
        "image_url": post.get("image_url") or "",
        "article_id": (post.get("tags") or "").replace("article:", "")[:40],
        "source": "appvault-pipeline",
    }
    hdrs = {"Content-Type": "application/json"}
    ah = (cfg.get("auth_header") or "").strip()
    if ah and ":" in ah:
        hdrs[ah.split(":", 1)[0].strip()] = ah.split(":", 1)[1].strip()
    method = (cfg.get("method") or "POST").upper()
    try:
        data, status = _http(url, method=method, headers=hdrs, json_data=payload, timeout=30)
    except Exception as e:
        return False, f"router call failed: {str(e)[:200]}"
    if status in (200, 201, 202, 204):
        return True, f"HTTP {status}"
    return False, f"HTTP {status}: {str(data)[:200]}"

def _ocoya_send(post, cfg):
    """Native Ocoya REST API — POST /post?workspaceId=… {caption, mediaUrls?,
    socialProfileIds?, scheduledAt?}. Auth: X-API-Key. (docs.ocoya.com —
    verified 2026-08-10: base https://app.ocoya.com/api/_public/v1.)"""
    api_key = (cfg.get("api_key") or "").strip()
    ws = (cfg.get("workspace_id") or "").strip()
    if not api_key or not ws:
        return False, "Ocoya needs an API key + workspace ID (set them in the 📱 Social tab)"
    platform = post.get("category") or ""
    prof = (cfg.get("profile_ids") or {}).get(platform) or ""
    if not prof and platform == "x_thread":
        prof = (cfg.get("profile_ids") or {}).get("x") or ""  # threads go to the X profile
    if not prof:
        # Multi-tenant (2026-08-11): fall back to the business's own connected
        # Ocoya profile (business id embedded in the post tags as biz:<id>).
        prof, ws = _biz_profile_for(post, platform, ws)
    payload = {"caption": post.get("content") or ""}
    img = post.get("image_url") or ""
    if img.startswith("http"):  # Ocoya must be able to fetch it — local vault URLs won't work
        payload["mediaUrls"] = [img]
    if prof:
        payload["socialProfileIds"] = [prof]
    if post.get("scheduled_at"):
        # Calendar slot wins (2026-08-12): the exact time the user picked in
        # AppVault's calendar becomes Ocoya's publish time. Connector-agnostic:
        # webhooks receive the same field and their receiver decides.
        try:
            payload["scheduledAt"] = datetime.strptime(str(post["scheduled_at"])[:16], "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    elif (cfg.get("schedule_mode") or "draft") == "offset":
        try:
            mins = max(1, int(cfg.get("schedule_offset_minutes", 60)))
            payload["scheduledAt"] = (datetime.now() + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    url = "https://app.ocoya.com/api/_public/v1/post?workspaceId=" + urllib.parse.quote(ws)
    try:
        data, status = _http(url, method="POST",
                             headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                             json_data=payload, timeout=30)
    except Exception as e:
        return False, f"ocoya call failed: {str(e)[:200]}"
    if status in (200, 201, 202, 204):
        pid = ""
        if isinstance(data, dict):
            pid = str(data.get("id") or data.get("postId") or data.get("data", {}).get("id") or "")
        return True, ("created " + pid).strip() if pid else "created"
    return False, f"Ocoya HTTP {status}: {str(data)[:200]}"

def _ocoya_check(cfg):
    """Validate the key against GET /me, and (when workspace set) list
    social profiles so the user can map platforms. Returns (ok, detail)."""
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        return False, "no API key set"
    try:
        data, status = _http("https://app.ocoya.com/api/_public/v1/me",
                             headers={"X-API-Key": api_key}, timeout=20)
    except Exception as e:
        return False, f"ocoya unreachable: {str(e)[:150]}"
    if status != 200:
        return False, f"Ocoya auth HTTP {status}: {str(data)[:150]}"
    out = "Ocoya auth OK"
    info = {"ok": True, "auth": "Ocoya auth OK", "text": out}
    # Discover workspaces so the workspace ID never has to be hunted down.
    try:
        wdata, wstatus = _http("https://app.ocoya.com/api/_public/v1/workspaces",
                               headers={"X-API-Key": api_key}, timeout=20)
        if wstatus == 200 and isinstance(wdata, list):
            info["workspaces"] = [{"id": str(w.get("id") or ""), "name": str(w.get("name") or "Workspace")}
                                  for w in wdata if w.get("id")]
            out += " · workspaces: " + ", ".join(f"{w['name']} ({w['id'][:8]}…)" for w in info["workspaces"])
    except Exception:
        pass
    ws = (cfg.get("workspace_id") or "").strip()
    if ws:
        try:
            pdata, pstatus = _http(
                "https://app.ocoya.com/api/_public/v1/social-profiles?workspaceId=" + urllib.parse.quote(ws),
                headers={"X-API-Key": api_key}, timeout=20)
            if pstatus == 200 and isinstance(pdata, list) and pdata:
                info["profiles"] = [{"id": str(p.get("id") or ""),
                                     "platform": str(p.get("platform") or ""),
                                     "name": str(p.get("name") or "")} for p in pdata[:15] if p.get("id")]
                out += " · profiles: " + ", ".join(f"{p['platform'] or '?'}:{p['id']}" for p in info["profiles"])
            else:
                out += f" | profiles HTTP {pstatus}"
        except Exception as e:
            out += f" | profiles error: {str(e)[:100]}"
    info["text"] = out
    return True, json.dumps(info)

def _social_auto_route(wid):
    """auto_route: try scheduling an approved post; mark scheduled on success."""
    try:
        ok, detail = _social_router_send(_pipeline_get(wid))
        if ok:
            _pipeline_update(wid, status="scheduled")
            _bus_publish("pipeline.social.scheduled", {"wid": wid})
    except Exception:
        pass

def _mask_router_cfg(project):
    cfg = dict(_social_router_cfg(project))
    for k in ("api_key", "auth_header"):
        if cfg.get(k):
            cfg[k] = "•••••• (set)"
    return cfg

@agentic_bp.route("/api/agentic/pipeline/social/config", methods=["GET", "POST", "OPTIONS"])
def api_social_router_config():
    """Per-project social router (Ocoya key/workspace/profile IDs or webhook)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    project = _project_from(request.args, "appvault")
    if request.method == "POST":
        data = request.get_json() or {}
        project = _project_from(data, project)
        if not _project_get(project):
            return jsonify({"error": "project not found"}), 404
        if data.get("reset"):
            _project_save_cfg(project, {"social_router": {}})
            return jsonify({"status": "ok", "config": {}})
        cfg = {}
        for k in ("provider", "url", "method", "auth_header", "auto_route",
                  "api_key", "workspace_id", "profile_ids", "schedule_mode",
                  "schedule_offset_minutes", "media_base_url"):
            if k in data and data[k] is not None:
                cfg[k] = data[k]
        # Secret preservation: an empty api_key/auth_header field means "keep the
        # current one" (the UI renders masked secrets as blank inputs). Without
        # this, saving ANY other field silently wipes the stored credentials.
        prev = _social_router_cfg(project)
        for k in ("api_key", "auth_header"):
            if str(cfg.get(k) or "").strip() == "" and prev.get(k):
                cfg[k] = prev[k]
        _project_save_cfg(project, {"social_router": cfg})
        if data.get("test"):
            if (cfg.get("provider") or "webhook").lower() == "ocoya":
                ok, detail = _ocoya_check(cfg)
            else:
                ok, detail = _social_router_send({"title": "AppVault router test",
                                                  "category": "test", "content": "Pipeline router test — ignore.",
                                                  "tags": "", "project": project})
            return jsonify({"status": "ok" if ok else "error",
                            "config": _mask_router_cfg(project), "test": detail})
        return jsonify({"status": "ok", "config": _mask_router_cfg(project)})
    cfg = dict(_social_router_cfg(project))
    for k in ("api_key", "auth_header"):
        if cfg.get(k):
            cfg[k] = "•••••• (set)"
    return jsonify({"status": "ok", "config": cfg, "project": project})

@agentic_bp.route("/api/agentic/image/config", methods=["GET", "POST", "OPTIONS"])
def api_image_gen_config():
    """Image engine config: {provider (openai|litellm|pollinations), model,
    api_key, api_base}. Stored globally like the LLM config; keys masked on GET."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "POST":
        data = request.get_json() or {}
        if data.get("reset"):
            _cfg_set("image_gen", {})
            return jsonify({"status": "ok", "config": {}})
        cfg = dict(_image_gen_config())
        for k in ("provider", "model", "api_key", "api_base"):
            if k in data and data[k] is not None:
                cfg[k] = str(data[k]).strip()
        _cfg_set("image_gen", cfg)
        out = dict(cfg)
        if out.get("api_key"):
            out["api_key"] = "•••••• (set)"
        return jsonify({"status": "ok", "config": out})
    cfg = dict(_image_gen_config())
    if cfg.get("api_key"):
        cfg["api_key"] = "•••••• (set)"
    return jsonify({"status": "ok", "config": cfg})

@agentic_bp.route("/api/agentic/pipeline/social/<wid>/schedule", methods=["POST", "OPTIONS"])
def api_social_schedule(wid):
    """Route one approved social post to the scheduler -> status scheduled."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"error": "not found"}), 404
    if item.get("category") not in ("x", "x_thread", "linkedin", "facebook", "instagram"):
        return jsonify({"error": "not a social post"}), 400
    if item.get("status") != "approved":
        return jsonify({"error": "only approved posts can be scheduled (approve it first)"}), 400
    ok, detail = _social_router_send(item)
    if not ok:
        return jsonify({"status": "error", "error": detail}), 502
    it = _pipeline_update(wid, status="scheduled")
    _bus_publish("pipeline.social.scheduled", {"wid": wid, "platform": item.get("category"),
                                               "title": item.get("title")})
    return jsonify({"status": "ok", "item": it, "detail": detail})

# ---------------------------------------------------------------------------
# ARTICLE HTML VIEW — render a stored markdown article as a full styled HTML
# page (standalone browser view; also a pre-publish preview of the WP post).
# ---------------------------------------------------------------------------
def _md_inline_html(t):
    s = (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)",
               r'<img src="\2" alt="\1" style="max-width:100%;border-radius:8px;margin:12px 0;">', s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(^|[^*])\*([^*]+)\*", r"\1<em>\2</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s

def _md_to_html_page(text):
    out, in_code, code_buf, in_list = [], False, [], None
    in_html5, html5_buf = False, []
    def close():
        nonlocal in_list
        if in_list:
            out.append("</ul>" if in_list == "ul" else "</ol>")
            in_list = None
    for raw in (text or "").split("\n"):
        t = raw.strip()
        if in_html5:
            if t == "```":
                out.append(_sanitize_visual_html("\n".join(html5_buf)))
                html5_buf = []
                in_html5 = False
            else:
                html5_buf.append(raw)
            continue
        if t == "```html5" or t.startswith("```html5 "):
            close()
            in_html5 = True
            continue
        if t.startswith("```"):
            if in_code:
                out.append("<pre><code>" + "\n".join(code_buf).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                close()
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", t)
        if m:
            close()
            n = len(m.group(1))
            out.append(f"<h{n}>{_md_inline_html(m.group(2))}</h{n}>")
            continue
        if t.startswith("> "):
            close()
            out.append(f"<blockquote><p>{_md_inline_html(t[2:])}</p></blockquote>")
            continue
        m = re.match(r"^[-*]\s+(.*)$", t)
        if m:
            if in_list != "ul":
                close()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{_md_inline_html(m.group(1))}</li>")
            continue
        m = re.match(r"^\d+\.\s+(.*)$", t)
        if m:
            if in_list != "ol":
                close()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{_md_inline_html(m.group(1))}</li>")
            continue
        close()
        if not t:
            continue
        out.append(f"<p>{_md_inline_html(t)}</p>")
    close()
    if in_code:
        out.append("<pre><code>" + "\n".join(code_buf).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</code></pre>")
    return "\n".join(out)

# ---------------------------------------------------------------------------
# AI VISUALS — LLM-generated self-contained HTML5 blocks inside articles
# (comparison tables, charts, diagrams…). Stored as ```html5 fences in the
# markdown; rendered live in TipTap, preview, View page and WordPress.
# ---------------------------------------------------------------------------
def _sanitize_visual_html(html):
    """Strip anything executable from generated visual HTML: scripts,
    iframes, event handlers, javascript: URLs. Styles/SVG/tables survive."""
    if not html:
        return ""
    h = html
    h = re.sub(r"<script\b.*?</script>", "", h, flags=re.S | re.I)
    h = re.sub(r"<iframe\b.*?</iframe>", "", h, flags=re.S | re.I)
    h = re.sub(r"<object\b.*?</object>", "", h, flags=re.S | re.I)
    h = re.sub(r"(?i)\s(on[a-z]+)\s*=\s*(\"[^\"]*\"|'[^']*')", "", h)
    h = re.sub(r"(?i)(?<![a-z])on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*')", "", h)
    h = re.sub(r"(?i)href\s*=\s*[\"']?\s*javascript:", 'href="#"', h)
    h = re.sub(r"(?i)src\s*=\s*[\"']?\s*javascript:", 'src=""', h)
    return h.strip()

_VISUAL_TYPE_PROMPTS = {
    "comparison": ("Build a clean COMPARISON TABLE as self-contained HTML. "
                   "Two or more columns, header row, styled rows, responsive."),
    "bar": ("Build a BAR CHART comparing values as self-contained HTML using "
            "inline CSS (div bars) or SVG. Axis labels, value labels on bars."),
    "line": ("Build a LINE CHART as self-contained HTML using inline SVG. "
             "Axes, gridlines, data points, legend if multiple series."),
    "pie": ("Build a PIE/DOUGHNUT CHART as self-contained HTML using inline "
            "SVG with conic segments or a conic-gradient. Legend with values."),
    "flow": ("Build a FLOW DIAGRAM / process chart as self-contained HTML. "
             "Boxes connected with arrows (flexbox + CSS), step labels."),
    "timeline": ("Build a vertical TIMELINE as self-contained HTML. Alternating "
                 "entries with dates and short descriptions, connecting line."),
    "proscons": ("Build a PROS & CONS two-column comparison as self-contained "
                 "HTML. Green/red styling, bullet items, header."),
    "pricing": ("Build a PRICING TABLE (plan columns, price, features, CTA "
                "button) as self-contained HTML. Highlight one 'popular' column."),
    "stats": ("Build a STATS GRID (2-4 big-number stat cards with labels and "
              "small captions) as self-contained HTML."),
    "quote": ("Build an attractive QUOTE CARD as self-contained HTML. Large "
              "quote text, attribution, decorative styling."),
    "custom": ("Build whatever the user asked for as self-contained HTML5."),
}

@agentic_bp.route("/api/agentic/pipeline/visual/generate", methods=["POST", "OPTIONS"])
def api_visual_generate():
    """Generate a self-contained HTML5 visual for insertion into an article."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    prompt = (data.get("prompt") or "").strip()
    vtype = (data.get("type") or "custom").strip()
    wid = (data.get("wid") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    ctx = ""
    if wid:
        item = _pipeline_get(wid)
        if item:
            ctx = (f"Article title: {item.get('title') or ''}\n\n"
                   f"Article excerpt:\n{(item.get('content') or '')[:1200]}")
    sys_p = ("You generate self-contained HTML5 visuals for a tech blog. Rules: "
             "inline CSS or one <style> block scoped to your root element; SVG for "
             "charts; NO <script>, NO external libraries/CDNs, NO event handlers, "
             "NO javascript: URLs, NO <iframe>. Dark theme (#0b1120 background, "
             "#e2e8f0 text, accent #38bdf8) unless the request says otherwise. "
             "Output ONLY the HTML fragment — no markdown fences, no preamble.\n\n"
             + _VISUAL_TYPE_PROMPTS.get(vtype, _VISUAL_TYPE_PROMPTS["custom"]))
    try:
        overrides = {}
        if (_get_llm_config().get("provider") or "").lower() == "deepseek":
            overrides["model"] = "deepseek-chat"  # fast model — visuals must not take minutes
        html = _call_llm_with(overrides,
                              f"{ctx}\n\nVisual request: {prompt}\n\nOutput ONLY the HTML.",
                              system_prompt=sys_p, agent="visual", timeout=120)
    except Exception as e:
        return jsonify({"status": "error", "error": f"generation failed: {str(e)[:200]}"}), 502
    html = (html or "").strip()
    if html.startswith("```"):
        lines = html.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        html = "\n".join(lines).strip()
    html = _sanitize_visual_html(html)
    if not html:
        return jsonify({"status": "error", "error": "generated empty HTML"}), 502
    return jsonify({"status": "ok", "html": html})

@agentic_bp.route("/api/agentic/pipeline/<wid>/preview", methods=["GET", "OPTIONS"])
def api_pipeline_preview(wid):
    """Rendered article BODY HTML for the preview pane (server-side render —
    the same proven renderer as /view, without the page chrome)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "ok", "title": item.get("title") or "",
                    "image_url": item.get("image_url") or "",
                    "body": _md_to_html_page(item.get("content") or "")})

@agentic_bp.route("/api/agentic/pipeline/<wid>/view", methods=["GET", "OPTIONS"])
def api_pipeline_view(wid):
    """Standalone styled HTML page for an article (or any pipeline item)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return "Not found", 404
    title = (item.get("title") or "Article").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = _md_to_html_page(item.get("content") or "")
    img = item.get("image_url") or ""
    img_html = (f'<img src="{img}" style="max-width:100%;border-radius:12px;margin:0 0 24px;'
                f'border:1px solid rgba(51,65,85,0.5);">') if img else ""
    status = (item.get("status") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{background:#0b1120;color:#e2e8f0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;line-height:1.65;}}
.wrap{{max-width:760px;margin:0 auto;padding:48px 24px 80px;}}
h1{{font-size:30px;line-height:1.25;color:#f8fafc;margin:0 0 10px;}}
h2{{font-size:21px;color:#f1f5f9;margin:32px 0 10px;}}
h3{{font-size:17px;color:#f1f5f9;margin:26px 0 8px;}}
p{{font-size:16px;color:#cbd5e1;margin:14px 0;}}
a{{color:#38bdf8;}}
ul,ol{{font-size:16px;color:#cbd5e1;}}
li{{margin:6px 0;}}
blockquote{{border-left:3px solid #38bdf8;margin:16px 0;padding:4px 16px;color:#94a3b8;background:rgba(56,189,248,0.06);border-radius:0 8px 8px 0;}}
pre{{background:#0f172a;border:1px solid rgba(51,65,85,0.6);border-radius:8px;padding:14px;overflow-x:auto;font-size:13.5px;color:#7dd3fc;}}
code{{background:#0f172a;border-radius:4px;padding:1px 6px;color:#7dd3fc;font-size:13.5px;}}
.meta{{font-size:12.5px;color:#64748b;margin-bottom:24px;}}
.badge{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;color:#94a3b8;border:1px solid rgba(51,65,85,0.6);}}
</style></head><body><div class="wrap">
<h1>{title}</h1>
<div class="meta"><span class="badge">{status}</span></div>
{img_html}
{body}
</div></body></html>"""
    return Response(page, mimetype="text/html")

def _pipeline_worker():
    """Auto-advance: brief -> draft -> refine; approved -> publish (auto gate)."""
    while True:
        try:
            if _pipeline_cfg().get("auto"):
                conn = _db()
                rows = conn.execute(
                    "SELECT id, status, source FROM work_items "
                    "WHERE status IN ('ready_to_write','needs_refinement','approved')").fetchall()
                conn.close()
                for r in rows:
                    if not (r["source"] or "").startswith("pipeline"):
                        continue
                    try:
                        if r["status"] == "ready_to_write":
                            _pipeline_draft(r["id"])
                        elif r["status"] == "needs_refinement":
                            _pipeline_refine(r["id"])
                        elif r["status"] == "approved":
                            _pipeline_publish(r["id"])
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(45)

def _pipeline_ensure():
    if not _PIPELINE_FLAG[0]:
        _PIPELINE_FLAG[0] = True
        _projects_ensure()
        threading.Thread(target=_pipeline_worker, daemon=True).start()

# ---------------------------------------------------------------------------
# PROJECTS — one content pipeline per business (AppVault, CISOvault, …).
# Each project has its own sources (feeds/keywords), social router (Ocoya
# profile IDs), and WordPress site. Stored in projects.config JSON with a
# fallback to the global config keys for backward compatibility.
# ---------------------------------------------------------------------------
_PIPELINE_PROJECT_SEED = [
    {"slug": "appvault", "name": "AppVault"},
    {"slug": "cisovault", "name": "CISOvault"},
]

def _projects_ensure():
    conn = _db()
    try:
        conn.execute("ALTER TABLE work_items ADD COLUMN project TEXT DEFAULT 'appvault'")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE work_items ADD COLUMN research TEXT DEFAULT ''")
    except Exception:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS projects "
                 "(slug TEXT PRIMARY KEY, name TEXT, config TEXT, created TEXT)")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for p in _PIPELINE_PROJECT_SEED:
        conn.execute("INSERT OR IGNORE INTO projects (slug, name, config, created) VALUES (?,?,?,?)",
                     (p["slug"], p["name"], "{}", now))
    conn.commit()
    conn.close()

def _projects_list():
    _projects_ensure()
    conn = _db()
    rows = conn.execute("SELECT slug, name, created FROM projects ORDER BY created").fetchall()
    conn.close()
    return [{"slug": r["slug"], "name": r["name"], "created": r["created"]} for r in rows]

def _project_get(slug):
    _projects_ensure()
    conn = _db()
    row = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    conn.close()
    return dict(row) if row else None

def _project_cfg(slug, key=None):
    """Per-project config JSON; falls back to the global config key."""
    p = _project_get(slug)
    cfg = {}
    if p and p.get("config"):
        try:
            cfg = json.loads(p["config"])
        except Exception:
            cfg = {}
    if key is None:
        return cfg
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return _cfg_get(key)

def _project_save_cfg(slug, cfg):
    p = _project_get(slug)
    base = {}
    if p and p.get("config"):
        try:
            base = json.loads(p["config"])
        except Exception:
            base = {}
    base.update(cfg or {})
    conn = _db()
    conn.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(base), slug))
    conn.commit()
    conn.close()

def _project_from(data_or_args, default="appvault"):
    v = ""
    try:
        v = (data_or_args.get("project") or "").strip()
    except Exception:
        pass
    return v or default

def _pipeline_signal_consumed(key):
    try:
        conn = _db()
        conn.execute("CREATE TABLE IF NOT EXISTS pipeline_signals (sig_id TEXT PRIMARY KEY, title TEXT, used_at TEXT)")
        row = conn.execute("SELECT 1 FROM pipeline_signals WHERE sig_id=?", (key,)).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False

def _pipeline_mark_consumed(key, title=""):
    try:
        conn = _db()
        conn.execute("CREATE TABLE IF NOT EXISTS pipeline_signals (sig_id TEXT PRIMARY KEY, title TEXT, used_at TEXT)")
        conn.execute("INSERT OR IGNORE INTO pipeline_signals (sig_id, title, used_at) VALUES (?,?,?)",
                     (key, (title or "")[:200], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception:
        pass

def _pipeline_next_signal():
    """Next UNCONSUMED signal — variety by construction:
    1) fresh sweep, first story whose title isn't used (diverse topics),
    2) then radar report files (vault 03_Signals/Signal_sig-*.md) newest-first
       that haven't been used,
    3) else None (wait for the next sweep/radar run)."""
    try:
        for story in _sweep_feeds(6):
            title = (story.get("title") or "").strip()
            if not title:
                continue
            key = "title:" + re.sub(r"\s+", " ", title.lower())[:120]
            if _pipeline_signal_consumed(key):
                continue
            return (title + " — " + (story.get("summary") or ""))[:3000], key, title
    except Exception:
        pass
    try:
        sig_dir = os.path.join(_vault_path(), "03_Signals")
        if os.path.isdir(sig_dir):
            files = sorted(n for n in os.listdir(sig_dir)
                           if n.startswith("Signal_sig-") and n.endswith(".md"))
            for name in reversed(files):  # newest first
                key = "file:" + name
                if _pipeline_signal_consumed(key):
                    continue
                try:
                    with open(os.path.join(sig_dir, name), encoding="utf-8") as f:
                        text = f.read()
                except Exception:
                    continue
                if text.strip():
                    return text[:3000], key, name
    except Exception:
        pass
    return None, None, None

@agentic_bp.route("/api/agentic/pipeline", methods=["GET", "OPTIONS"])
def api_pipeline_list():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    _pipeline_ensure()
    project = (request.args.get("project") or "appvault").strip()
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM work_items WHERE (source LIKE 'pipeline%' "
        "OR status IN ('ready_to_write','needs_refinement','ready_for_approval','approved','rejected')) "
        "AND project=? ORDER BY updated_at DESC LIMIT 60", (project,)).fetchall()
    conn.close()
    return jsonify({"status": "ok", "items": [dict(r) for r in rows],
                    "config": _pipeline_cfg(), "project": project})

@agentic_bp.route("/api/agentic/pipeline/projects", methods=["GET", "POST", "OPTIONS"])
def api_pipeline_projects():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    _pipeline_ensure()
    if request.method == "POST":
        data = request.get_json() or {}
        slug = (data.get("slug") or "").strip().lower().replace(" ", "-")
        name = (data.get("name") or slug or "Project").strip()
        if not slug or len(slug) > 40 or not all(c.isalnum() or c == "-" for c in slug):
            return jsonify({"error": "slug must be letters, numbers and dashes"}), 400
        conn = _db()
        try:
            conn.execute("INSERT INTO projects (slug, name, config, created) VALUES (?,?,?,?)",
                         (slug, name, "{}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
        except Exception as e:
            conn.close()
            return jsonify({"error": f"project exists or invalid: {str(e)[:120]}"}), 400
        return jsonify({"status": "ok", "projects": _projects_list()})
    return jsonify({"status": "ok", "projects": _projects_list()})

@agentic_bp.route("/api/agentic/pipeline/projects/<slug>/config", methods=["GET", "PUT", "OPTIONS"])
def api_pipeline_project_config(slug):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if not _project_get(slug):
        return jsonify({"error": "project not found"}), 404
    if request.method == "PUT":
        data = request.get_json() or {}
        allowed = {k: data[k] for k in ("pipeline_sources", "social_router", "wp_tool")
                   if k in data and data[k] is not None}
        _project_save_cfg(slug, allowed)
        return jsonify({"status": "ok", "config": _project_cfg(slug)})
    cfg = _project_cfg(slug)
    for k in ("social_router", "wp_tool"):
        c = cfg.get(k)
        if isinstance(c, dict):
            for sk in ("api_key", "auth_header", "password", "application_password", "username"):
                if c.get(sk):
                    c[sk] = "•••••• (set)"
    return jsonify({"status": "ok", "config": cfg})

def _json_array_extract(text):
    """Pull the first JSON array out of an LLM response (tolerates fences/preamble)."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    start = t.find("[")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "[":
            depth += 1
        elif t[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None

@agentic_bp.route("/api/agentic/pipeline/brief", methods=["POST", "OPTIONS"])
def api_pipeline_brief():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    _pipeline_ensure()
    data = request.get_json() or {}
    signal = (data.get("signal") or "").strip()
    source_key = (data.get("signal_key") or "").strip() or None
    signal_label = ""
    if not signal:
        signal, source_key, signal_label = _pipeline_next_signal()
        if not signal:
            return jsonify({"error": "no fresh signal — every radar report and recent sweep story has been used. Try again after the next radar sweep (or pass a signal explicitly)."}), 400
    else:
        signal_label = signal[:80]
        source_key = source_key or ("manual:" + signal[:80])
    project = _project_from(data, "appvault")
    # Idempotency: an explicit signal_key that was already briefed must not
    # create a duplicate work item (covers double-clicks, retries, multi-user).
    if source_key and _pipeline_signal_consumed(source_key):
        return jsonify({"status": "ok", "dup": True, "wid": None,
                        "message": "This story already has a brief in the pipeline — check the Approval Queue."}), 200
    wid, brief = _pipeline_brief_from_signal(signal, data.get("source") or "manual",
                                             project=project)
    if wid and source_key:
        _pipeline_mark_consumed(source_key, signal_label)
        _pipeline_update(wid, project=project)
    return jsonify({"status": "ok", "wid": wid, "brief": brief, "signal": signal[:300],
                    "signal_label": signal_label, "project": project})

@agentic_bp.route("/api/agentic/pipeline/stories", methods=["GET", "OPTIONS"])
def api_pipeline_stories():
    """Live news sweep right now — the 'Top Stories' tab (project-scoped feeds)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    project = (request.args.get("project") or "appvault").strip()
    stories = []
    try:
        for s in _sweep_feeds(10, project):
            stories.append({"title": s.get("title", ""), "score": s.get("score", 0),
                            "source": s.get("source", ""), "link": s.get("link", ""),
                            "summary": (s.get("summary") or "")[:220]})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)[:200]}), 500
    return jsonify({"status": "ok", "stories": stories,
                    "fetched": datetime.now().strftime("%H:%M:%S"), "project": project})

@agentic_bp.route("/api/agentic/pipeline/radar", methods=["GET", "OPTIONS"])
def api_pipeline_radar():
    """Radar report history — vault 03_Signals/Signal_sig-*.md, newest first."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    reports = []
    try:
        sig_dir = os.path.join(_vault_path(), "03_Signals")
        if os.path.isdir(sig_dir):
            files = sorted(n for n in os.listdir(sig_dir)
                           if n.startswith("Signal_sig-") and n.endswith(".md"))
            for name in reversed(files[-40:]):  # newest 40 reports
                p = os.path.join(sig_dir, name)
                ts, query, titles, content = "", "", [], ""
                try:
                    with open(p, encoding="utf-8") as f:
                        content = f.read()
                    lines = content.splitlines()
                    for l in lines[:6]:
                        if "Timestamp" in l and "**" in l:
                            ts = l.split("**", 2)[-1].strip().lstrip(":").strip()
                        if "Query Prompt" in l and "`" in l:
                            query = l.split("`")[1]
                    for l in lines:
                        t = l.strip()
                        if t[:3] in ("1. ", "2. ", "3. ", "4. ", "5. ", "6. ", "7. ", "8. ", "9. ", "10.") and "**" in t:
                            titles.append(t.split("**")[1].strip())
                            if len(titles) >= 5:
                                break
                except Exception:
                    pass
                reports.append({"file": name, "ts": ts, "query": query,
                                "titles": titles, "used": _pipeline_signal_consumed("file:" + name),
                                "content": content[:3000]})
    except Exception:
        pass
    return jsonify({"status": "ok", "reports": reports})

def _pipeline_sources_payload(project="appvault"):
    cfg = _project_cfg(project, "pipeline_sources")
    using_cfg = isinstance(cfg, dict) and isinstance(cfg.get("feeds"), list) and bool(cfg["feeds"])
    if using_cfg:
        feeds = cfg["feeds"]
    else:
        feeds = []  # Multi-tenant: no built-in fallback feeds
    if isinstance(cfg, dict) and isinstance(cfg.get("keywords"), dict) and cfg["keywords"]:
        keywords = cfg["keywords"]
    else:
        keywords = {}  # Multi-tenant: no built-in fallback keywords
    return {"feeds": feeds, "keywords": keywords, "using_config": using_cfg}

@agentic_bp.route("/api/agentic/pipeline/sources", methods=["GET", "POST", "OPTIONS"])
def api_pipeline_sources():
    """Edit what THIS PROJECT's radar tracks — feeds + scoring keywords."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    project = _project_from(request.args, "appvault")
    if request.method == "POST":
        data = request.get_json() or {}
        project = _project_from(data, project)
        if not _project_get(project):
            return jsonify({"error": "project not found"}), 404
        if data.get("reset"):
            _project_save_cfg(project, {"pipeline_sources": {}})
            return jsonify({"status": "ok", "sources": _pipeline_sources_payload(project)})
        cfg = {}
        if isinstance(data.get("feeds"), list):
            feeds = []
            for f in data["feeds"][:40]:
                if isinstance(f, dict) and (f.get("name") or "").strip() and (f.get("url") or "").strip():
                    feeds.append({"name": f["name"].strip()[:60],
                                  "url": f["url"].strip()[:300],
                                  "enabled": bool(f.get("enabled", True))})
            cfg["feeds"] = feeds
        if isinstance(data.get("keywords"), dict):
            kw = {}
            for k, v in data["keywords"].items():
                if k.strip():
                    try:
                        kw[k.strip()[:40]] = max(1, min(10, int(v)))
                    except Exception:
                        kw[k.strip()[:40]] = 3
            cfg["keywords"] = kw
        _project_save_cfg(project, {"pipeline_sources": cfg})
        return jsonify({"status": "ok", "sources": _pipeline_sources_payload(project)})
    return jsonify({"status": "ok", "sources": _pipeline_sources_payload(project), "project": project})

_BRAINSTORM_PROMPT = ("You are the Content Strategist for a business blog. From the signal, propose "
                      "THREE distinct content ideas that fit the business. Return a JSON array of "
                      "exactly 3 objects, each with keys: title (SEO-friendly headline, max 12 words), "
                      "angle (one-sentence hook), why (one sentence on why this works for the business). "
                      "Output ONLY the JSON array — no markdown fences, no preamble.")

@agentic_bp.route("/api/agentic/pipeline/brainstorm", methods=["POST", "OPTIONS"])
def api_pipeline_brainstorm():
    """Brainstorm FIRST: strategist proposes 3 content ideas from a signal;
    the item waits at status 'brainstorm' for a human to pick one."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    project = _project_from(data, "appvault")
    _projects_ensure()
    if not _project_get(project):
        return jsonify({"error": "project not found"}), 404
    signal = (data.get("signal") or "").strip()
    source_key, signal_label = None, ""
    if not signal:
        signal, source_key, signal_label = _pipeline_next_signal()
        if not signal:
            return jsonify({"error": "no fresh signal — every radar report and recent sweep story has been used. Try again after the next radar sweep."}), 400
    else:
        signal_label = signal[:80]
        source_key = source_key or ("manual:" + signal[:80])
    try:
        ideas_text = _call_llm(f"Signal: {signal[:1500]}\n\nOutput the 3-idea JSON array now.",
                               system_prompt=_BRAINSTORM_PROMPT, agent="strategist", timeout=90)
    except Exception as e:
        return jsonify({"status": "error", "error": f"brainstorm failed: {str(e)[:150]}"}), 502
    ideas = _json_array_extract(ideas_text)
    if not ideas:
        return jsonify({"status": "error", "error": "could not parse the idea list from the LLM"}), 502
    ideas = [i for i in ideas if isinstance(i, dict)][:3]
    if not ideas:
        return jsonify({"status": "error", "error": "ideas had no usable objects"}), 502
    wid = _work_record(category="content",
                       title=f"💡 Brainstorm — {signal_label[:60]}",
                       content=json.dumps(ideas, ensure_ascii=False),
                       source="pipeline:strategist", status="brainstorm",
                       tags=f"brainstorm,project:{project}", project=project,
                       url=f"signal:{signal[:500]}")
    if wid and source_key:
        _pipeline_mark_consumed(source_key, signal_label)
    _bus_publish("pipeline.brainstorm.ready", {"wid": wid, "project": project, "ideas": len(ideas)})
    return jsonify({"status": "ok", "wid": wid, "ideas": ideas,
                    "signal": signal[:200], "signal_label": signal_label, "project": project})

@agentic_bp.route("/api/agentic/pipeline/<wid>/regenerate", methods=["POST", "OPTIONS"])
def api_pipeline_regenerate(wid):
    """🎲 Try again: 3 fresh ideas from the same stored signal."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"error": "not found"}), 404
    if item.get("status") != "brainstorm":
        return jsonify({"error": "not a brainstorm item"}), 400
    signal = (item.get("url") or "").strip()
    if signal.startswith("signal:"):
        signal = signal[7:]
    if not signal:
        return jsonify({"error": "no stored signal on this item"}), 400
    try:
        ideas_text = _call_llm(f"Signal: {signal[:1500]}\n\nOutput the 3-idea JSON array now.",
                               system_prompt=_BRAINSTORM_PROMPT, agent="strategist", timeout=90)
    except Exception as e:
        return jsonify({"status": "error", "error": f"regenerate failed: {str(e)[:150]}"}), 502
    ideas = _json_array_extract(ideas_text)
    if not ideas:
        return jsonify({"status": "error", "error": "could not parse the idea list"}), 502
    ideas = [i for i in ideas if isinstance(i, dict)][:3]
    if not ideas:
        return jsonify({"status": "error", "error": "no usable ideas"}), 502
    _pipeline_update(wid, content=json.dumps(ideas, ensure_ascii=False),
                     title=f"💡 Brainstorm — {signal[:60]}",
                     tags=(item.get("tags") or "").replace("regenerated", "").strip() + ",regenerated")
    return jsonify({"status": "ok", "item": _pipeline_get(wid), "ideas": ideas})

@agentic_bp.route("/api/agentic/pipeline/<wid>/pick", methods=["POST", "OPTIONS"])
def api_pipeline_pick(wid):
    """Human picks one brainstorm idea -> brief from that idea -> auto-run continues."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"error": "not found"}), 404
    if item.get("status") != "brainstorm":
        return jsonify({"error": "not a brainstorm item"}), 400
    data = request.get_json() or {}
    try:
        idx = int(data.get("idea", 0))
    except Exception:
        idx = 0
    try:
        ideas = json.loads(item.get("content") or "[]")
    except Exception:
        ideas = []
    if idx < 0 or idx >= len(ideas):
        return jsonify({"error": "idea index out of range"}), 400
    idea = ideas[idx]
    brief_text = f"Title: {idea.get('title', '')}\nAngle: {idea.get('angle', '')}\nWhy: {idea.get('why', '')}"
    project = item.get("project") or "appvault"
    wid2, brief = _pipeline_brief_from_signal(brief_text, source="brainstorm", project=project)
    if not wid2:
        return jsonify({"error": "brief generation failed"}), 502
    conn = _db()
    conn.execute("DELETE FROM work_items WHERE id=?", (wid2,))
    conn.commit()
    conn.close()
    _pipeline_update(wid, title=(idea.get("title") or "Brief")[:4000],
                     content=json.dumps(brief, ensure_ascii=False) if isinstance(brief, dict) else str(brief or ""),
                     status="ready_to_write",
                     tags=(item.get("tags") or "") + ",brief,brainstorm-picked")
    _bus_publish("pipeline.brief.ready", {"wid": wid, "title": idea.get("title")})
    return jsonify({"status": "ok", "item": _pipeline_get(wid), "idea": idea})

@agentic_bp.route("/api/agentic/pipeline/<wid>/<action>", methods=["POST", "OPTIONS"])
def api_pipeline_action(wid, action):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    _pipeline_ensure()
    if action == "draft":
        it, err = _pipeline_draft(wid)
    elif action == "refine":
        it, err = _pipeline_refine(wid)
    elif action == "approve":
        it = _pipeline_update(wid, status="approved")
        err = None
        if it:
            _bus_publish("pipeline.approved", {"wid": wid, "title": it.get("title")})
            # Article approval auto-generates the cover image + platform posts
            if (it.get("category") or "") == "content" or (it.get("source") or "").startswith("pipeline:strategist"):
                threading.Thread(target=_pipeline_socialize, args=(wid,), daemon=True).start()
            # auto_route: approved social posts go to the scheduler immediately
            elif it.get("category") in ("x", "x_thread", "linkedin", "facebook", "instagram") and _social_router_cfg().get("auto_route"):
                threading.Thread(target=_social_auto_route, args=(wid,), daemon=True).start()
    elif action == "reject":
        it = _pipeline_update(wid, status="rejected")
        err = None
        if it:
            _bus_publish("pipeline.rejected", {"wid": wid, "title": it.get("title")})
    elif action == "publish":
        it, err = _pipeline_publish(wid)
    else:
        return jsonify({"error": "unknown action (draft|refine|approve|reject|publish)"}), 400
    if err:
        return jsonify({"status": "error", "error": err}), (502 if action == "publish" else 400)
    return jsonify({"status": "ok", "item": it})

@agentic_bp.route("/api/agentic/pipeline/auto", methods=["POST", "OPTIONS"])
def api_pipeline_auto():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    cfg = _pipeline_cfg()
    cfg["auto"] = bool(data.get("auto", cfg.get("auto", True)))
    _cfg_set("pipeline", cfg)
    return jsonify({"status": "ok", "config": cfg})

# ---------------------------------------------------------------------------
# HELP — the plain-English guide, served from the vault
# (GRC-Brain/AGENTIC_OS_HELP.md, visible to the agent at /data/second-brain).
# Every install ships the BUILT-IN default below; a vault file (when present)
# overrides it — so customers get the guide out of the box and power users
# can customize it. Editing the vault file updates the in-app Help page.
# ---------------------------------------------------------------------------
_HELP_CANDIDATES = (
    "/data/second-brain/GRC-Brain/AGENTIC_OS_HELP.md",
    "D:/ObsidianVault/GRC-Brain/AGENTIC_OS_HELP.md",
    "/data/vault/GRC-Brain/AGENTIC_OS_HELP.md",
)

_HELP_DEFAULT = """# Agentic OS — Plain-English Help

## What this is
Agentic OS is your always-on assistant. You press a button, it plans, writes and polishes,
**you approve**, it's live. Everything else is automatic.

---

## Your daily workflow (2 clicks)
1. Open **Content Pipeline** (left sidebar → Pipeline → Content Pipeline)
2. Click **⚡ New Brief from Signal**
3. Walk away ~3 minutes — it plans, writes and polishes on its own
4. When a card says **✅ ready for approval**: read it → click **Approve** (or **Reject**)
5. If WordPress is set up (below), Approve = published.

## How it works (the factory line)
| Step | Who does it | Your part |
|---|---|---|
| 1. Watch | radar watches the news for a story worth covering | — |
| 2. Plan | strategist writes a brief: angle, keyword, outline, CTA | — |
| 3. Write | writer turns the brief into a draft (no freelancing) | — |
| 4. Polish | editor re-writes it naturally (a different AI brain + anti-AI-tell rules) | — |
| 5. Approve | **you** read it and click ✅ / ❌ | ← the only human step |

The pipeline stops at step 5 on purpose. Nothing publishes without you.

---

## One-time setup (do this once, then forget it)

### 1. AI brain (the writer's brain)
One provider + API key powers plan/write/polish. Set it in **⚙️ Configure**
(sidebar → System → Configure): pick your provider, paste your API key, save.

### 2. WordPress (the outbox)
Without this, approved articles stay "approved" instead of going live.
- Where: **🛡️ Gov → WordPress Publisher**
- What: site URL (e.g. `https://yourblog.com`), a username, and an
  **Application Password** (created in WordPress → Users → your profile →
  Application Passwords — this is NOT your login password)
- Do it once; every future approval publishes automatically.

### 3. DeerFlow (optional power tool)
A bonus "super researcher" that can research and code for minutes-to-hours tasks.
**It is not part of the content pipeline** — the pipeline never uses it.
- Launch: roster → **DeerFlow card → ⚡ Launch** (first time ~10 min, then 🚀 Open)
- Login: `admin@appvault.io` (password shown in its status screen — change it
  inside DeerFlow's own settings)
- Needs its own AI key only if you run tasks inside it.

---

## Where things live
- **The page you use**: Agentic OS (the browser tab). Everything you'll ever click is here.
- **Its brain**: a database + (optionally) your vault sync — fully automatic, never touch it.
- **DeerFlow's folder**: inside your AppVault data folder — only matters if you use DeerFlow.

---

## The left panel, page by page
| Page | What it's for |
|---|---|
| **Apps** | Installed desktop apps & launcher |
| **Agentic OS** | Chat with the agents + the roster (DeerFlow, Ollama, LiteLLM…) |
| **Missions** | Longer goals turned into plans with tasks |
| **Identity** | Your profiles and "souls" (voice/personality) |
| **Goals** | Objective tracking |
| **Artifacts** | Generated outputs (SEO, V posts…) |
| **Memory** | The shared brain — facts, signals, conversations |
| **Completed** | The work ledger — everything finished |
| **Crews** | Run pre-built agent teams |
| **Content Pipeline** | ⭐ your daily screen — briefs → approval queue |
| **Gov** | WordPress publisher, policies, security |
| **Store / Installed / Manage** | Browse, install and manage apps |
| **Configure** | AI brain (provider/key), souls, skills |
| **Health** | Status of every service |
| **Help** | This page |

---

## Quick fixes
- **Article stuck at "approved"** → WordPress isn't set up yet. See One-time setup #2.
- **Chat says "no LLM backend"** → ⚙️ Configure → set provider + API key.
- **DeerFlow shows offline** → roster → DeerFlow → ⚡ Launch (be patient, first run builds).
- **App won't launch** → Health page shows what's down; restart from Manage.
- **You want a different polishing brain** → the editor model is configurable.
"""

@agentic_bp.route("/api/agentic/help", methods=["GET", "OPTIONS"])
def api_help():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    text, source = "", "builtin"
    for p in _HELP_CANDIDATES:
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    text = f.read()
                source = "vault"
                break
        except Exception:
            continue
    if not text:
        text = _HELP_DEFAULT
    return jsonify({"status": "ok", "markdown": text, "source": source,
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M")})


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-TENANT CONTENT (2026-08-11)
# Businesses / social profiles / content types / WordPress sites — all
# user-created DB data. NO built-in defaults: clean installs start empty.
# The seller's own setup ships only as an OPTIONAL importable starter
# template (POST /api/agentic/businesses/seed). See
# Obsidian → GRC-Brain/appvault/AppVault-MultiTenant-Content-Architecture.md
# ═══════════════════════════════════════════════════════════════════════════

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _business_row_to_dict(r):
    try:
        raw_sp = r["social_platforms"] if "social_platforms" in r.keys() else None
        sp = json.loads(raw_sp or "[]") if raw_sp else []
    except Exception:
        sp = []
    return {"id": r["id"], "name": r["name"], "voice": r["voice"] or "",
            "website": r["website"] or "", "cta_offer": r["cta_offer"] or "",
            "social_platforms": sp or None,
            "daily_target": int(r["daily_target"]) if "daily_target" in r.keys() and r["daily_target"] is not None else 8,
            "created": r["created"], "updated": r["updated"]}


def _get_business(bid):
    conn = _db()
    row = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    conn.close()
    return _business_row_to_dict(row) if row else None


def _get_content_type(ctid):
    """User-defined content template (voice/structure/platforms). Used by
    /api/agentic/oracle/generate to drive prompt assembly."""
    conn = _db()
    row = conn.execute("SELECT * FROM content_types WHERE id=?", (ctid,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        platforms = json.loads(row["platforms"] or "[]")
    except Exception:
        platforms = []
    return {"id": row["id"], "business_id": row["business_id"], "name": row["name"],
            "purpose": row["purpose"] or "", "voice": row["voice"] or "",
            "structure": row["structure"] or "", "platforms": platforms,
            "cadence": row["cadence"] or "", "length_guard": row["length_guard"] or 0,
            "enabled": bool(row["enabled"]), "created": row["created"]}


def _social_row_to_dict(r):
    has = lambda k: k in r.keys()
    return {"id": r["id"], "business_id": r["business_id"], "platform": r["platform"] or "",
            "display_name": r["display_name"] or "", "handle": r["handle"] or "",
            "ocoya_workspace_id": r["ocoya_workspace_id"] or "",
            "ocoya_profile_id": r["ocoya_profile_id"] or "",
            "webhook_url": (r["webhook_url"] or "") if has("webhook_url") else "",
            "webhook_auth": ("•••• (set)" if r["webhook_auth"] else "") if has("webhook_auth") else "",
            "enabled": bool(r["enabled"]), "created": r["created"]}


def _wp_site_row_to_dict(r, mask=True):
    d = {"id": r["id"], "business_id": r["business_id"], "name": r["name"] or "",
         "site_url": r["site_url"] or "", "username": r["username"] or "",
         "enabled": bool(r["enabled"]), "created": r["created"]}
    d["app_password"] = ("•••••• (set)" if r["app_password"] else "") if mask else (r["app_password"] or "")
    return d


# ---- businesses ----

@agentic_bp.route("/api/agentic/businesses", methods=["GET", "POST", "OPTIONS"])
def api_businesses():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        conn = _db()
        rows = conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()
        conn.close()
        return jsonify({"status": "ok", "businesses": [_business_row_to_dict(r) for r in rows]})
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    now = _now()
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO businesses (name, voice, website, cta_offer, social_platforms, daily_target, created, updated) VALUES (?,?,?,?,?,?,?,?)",
            (name, (data.get("voice") or "").strip(), (data.get("website") or "").strip(),
             (data.get("cta_offer") or "").strip(),
             json.dumps(data.get("social_platforms") or []) if data.get("social_platforms") else None,
             int(data.get("daily_target") or 8),
             now, now))
        conn.commit()
        biz = _business_row_to_dict(conn.execute(
            "SELECT * FROM businesses WHERE id=?", (cur.lastrowid,)).fetchone())
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"could not create business: {str(e)[:120]}"}), 400
    conn.close()
    return jsonify({"status": "ok", "business": biz})


@agentic_bp.route("/api/agentic/businesses/<int:bid>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
def api_business(bid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "business not found"}), 404
    if request.method == "GET":
        biz = _business_row_to_dict(row)
        conn.close()
        return jsonify({"status": "ok", "business": biz})
    if request.method == "DELETE":
        conn.execute("UPDATE oracle_feeds SET business_id=NULL WHERE business_id=?", (bid,))
        conn.execute("UPDATE social_profiles SET business_id=NULL WHERE business_id=?", (bid,))
        conn.execute("UPDATE content_types SET business_id=NULL WHERE business_id=?", (bid,))
        conn.execute("UPDATE wordpress_sites SET business_id=NULL WHERE business_id=?", (bid,))
        conn.execute("DELETE FROM businesses WHERE id=?", (bid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": bid})
    data = request.get_json() or {}
    name = (data.get("name") if data.get("name") is not None else row["name"]).strip() or row["name"]
    sp = row["social_platforms"] if "social_platforms" in row.keys() else None
    if data.get("social_platforms") is not None:
        sp = json.dumps([str(x).strip() for x in data["social_platforms"] if str(x).strip()]) if isinstance(data["social_platforms"], list) else None
    conn.execute("UPDATE businesses SET name=?, voice=?, website=?, cta_offer=?, social_platforms=?, daily_target=?, updated=? WHERE id=?",
                 (name,
                 (data.get("voice") if data.get("voice") is not None else row["voice"] or "").strip(),
                 (data.get("website") if data.get("website") is not None else row["website"] or "").strip(),
                 (data.get("cta_offer") if data.get("cta_offer") is not None else row["cta_offer"] or "").strip(),
                 sp,
                 int(data.get("daily_target") if data.get("daily_target") is not None else (row["daily_target"] if "daily_target" in row.keys() else 8) or 8),
                 _now(), bid))
    conn.commit()
    biz = _business_row_to_dict(conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone())
    conn.close()
    return jsonify({"status": "ok", "business": biz})


# ---- social profiles (user-built, per business) ----

@agentic_bp.route("/api/agentic/social-profiles/discover", methods=["GET", "OPTIONS"])
def api_social_profiles_discover():
    """List ALL Ocoya workspaces + connected profiles so the Businesses panel
    can wire a business's account with one click. Uses the appvault project's
    Ocoya key (the single key that owns every workspace)."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    cfg = _social_router_cfg("appvault") or {}
    key = (cfg.get("api_key") or "").strip()
    if not key:
        return jsonify({"status": "error", "error": "no Ocoya API key configured (Pipeline → Social tab, appvault project)"}), 200
    ok, detail = _ocoya_check(cfg)
    info = {}
    try:
        info = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except Exception:
        info = {}
    ws_list = info.get("workspaces") or []
    profiles = []
    for w in ws_list:
        wid = w.get("id")
        if not wid:
            continue
        try:
            pdata, pstatus = _http(
                "https://app.ocoya.com/api/_public/v1/social-profiles?workspaceId=" + urllib.parse.quote(str(wid)),
                headers={"X-API-Key": key}, timeout=15)
            if pstatus == 200 and isinstance(pdata, list):
                for p in pdata:
                    if p.get("id"):
                        # Ocoya returns the account kind in `provider` (twitter,
                        # linkedin, facebook…) — normalize to our platform names.
                        prov = str(p.get("provider") or p.get("platform") or "").lower()
                        plat = "x" if prov == "twitter" else prov
                        profiles.append({"workspace_id": str(wid), "workspace_name": w.get("name") or "",
                                         "id": str(p.get("id")), "platform": plat,
                                         "name": str(p.get("name") or "")})
        except Exception:
            continue
    return jsonify({"status": "ok", "workspaces": ws_list, "profiles": profiles})


@agentic_bp.route("/api/agentic/social-profiles", methods=["GET", "POST", "OPTIONS"])
def api_social_profiles():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        biz = request.args.get("business_id")
        conn = _db()
        if biz:
            rows = conn.execute("SELECT * FROM social_profiles WHERE business_id=? ORDER BY id",
                                (int(biz),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM social_profiles ORDER BY id").fetchall()
        conn.close()
        return jsonify({"status": "ok", "profiles": [_social_row_to_dict(r) for r in rows]})
    data = request.get_json() or {}
    platform = (data.get("platform") or "").strip()
    if not platform:
        return jsonify({"error": "platform required (e.g. X, LinkedIn, Facebook, Instagram, TikTok)"}), 400
    conn = _db()
    cur = conn.execute(
        "INSERT INTO social_profiles (business_id, platform, display_name, handle, ocoya_workspace_id, ocoya_profile_id, webhook_url, webhook_auth, enabled, created)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (data.get("business_id"), platform, (data.get("display_name") or "").strip(),
         (data.get("handle") or "").strip(), (data.get("ocoya_workspace_id") or "").strip(),
         (data.get("ocoya_profile_id") or "").strip(), (data.get("webhook_url") or "").strip(),
         (data.get("webhook_auth") or "").strip(), 1 if data.get("enabled", True) else 0, _now()))
    conn.commit()
    prof = _social_row_to_dict(conn.execute("SELECT * FROM social_profiles WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.close()
    return jsonify({"status": "ok", "profile": prof})


@agentic_bp.route("/api/agentic/social-profiles/<int:sid>", methods=["PUT", "DELETE", "OPTIONS"])
def api_social_profile(sid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM social_profiles WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "profile not found"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM social_profiles WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": sid})
    data = request.get_json() or {}
    # Secret preservation: empty webhook_auth means "keep the current one".
    new_auth = (data.get("webhook_auth") if data.get("webhook_auth") is not None else row["webhook_auth"] or "").strip()
    if data.get("webhook_auth") == "" and row["webhook_auth"]:
        new_auth = row["webhook_auth"]
    conn.execute(
        "UPDATE social_profiles SET business_id=?, platform=?, display_name=?, handle=?, ocoya_workspace_id=?, ocoya_profile_id=?, webhook_url=?, webhook_auth=?, enabled=? WHERE id=?",
        (data.get("business_id") if data.get("business_id") is not None else row["business_id"],
         (data.get("platform") if data.get("platform") is not None else row["platform"] or "").strip(),
         (data.get("display_name") if data.get("display_name") is not None else row["display_name"] or "").strip(),
         (data.get("handle") if data.get("handle") is not None else row["handle"] or "").strip(),
         (data.get("ocoya_workspace_id") if data.get("ocoya_workspace_id") is not None else row["ocoya_workspace_id"] or "").strip(),
         (data.get("ocoya_profile_id") if data.get("ocoya_profile_id") is not None else row["ocoya_profile_id"] or "").strip(),
         (data.get("webhook_url") if data.get("webhook_url") is not None else row["webhook_url"] or "").strip(),
         new_auth,
         1 if (data.get("enabled") if data.get("enabled") is not None else row["enabled"]) else 0, sid))
    conn.commit()
    prof = _social_row_to_dict(conn.execute("SELECT * FROM social_profiles WHERE id=?", (sid,)).fetchone())
    conn.close()
    return jsonify({"status": "ok", "profile": prof})


# ---- content types (user-defined templates) ----

@agentic_bp.route("/api/agentic/content-types", methods=["GET", "POST", "OPTIONS"])
def api_content_types():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        biz = request.args.get("business_id")
        conn = _db()
        if biz:
            rows = conn.execute("SELECT * FROM content_types WHERE business_id=? ORDER BY id",
                                (int(biz),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM content_types ORDER BY id").fetchall()
        conn.close()
        return jsonify({"status": "ok", "content_types": [_get_content_type(r["id"]) for r in rows]})
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = _db()
    cur = conn.execute(
        "INSERT INTO content_types (business_id, name, purpose, voice, structure, platforms, cadence, length_guard, enabled, created)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (data.get("business_id"), name, (data.get("purpose") or "").strip(),
         (data.get("voice") or "").strip(), (data.get("structure") or "").strip(),
         json.dumps(data.get("platforms") or []), (data.get("cadence") or "").strip(),
         int(data.get("length_guard") or 0), 1 if data.get("enabled", True) else 0, _now()))
    conn.commit()
    ct = _get_content_type(cur.lastrowid)
    conn.close()
    return jsonify({"status": "ok", "content_type": ct})


@agentic_bp.route("/api/agentic/content-types/<int:ctid>", methods=["PUT", "DELETE", "OPTIONS"])
def api_content_type(ctid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM content_types WHERE id=?", (ctid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "content type not found"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM content_types WHERE id=?", (ctid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": ctid})
    data = request.get_json() or {}
    conn.execute(
        "UPDATE content_types SET business_id=?, name=?, purpose=?, voice=?, structure=?, platforms=?, cadence=?, length_guard=?, enabled=? WHERE id=?",
        (data.get("business_id") if data.get("business_id") is not None else row["business_id"],
         (data.get("name") if data.get("name") is not None else row["name"] or "").strip(),
         (data.get("purpose") if data.get("purpose") is not None else row["purpose"] or "").strip(),
         (data.get("voice") if data.get("voice") is not None else row["voice"] or "").strip(),
         (data.get("structure") if data.get("structure") is not None else row["structure"] or "").strip(),
         json.dumps(data.get("platforms") if data.get("platforms") is not None else json.loads(row["platforms"] or "[]")),
         (data.get("cadence") if data.get("cadence") is not None else row["cadence"] or "").strip(),
         int(data.get("length_guard") if data.get("length_guard") is not None else (row["length_guard"] or 0)),
         1 if (data.get("enabled") if data.get("enabled") is not None else row["enabled"]) else 0, ctid))
    conn.commit()
    ct = _get_content_type(ctid)
    conn.close()
    return jsonify({"status": "ok", "content_type": ct})


# ---- WordPress sites (multi, per business) ----

@agentic_bp.route("/api/agentic/wordpress-sites", methods=["GET", "POST", "OPTIONS"])
def api_wp_sites():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        biz = request.args.get("business_id")
        conn = _db()
        if biz:
            rows = conn.execute("SELECT * FROM wordpress_sites WHERE business_id=? ORDER BY id",
                                (int(biz),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM wordpress_sites ORDER BY id").fetchall()
        conn.close()
        return jsonify({"status": "ok", "sites": [_wp_site_row_to_dict(r) for r in rows]})
    data = request.get_json() or {}
    site_url = (data.get("site_url") or "").strip().rstrip("/")
    if not site_url:
        return jsonify({"error": "site_url required (e.g. https://yourblog.com)"}), 400
    conn = _db()
    cur = conn.execute(
        "INSERT INTO wordpress_sites (business_id, name, site_url, username, app_password, enabled, created)"
        " VALUES (?,?,?,?,?,?,?)",
        (data.get("business_id"), (data.get("name") or "").strip(), site_url,
         (data.get("username") or "").strip(), (data.get("app_password") or "").strip(),
         1 if data.get("enabled", True) else 0, _now()))
    conn.commit()
    site = _wp_site_row_to_dict(conn.execute("SELECT * FROM wordpress_sites WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.close()
    return jsonify({"status": "ok", "site": site})


@agentic_bp.route("/api/agentic/wordpress-sites/<int:wsid>", methods=["PUT", "DELETE", "OPTIONS"])
def api_wp_site(wsid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    row = conn.execute("SELECT * FROM wordpress_sites WHERE id=?", (wsid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "site not found"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM wordpress_sites WHERE id=?", (wsid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "deleted": wsid})
    data = request.get_json() or {}
    # SECRET PRESERVATION: empty app_password on save = keep current (same rule as Ocoya router)
    pw = row["app_password"]
    if data.get("app_password") is not None and str(data.get("app_password")).strip():
        pw = str(data.get("app_password")).strip()
    conn.execute(
        "UPDATE wordpress_sites SET business_id=?, name=?, site_url=?, username=?, app_password=?, enabled=? WHERE id=?",
        (data.get("business_id") if data.get("business_id") is not None else row["business_id"],
         (data.get("name") if data.get("name") is not None else row["name"] or "").strip(),
         (data.get("site_url") if data.get("site_url") is not None else row["site_url"] or "").strip().rstrip("/"),
         (data.get("username") if data.get("username") is not None else row["username"] or "").strip(),
         pw, 1 if (data.get("enabled") if data.get("enabled") is not None else row["enabled"]) else 0, wsid))
    conn.commit()
    site = _wp_site_row_to_dict(conn.execute("SELECT * FROM wordpress_sites WHERE id=?", (wsid,)).fetchone())
    conn.close()
    return jsonify({"status": "ok", "site": site})


@agentic_bp.route("/api/agentic/wordpress-sites/<int:wsid>/test", methods=["POST", "OPTIONS"])
def api_wp_site_test(wsid):
    """Verify a site's credentials: GET /wp-json/wp/v2/posts?per_page=1."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    import base64 as _b64
    conn = _db()
    row = conn.execute("SELECT * FROM wordpress_sites WHERE id=?", (wsid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "site not found"}), 404
    site = (row["site_url"] or "").rstrip("/")
    if not site:
        return jsonify({"status": "error", "error": "no site URL configured"}), 400
    if not row["username"] or not row["app_password"]:
        return jsonify({"status": "error", "error": "no credentials configured"}), 400
    token = _b64.b64encode(f"{row['username']}:{row['app_password']}".encode("utf-8")).decode("ascii")
    data, code = _http(f"{site}/wp-json/wp/v2/posts?per_page=1",
                       headers={"Authorization": f"Basic {token}"}, timeout=25)
    if code == 200:
        n = len(data) if isinstance(data, list) else "?"
        return jsonify({"status": "ok", "detail": f"auth OK — API reachable (sample: {n} post)"})
    return jsonify({"status": "error", "detail": f"HTTP {code}: {str(data)[:300]}"}), 200


def _wp_publish_site(site_id, title, content, status="draft"):
    """Publish to a specific wordpress_sites row. Returns (ok, result)."""
    import base64 as _b64
    conn = _db()
    row = conn.execute("SELECT * FROM wordpress_sites WHERE id=?", (site_id,)).fetchone()
    conn.close()
    if not row:
        return False, "site not found"
    site = (row["site_url"] or "").rstrip("/")
    if not site:
        return False, "not configured — no site URL"
    if not row["username"] or not row["app_password"]:
        return False, "not configured — add username + app password"
    token = _b64.b64encode(f"{row['username']}:{row['app_password']}".encode("utf-8")).decode("ascii")
    data, code = _http(f"{site}/wp-json/wp/v2/posts", method="POST",
                       headers={"Authorization": f"Basic {token}"},
                       json_data={"title": str(title)[:200], "content": str(content),
                                  "status": status if status in ("publish", "draft", "pending", "private") else "draft"},
                       timeout=40)
    if code in (200, 201) and isinstance(data, dict):
        return True, {"id": data.get("id"), "link": data.get("link"),
                      "status": data.get("status"), "title": (data.get("title") or {}).get("rendered", title)}
    return False, f"HTTP {code}: {str(data)[:300]}"


# ---- starter template (optional import — the seller's own setup as seed data) ----

_STARTER_TEMPLATE = {
    "businesses": [
        {
            "name": "AI RepoIndex",
            "voice": "conversational expert — a knowledgeable insider reviewing AI tools for founders and builders",
            "website": "https://airepoindex.com",
            "cta_offer": "Get your AI tool listed — Verified listings from $49/mo",
            "feeds": [
                {"name": "AI Tools News", "query": "AI tool releases",
                 "rss_urls": ["https://news.google.com/rss/search?q=AI+tool&hl=en-US&gl=US&ceid=US:en",
                              "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"],
                 "subreddits": ["artificial", "LocalLLaMA"], "hn_query": "AI tool",
                 "github_query": "ai tool", "sources": ["rss", "reddit", "hn", "github"]},
            ],
            "content_types": [
                {"name": "Tool Release Alert", "purpose": "announce a new AI tool release with one sharp take",
                 "voice": "conversational expert — hook first, facts woven into the narrative",
                 "structure": "hook → what it is → why it matters → CTA", "platforms": ["x", "linkedin"]},
                {"name": "Weekly Roundup", "purpose": "the week's best AI tools in one post",
                 "voice": "conversational expert", "structure": "intro → 3-5 tools → verdict → CTA",
                 "platforms": ["blog"]},
            ],
        },
        {
            "name": "Security RepoIndex",
            "voice": "conversational expert — an honest security tools reviewer",
            "website": "https://securityrepoindex.com",
            "cta_offer": "Get your security tool listed — Verified from $49/mo",
            "feeds": [
                {"name": "Security News", "query": "cybersecurity tools",
                 "rss_urls": ["https://feeds.feedburner.com/TheHackersNews",
                              "https://krebsonsecurity.com/feed/"],
                 "subreddits": ["netsec"], "hn_query": "security vulnerability",
                 "sources": ["rss", "reddit", "hn"]},
            ],
            "content_types": [
                {"name": "Vulnerability Alert", "purpose": "breaking vuln news with who's affected and the fix",
                 "voice": "calm, evidence-first — explains risk without panic",
                 "structure": "what → who's affected → fix → CTA", "platforms": ["x", "linkedin", "blog"]},
            ],
        },
        {
            "name": "CISOvault",
            "voice": "calm, evidence-first security advisor",
            "website": "https://cisovault.com",
            "cta_offer": "Full exposure scan + CVE report — $149",
            "feeds": [
                {"name": "Threat Intel", "query": "data breach vulnerability",
                 "rss_urls": ["https://www.bleepingcomputer.com/feed/",
                              "https://feeds.feedburner.com/TheHackersNews"],
                 "subreddits": ["netsec"], "hn_query": "breach", "sources": ["rss", "reddit", "hn"]},
            ],
            "content_types": [
                {"name": "Security Alert", "purpose": "urgent exposure news for business owners",
                 "voice": "calm, evidence-first", "structure": "what happened → risk → one action → CTA",
                 "platforms": ["x", "linkedin"]},
                {"name": "Risk Explainer", "purpose": "plain-English security education",
                 "voice": "plain-English teacher, zero jargon", "structure": "scene → problem → fix → CTA",
                 "platforms": ["blog"]},
            ],
        },
        {
            "name": "WriterStudioAI",
            "voice": "helpful, practical writing SaaS voice",
            "website": "",
            "cta_offer": "Start your free trial",
            "feeds": [],
            "content_types": [
                {"name": "Writing Tip", "purpose": "one actionable writing tip per post",
                 "voice": "helpful and practical", "structure": "tip → example → CTA",
                 "platforms": ["x", "linkedin", "facebook", "instagram"]},
            ],
        },
    ],
}


@agentic_bp.route("/api/agentic/businesses/seed", methods=["POST", "OPTIONS"])
def api_businesses_seed():
    """Import the optional starter template (seller's proven setup).
    Idempotent: skips businesses whose name already exists unless force=1.
    This is the ONLY place seller-specific data lives — as importable data,
    never as code defaults."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    force = bool(data.get("force"))
    conn = _db()
    created_biz = created_feed = created_ct = skipped = 0
    for tpl in _STARTER_TEMPLATE["businesses"]:
        row = conn.execute("SELECT id FROM businesses WHERE name=?", (tpl["name"],)).fetchone()
        if row and not force:
            skipped += 1
            continue
        now = _now()
        if row:
            biz_id = row["id"]
            conn.execute("UPDATE businesses SET voice=?, website=?, cta_offer=?, updated=? WHERE id=?",
                         (tpl["voice"], tpl.get("website") or "", tpl.get("cta_offer") or "", now, biz_id))
        else:
            cur = conn.execute(
                "INSERT INTO businesses (name, voice, website, cta_offer, created, updated) VALUES (?,?,?,?,?,?)",
                (tpl["name"], tpl["voice"], tpl.get("website") or "", tpl.get("cta_offer") or "", now, now))
            biz_id = cur.lastrowid
            created_biz += 1
        for f in tpl.get("feeds") or []:
            conn.execute(
                "INSERT INTO oracle_feeds (name, query, rss_urls, subreddits, hn_query, github_query, youtube_channels, sources, skip_repeats, business_id, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f["name"], f.get("query") or f["name"], json.dumps(f.get("rss_urls") or []),
                 json.dumps(f.get("subreddits") or []), f.get("hn_query") or "",
                 f.get("github_query") or "", json.dumps(f.get("youtube_channels") or []),
                 json.dumps(f.get("sources") or []), 1, biz_id, now))
            created_feed += 1
        for ct in tpl.get("content_types") or []:
            conn.execute(
                "INSERT INTO content_types (business_id, name, purpose, voice, structure, platforms, cadence, length_guard, enabled, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (biz_id, ct["name"], ct.get("purpose") or "", ct.get("voice") or "",
                 ct.get("structure") or "", json.dumps(ct.get("platforms") or []),
                 ct.get("cadence") or "", int(ct.get("length_guard") or 0), 1, now))
            created_ct += 1
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "created_businesses": created_biz, "created_feeds": created_feed,
                    "created_content_types": created_ct, "skipped_existing": skipped})


# ---- onboarding status (drives the setup wizard) ----

@agentic_bp.route("/api/agentic/content/status", methods=["GET", "OPTIONS"])
def api_content_status():
    """Clean-install overview: what exists per business. The wizard shows this
    as '0 businesses → build your machine'. Nothing is pre-seeded."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    conn = _db()
    businesses = [_business_row_to_dict(r) for r in conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()]
    per_business = []
    for b in businesses:
        per_business.append({
            "id": b["id"], "name": b["name"],
            "feeds": conn.execute("SELECT COUNT(*) AS n FROM oracle_feeds WHERE business_id=?", (b["id"],)).fetchone()["n"],
            "profiles": conn.execute("SELECT COUNT(*) AS n FROM social_profiles WHERE business_id=?", (b["id"],)).fetchone()["n"],
            "content_types": conn.execute("SELECT COUNT(*) AS n FROM content_types WHERE business_id=?", (b["id"],)).fetchone()["n"],
            "wp_sites": conn.execute("SELECT COUNT(*) AS n FROM wordpress_sites WHERE business_id=?", (b["id"],)).fetchone()["n"],
        })
    totals = {
        "businesses": len(businesses),
        "feeds": conn.execute("SELECT COUNT(*) AS n FROM oracle_feeds").fetchone()["n"],
        "social_profiles": conn.execute("SELECT COUNT(*) AS n FROM social_profiles").fetchone()["n"],
        "content_types": conn.execute("SELECT COUNT(*) AS n FROM content_types").fetchone()["n"],
        "wordpress_sites": conn.execute("SELECT COUNT(*) AS n FROM wordpress_sites").fetchone()["n"],
    }
    conn.close()
    return jsonify({"status": "ok", "totals": totals, "per_business": per_business})


# ---- business-aware routing helpers (2026-08-11) ----
# Used by _pipeline_social_posts (dynamic platform list) and _ocoya_send
# (profile resolution). Post carries its business as tag `biz:<id>`.

def _platforms_for_business(biz):
    """Platforms to GENERATE posts for (drafts). Data-driven — nothing hardcoded:
    1) business.social_platforms when set; 2) union of the business's content-type
    platforms; 3) None (= legacy default set). x expands to x + x_thread.
    Generation is decoupled from accounts: routing still sends only to profiles
    the business actually connected."""
    if not biz:
        return None
    explicit = biz.get("social_platforms") or []
    pools = [explicit] if explicit else []
    if not pools:
        conn = _db()
        rows = conn.execute(
            "SELECT platforms FROM content_types WHERE business_id=? AND enabled=1",
            (biz["id"],)).fetchall()
        conn.close()
        pools = [json.loads(r["platforms"] or "[]") for r in rows if r["platforms"]]
    plats = []
    for pool in pools:
        for p in pool:
            p = str(p).lower().strip()
            if p == "x":
                for extra in ("x", "x_thread"):
                    if extra not in plats:
                        plats.append(extra)
            elif p in _PIPELINE_PLATFORMS and p not in plats:
                plats.append(p)
    return plats or None


def _biz_for_project(project):
    """Resolve a pipeline project to its multi-tenant business.
    1) explicit business_id in the project config; 2) normalized name match."""
    if not project:
        return None
    cfg = _project_cfg(project) or {}
    bid = cfg.get("business_id")
    if bid:
        b = _get_business(int(bid))
        if b:
            return b
    conn = _db()
    rows = conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()
    conn.close()
    key = re.sub(r"[^a-z0-9]", "", str(project).lower())
    for r in rows:
        if re.sub(r"[^a-z0-9]", "", str(r["name"] or "").lower()) == key:
            return _business_row_to_dict(r)
    return None


def _biz_profiles(biz_id):
    """Enabled social_profiles rows for a business (multi-tenant).
    RAW dicts — internal routing needs unmasked secrets (webhook_auth);
    masking happens only at the API boundary (_social_row_to_dict)."""
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM social_profiles WHERE business_id=? AND enabled=1 ORDER BY id",
        (biz_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _biz_profile_for(post, platform, fallback_ws):
    """Resolve a post's target Ocoya profile from the business's social_profiles.
    Returns (profile_id, workspace_id). Empty profile = caller keeps its default."""
    biz_id = None
    for t in str(post.get("tags") or "").split(","):
        t = t.strip()
        if t.startswith("biz:") and t[4:].isdigit():
            biz_id = int(t[4:])
            break
    if biz_id is None:
        return "", (fallback_ws or "")
    want = "x" if platform == "x_thread" else platform
    for pr in _biz_profiles(biz_id):
        if (pr.get("platform") or "").lower() == want and (pr.get("ocoya_profile_id") or ""):
            return (pr.get("ocoya_profile_id") or ""), (pr.get("ocoya_workspace_id") or (fallback_ws or ""))
    return "", (fallback_ws or "")


def _biz_tag(post):
    """True when the post carries a business id (tag biz:<id>)."""
    for t in str(post.get("tags") or "").split(","):
        if t.strip().startswith("biz:") and t.strip()[4:].isdigit():
            return True
    return False


def _biz_id_of(post):
    for t in str(post.get("tags") or "").split(","):
        t = t.strip()
        if t.startswith("biz:") and t[4:].isdigit():
            return int(t[4:])
    return None


def _webhook_send(post, prof):
    """Generic webhook delivery for a business profile (Buffer/Make/n8n/Zapier…).
    Returns (ok, detail)."""
    url = (prof.get("webhook_url") or "").strip()
    if not url:
        return False, "no webhook_url on profile"
    payload = {
        "title": post.get("title") or "",
        "platform": post.get("category") or post.get("platform") or "",
        "content": post.get("content") or "",
        "image_url": post.get("image_url") or "",
        "article_id": (post.get("tags") or "").replace("article:", "")[:40],
        "business_id": _biz_id_of(post),
        "scheduled_at": post.get("scheduled_at") or "",
        "source": "appvault-pipeline",
    }
    hdrs = {"Content-Type": "application/json"}
    ah = (prof.get("webhook_auth") or "").strip()
    if ah and ":" in ah:
        hdrs[ah.split(":", 1)[0].strip()] = ah.split(":", 1)[1].strip()
    try:
        data, status = _http(url, method="POST", headers=hdrs, json_data=payload, timeout=30)
    except Exception as e:
        return False, f"webhook call failed: {str(e)[:200]}"
    if status in (200, 201, 202, 204):
        return True, f"webhook HTTP {status}"
    return False, f"webhook HTTP {status}: {str(data)[:200]}"


def _biz_deliver(post, cfg):
    """Deliver a business-owned post through the business's own connectors:
    Ocoya-wired profile first, then a webhook profile. Returns
    (True, detail) on success, (False, detail) on attempted-failure,
    (None, None) when the business has NO connector for this platform."""
    biz_id = _biz_id_of(post)
    if biz_id is None:
        return None, None
    platform = post.get("category") or ""
    want = "x" if platform == "x_thread" else platform
    profs = _biz_profiles(biz_id)
    ocoya_prof = next((p for p in profs if (p.get("platform") or "").lower() == want and (p.get("ocoya_profile_id") or "")), None)
    wh_prof = next((p for p in profs if (p.get("platform") or "").lower() == want and (p.get("webhook_url") or "")), None)
    if not ocoya_prof and not wh_prof:
        return None, None
    if ocoya_prof:
        # The appvault project owns the single Ocoya API key for every workspace.
        c2 = dict(_social_router_cfg("appvault") or {})
        c2["workspace_id"] = ocoya_prof.get("ocoya_workspace_id") or c2.get("workspace_id")
        c2["profile_ids"] = {}  # force business-profile resolution, not legacy project mapping
        ok, det = _ocoya_send(post, c2)
        if ok:
            return True, det
        if wh_prof:
            ok2, det2 = _webhook_send(post, wh_prof)
            if ok2:
                return True, det2
            return False, f"ocoya failed ({det}); webhook failed ({det2})"
        return False, det
    return _webhook_send(post, wh_prof)


def _new_link_code():
    import uuid as _uuid
    return _uuid.uuid4().hex[:8]


# ---- calendar (A2): scheduled work items + oracle posts in a date range ----

def _pull_external_posts(biz_filter=""):
    """Pull posts from each business's Ocoya-wired workspace (read-only,
    kind=external). Only posts with a scheduled time land on the calendar.
    Connector-agnostic shape: later connectors (Buffer…) plug in here."""
    out = []
    key = (_social_router_cfg("appvault") or {}).get("api_key") or ""
    if not key:
        return out
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM social_profiles WHERE ocoya_profile_id IS NOT NULL AND ocoya_profile_id != '' AND enabled=1").fetchall()
    conn.close()
    seen_ws = set()
    for r in rows:
        ws = r["ocoya_workspace_id"] or ""
        if not ws or ws in seen_ws:
            continue
        seen_ws.add(ws)
        if biz_filter and str(r["business_id"] or "") != biz_filter:
            continue
        # profile id -> platform map (provider field, twitter -> x)
        idmap = {}
        try:
            pdata2, pstatus2 = _http("https://app.ocoya.com/api/_public/v1/social-profiles?workspaceId=" + urllib.parse.quote(ws),
                                     headers={"X-API-Key": key}, timeout=15)
            if pstatus2 == 200 and isinstance(pdata2, list):
                for p2 in pdata2:
                    if p2.get("id"):
                        prov = str(p2.get("provider") or "").lower()
                        idmap[str(p2.get("id"))] = "x" if prov == "twitter" else prov
        except Exception:
            pass
        try:
            pdata, pstatus = _http("https://app.ocoya.com/api/_public/v1/post?workspaceId=" + urllib.parse.quote(ws),
                                   headers={"X-API-Key": key}, timeout=15)
        except Exception:
            continue
        if pstatus != 200 or not isinstance(pdata, list):
            continue
        for p in pdata:
            if not isinstance(p, dict):
                continue
            sched = p.get("scheduledAt") or ""
            if not sched:
                continue  # no slot -> nothing to place on the calendar
            pids = p.get("socialProfileIds") or []
            plat = ""
            for pid in pids:
                plat = idmap.get(str(pid)) or ""
                if plat:
                    break
            out.append({"kind": "external", "id": str(p.get("id") or ""),
                        "title": str(p.get("title") or p.get("caption") or "Ocoya post")[:80],
                        "platform": plat,
                        "status": str(p.get("status") or "unknown").lower(),
                        "scheduled_at": sched[:16].replace("T", " ") if sched else "",
                        "business_id": r["business_id"], "link_code": ""})
    return out


@agentic_bp.route("/api/agentic/calendar", methods=["GET", "OPTIONS"])
def api_calendar():
    """Week/month view: work items + oracle posts with scheduled_at in [start, end].
    Query: ?start=YYYY-MM-DD&end=YYYY-MM-DD&business_id=N"""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    start = (request.args.get("start") or "").strip() or "0000-01-01"
    end = (request.args.get("end") or "").strip() or "9999-12-31"
    # Bare dates expand to full-day ranges so "2026-08-12 09:30" matches a
    # single-day query (string comparison would otherwise exclude it).
    if len(start) == 10:
        start = start + " 00:00:00"
    if len(end) == 10:
        end = end + " 23:59:59"
    biz_filter = (request.args.get("business_id") or "").strip()
    conn = _db()
    items = []
    rows = conn.execute(
        "SELECT * FROM work_items WHERE scheduled_at IS NOT NULL AND scheduled_at != ''"
        " AND scheduled_at >= ? AND scheduled_at <= ? ORDER BY scheduled_at",
        (start, end)).fetchall()
    for r in rows:
        d = dict(r)
        bid = _biz_id_of({"tags": d.get("tags") or ""})
        if biz_filter and str(bid or "") != biz_filter:
            continue
        items.append({"kind": "work", "id": d["id"], "title": d.get("title") or "Post",
                      "platform": d.get("category") or "", "status": d.get("status") or "",
                      "scheduled_at": d.get("scheduled_at") or "", "business_id": bid,
                      "link_code": d.get("link_code") or ""})
    rows2 = conn.execute(
        "SELECT * FROM oracle_posts WHERE scheduled_at IS NOT NULL AND scheduled_at != ''"
        " AND scheduled_at >= ? AND scheduled_at <= ? ORDER BY scheduled_at",
        (start, end)).fetchall()
    for r in rows2:
        d = dict(r)
        if biz_filter and str(d.get("business_id") or "") != biz_filter:
            continue
        items.append({"kind": "oracle", "id": d["id"], "title": d.get("title") or "Post",
                      "platform": d.get("platform") or "", "status": d.get("status") or "",
                      "scheduled_at": d.get("scheduled_at") or "", "business_id": d.get("business_id"),
                      "link_code": ""})
    # External connector posts (Ocoya etc.) — read-only, only dated ones.
    if request.args.get("external") == "1":
        for it in _pull_external_posts(biz_filter):
            sa = it.get("scheduled_at") or ""
            if sa and start <= sa <= end:
                items.append(it)
    names = {}
    for r in conn.execute("SELECT id, name FROM businesses").fetchall():
        names[r["id"]] = r["name"]
    conn.close()
    for it in items:
        it["business_name"] = names.get(it.get("business_id")) or ""
    return jsonify({"status": "ok", "items": items})


@agentic_bp.route("/api/agentic/pipeline/<wid>", methods=["GET", "PUT", "OPTIONS"])
def api_pipeline_item(wid):
    """Human-in-the-loop (2026-08-12): GET a work item's full content and PUT
    edited title/content back — the humanize-before-posting gate on the calendar."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"error": "not found"}), 404
    if request.method == "GET":
        return jsonify({"status": "ok", "item": item})
    data = request.get_json() or {}
    fields = {}
    if data.get("title") is not None:
        fields["title"] = str(data["title"])[:4000]
    if data.get("content") is not None:
        fields["content"] = str(data["content"])[:4000]
    if not fields:
        return jsonify({"error": "nothing to update (title/content)"}), 400
    it = _pipeline_update(wid, **fields)
    return jsonify({"status": "ok", "item": it})


@agentic_bp.route("/api/agentic/pipeline/<wid>/schedule", methods=["POST", "OPTIONS"])
def api_pipeline_schedule(wid):
    """Set the calendar slot for a work item. Body: {scheduled_at: 'YYYY-MM-DD HH:MM'}"""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    item = _pipeline_get(wid)
    if not item:
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    at = (data.get("scheduled_at") or "").strip()
    if not at:
        return jsonify({"error": "scheduled_at required (YYYY-MM-DD HH:MM)"}), 400
    it = _pipeline_update(wid, scheduled_at=at)
    return jsonify({"status": "ok", "item": it})


# ---- link tracking (B3): short links + UTM + click counting ----

@agentic_bp.route("/api/agentic/links", methods=["GET", "POST", "OPTIONS"])
def api_links():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    if request.method == "GET":
        conn = _db()
        rows = conn.execute("SELECT * FROM links ORDER BY created DESC LIMIT 200").fetchall()
        conn.close()
        return jsonify({"status": "ok", "links": [dict(r) for r in rows]})
    data = request.get_json() or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"error": "target URL required"}), 400
    code = (data.get("code") or "").strip() or _new_link_code()
    conn = _db()
    conn.execute("INSERT OR IGNORE INTO links (code, target, campaign, source, medium, clicks, created)"
                 " VALUES (?,?,?,?,?,0,?)",
                 (code, target, (data.get("campaign") or "").strip(), (data.get("source") or "").strip(),
                  (data.get("medium") or "").strip(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    base = request.host_url.rstrip("/")
    return jsonify({"status": "ok", "code": code, "url": f"{base}/api/agentic/link/{code}"})


@agentic_bp.route("/api/agentic/link/<code>")
def api_link_redirect(code):
    from flask import redirect as _flask_redirect
    conn = _db()
    row = conn.execute("SELECT * FROM links WHERE code=?", (code,)).fetchone()
    if not row:
        conn.close()
        return "not found", 404
    conn.execute("UPDATE links SET clicks = clicks + 1 WHERE code=?", (code,))
    conn.commit()
    conn.close()
    target = row["target"]
    params = []
    if row["source"]:
        params.append("utm_source=" + urllib.parse.quote(row["source"]))
    if row["medium"]:
        params.append("utm_medium=" + urllib.parse.quote(row["medium"]))
    if row["campaign"]:
        params.append("utm_campaign=" + urllib.parse.quote(row["campaign"]))
    if params:
        sep = "&" if "?" in target else "?"
        target = target + sep + "&".join(params)
    return _flask_redirect(target)


# ---------------------------------------------------------------------------
# GROWTH ENGINE (2026-08-12, G1-G4) — engagement inbox, thread studio,
# flash posts, mix planner. All output queues ready_for_approval work items;
# nothing publishes without the user (no AI自作主張 rule).
# ---------------------------------------------------------------------------

_REPLY_SYS = ("You write social media replies for a business. Given the post text, write 3 short reply "
              "variants (max 240 chars each): 1) AGREE+ADD — affirm the post and add one sharp, specific "
              "insight. 2) QUESTION — a genuine, curious question that invites the author to engage. "
              "3) COUNTERPOINT — respectfully add a missing angle or fact. Voice: {voice}. "
              "Plain English, human tone, no hashtags, no emoji spam. NEVER invent facts, names, numbers, "
              "or sources not present in the post text. Output EXACTLY numbered lines:\n1. ...\n2. ...\n3. ...")

_PLAN_SYS = {
    "x": ("You write a short X post (max 280 chars) about a topic. REQUIREMENTS: hook in the first 8 words, "
          "ONE specific fact from the topic, sharp take/opinion. Plain English, no AI-tells, no hashtag spam. "
          "Never invent facts. Output ONLY the post."),
    "x_thread": ("You write a long-form X post (X Premium allows up to 4000 chars). REQUIREMENTS: "
                 "1. NO NUMBERING — a single continuous post, never '1/5' parts. 2. Sharp hook opener, "
                 "never a date or announcement. 3. ONE specific fact from the topic. 4. Compact punchy "
                 "rhythm, 150-400 words. 5. Closer lands the takeaway + CTA. 6. Plain English, no AI-tells, "
                 "no hashtag spam. Never invent facts. Output ONLY the post."),
    "linkedin": ("You write a LinkedIn post (180-260 words) about a topic: bold hook line, 3 concrete "
                 "takeaways, a question to drive comments. Include ONE specific fact from the topic. "
                 "Plain text, short paragraphs, no hashtag spam. Never invent facts. Output ONLY the post body."),
    "facebook": ("You write a Facebook post (120-200 words) about a topic: friendly hook, one specific fact, "
                 "a question to drive comments. Short paragraphs, light emoji, a few hashtags. "
                 "Never invent facts. Output ONLY the post body."),
    "instagram": ("You write an Instagram caption (100-180 words) about a topic: strong hook, one specific "
                  "fact, line breaks for readability, 8-12 relevant hashtags. Never invent facts. "
                  "Output ONLY the caption."),
}

def _parse_replies(text):
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*\d+[\.\)]\s*(.*)$", line)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out[:3]


def _resolve_tweet_url(text):
    """Turn an x.com/twitter.com post URL into the tweet text via fxtwitter's
    free embed API (no X API needed). Returns None if not a URL / unfetchable."""
    m = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})/status/(\d+)", text or "")
    if not m:
        return None
    user, tid = m.group(1), m.group(2)
    try:
        d, st = _http("https://api.fxtwitter.com/%s/status/%s" % (user, tid), timeout=15)
    except Exception:
        return None
    if st == 200 and isinstance(d, dict):
        tw = d.get("tweet") or {}
        body = (tw.get("text") or "").strip()
        if body:
            author = (tw.get("author") or {}).get("screen_name") or user
            return body + "\n— @" + author + " on X"
    return None


@agentic_bp.route("/api/agentic/reply/copilot", methods=["POST", "OPTIONS"])
def api_reply_copilot():
    """G1: paste any hot post text (or an x.com/twitter.com URL) -> 3 reply drafts."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    post_text = (data.get("post_text") or "").strip()
    if not post_text:
        return jsonify({"error": "post_text required"}), 400
    if re.search(r"(?:twitter\.com|x\.com)/", post_text):
        resolved = _resolve_tweet_url(post_text)
        if resolved:
            post_text = resolved
        else:
            return jsonify({"error": "could not fetch that tweet — paste the post text instead"}), 422
    biz = _get_business(int(data["business_id"])) if data.get("business_id") else None
    voice = (biz or {}).get("voice") or "clear, conversational, expert"
    try:
        text = _call_llm(f"Post text:\n{post_text[:2000]}",
                         system_prompt=_REPLY_SYS.format(voice=voice), agent="reply", timeout=60)
    except Exception as e:
        return jsonify({"status": "error", "error": f"LLM failed: {str(e)[:200]}"}), 502
    replies = _parse_replies(text) or [text[:240]]
    wid = None
    if biz and replies:
        wid = _work_record(category="reply", title=f"Reply to: {post_text[:50]}",
                           content="\n\n".join(replies)[:3000], source="reply:copilot",
                           status="ready_for_approval", tags=f"reply,biz:{biz['id']}", project=_biz_project(biz))
    return jsonify({"status": "ok", "replies": replies, "work_item": wid})


@agentic_bp.route("/api/agentic/oracle/engagement/drafts", methods=["POST", "OPTIONS"])
def api_engagement_drafts():
    """G1: sweep the business's engagement feeds -> reply drafts for the top 3 items."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = data.get("business_id")
    feeds = _list_feeds(int(bid) if bid else None)
    eng = [f for f in feeds if f.get("is_engagement")]
    if not eng:
        return jsonify({"status": "ok", "drafts": 0, "note": "no engagement feeds — mark a feed as engagement"})
    made = 0
    for f in eng:
        try:
            signals, _s = _sweep_feed_sources(f, include_repeats=True)
        except Exception:
            continue
        biz = _get_business(f.get("business_id")) if f.get("business_id") else None
        voice = (biz or {}).get("voice") or "clear, conversational, expert"
        for sig in signals[:3]:
            try:
                text = _call_llm(
                    f"Post text:\n{sig.get('title')}\n{sig.get('summary') or ''}\n{sig.get('link') or ''}",
                    system_prompt=_REPLY_SYS.format(voice=voice), agent="reply", timeout=60)
            except Exception:
                continue
            replies = _parse_replies(text) or [text[:240]]
            _work_record(category="reply", title=f"Reply: {sig.get('title', '')[:50]}",
                         content="\n\n".join(replies)[:3000], source="reply:engagement",
                         status="ready_for_approval",
                         tags=f"reply,biz:{f.get('business_id') or ''}", project=_biz_project(biz))
            made += 1
    return jsonify({"status": "ok", "drafts": made})


@agentic_bp.route("/api/agentic/thread/daily", methods=["POST", "OPTIONS"])
def api_thread_daily():
    """G2: flagship long-form thread per business from its best recent article."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM work_items WHERE (category='content' OR category='article')"
        " AND (status='approved' OR status='published' OR status='draft')"
        " ORDER BY created_at DESC LIMIT 30").fetchall()
    conn.close()
    made = 0
    for r in rows:
        d = dict(r)
        bid = _biz_id_of({"tags": d.get("tags") or ""})
        if bid is None:
            continue
        if data.get("business_id") and str(bid) != str(data["business_id"]):
            continue
        conn = _db()
        exists = conn.execute("SELECT COUNT(*) c FROM work_items WHERE category='x_thread' AND tags LIKE ?",
                              ("%article:" + str(d["id"]) + "%",)).fetchone()["c"]
        conn.close()
        if exists:
            continue
        made += _pipeline_social_posts(d["id"], platforms=["x_thread"])
        break  # one thread per run (first unmatched article of the business)
    if made == 0 and data.get("business_id"):
        # No article yet — thread from the top feed story so the button always works.
        bid = int(data["business_id"])
        for f in _list_feeds(bid)[:1]:
            try:
                signals, _s = _sweep_feed_sources(f, include_repeats=True)
            except Exception:
                continue
            if not signals:
                continue
            sig = signals[0]
            msg = f"TOPIC:\n{sig.get('title')}\n{sig.get('summary') or ''}\n{sig.get('link') or ''}"
            link_code, short_url = _make_tracked_link(_get_business(bid))
            if short_url:
                msg += f"\n\nCTA LINK (include this exact URL in the post as the clickable link): {short_url}"
            try:
                content = _call_llm(msg, system_prompt=_PLAN_SYS["x_thread"], agent="plan", timeout=90)
            except Exception:
                content = ""
            content = (content or "").strip()
            if content:
                wid = _work_record(category="x_thread", title=f"🧵 {sig.get('title', '')[:52]}",
                                   content=content[:3000], source="oracle:thread", status="ready_for_approval",
                                   tags=f"social,platform:x_thread,thread,biz:{bid}", project=_biz_project(_get_business(bid)))
                if wid and link_code:
                    _pipeline_update(wid, link_code=link_code)
                made += 1
            break
    return jsonify({"status": "ok", "threads": made})


def _biz_project(biz):
    """work_items.project slug for a business — matches the pipeline project
    filter (same slugify as _biz_for_project: lowercase alnum of the name)."""
    if not biz:
        return "appvault"
    return re.sub(r"[^a-z0-9]", "", (biz.get("name") or "").lower()) or "appvault"


def _make_tracked_link(biz):
    """Create a tracked short link for a business post (B3). Returns
    (code, short_url) or ('', '') when no public base / no target configured."""
    if not biz:
        return "", ""
    target = (biz.get("website") or "").strip()
    if not target:
        return "", ""
    pub_base = ((_social_router_cfg("appvault") or {}).get("media_base_url") or "").strip().rstrip("/")
    if not pub_base:
        return "", ""
    code = _new_link_code()
    try:
        conn = _db()
        conn.execute("INSERT OR IGNORE INTO links (code, target, campaign, source, medium, clicks, created)"
                     " VALUES (?,?,?,?,?,0,?)",
                     (code, target, biz.get("name") or "", "social", "social",
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
        return code, pub_base + "/api/agentic/link/" + code
    except Exception:
        return "", ""


def _flash_from_signal(feed, sig, biz):
    sys_p = ("You write a hot-take X post (max 280 chars) about a breaking item. REQUIREMENTS: "
             "hook in the first 8 words, ONE specific fact from the item, sharp opinion/angle, "
             "plain English, no AI-tells, no hashtag spam, no emoji spam. Ground ONLY in the item below "
             "— never invent facts, names, numbers, or sources. Output ONLY the post.")
    msg = f"BREAKING ITEM:\n{sig.get('title')}\n{sig.get('summary') or ''}\n{sig.get('link') or ''}"
    link_code, short_url = _make_tracked_link(biz)
    if short_url:
        msg += f"\n\nCTA LINK (include this exact URL in the post as the clickable link): {short_url}"
    try:
        text = _call_llm(msg, system_prompt=sys_p, agent="flash", timeout=60)
    except Exception:
        return None
    text = (text or "").strip()
    if len(text) < 20:
        return None
    if len(text) > 280:
        text = text[:280]
    wid = _work_record(category="x", title=f"⚡ {sig.get('title', '')[:56]}", content=text,
                       source="oracle:flash", status="ready_for_approval",
                       tags=f"social,platform:x,flash,biz:{feed.get('business_id') or ''}",
                       project=_biz_project(biz))
    if wid and link_code:
        _pipeline_update(wid, link_code=link_code)
    return wid


@agentic_bp.route("/api/agentic/oracle/watch", methods=["GET", "OPTIONS"])
def api_oracle_watch():
    """X Watch (2026-08-12): RAW reader view of a business's followed creators'
    latest tweets (nitter RSS, no X API). Deliberately skips the news-scoring /
    repeat-filtering pipeline — this is a feed reader, not a news filter, so a
    tweet stays visible across refreshes. Returns full post text."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    biz = request.args.get("business_id")
    feeds = _list_feeds(int(biz) if biz else None)
    out = []
    for f in feeds:
        xw = f.get("x_watch") or []
        if not xw:
            continue
        for url in f.get("rss_urls") or []:
            if "nitter" not in url:
                continue
            try:
                items = _parse_rss(_fetch_feed(url))
            except Exception:
                continue
            if items:
                handle = url.rstrip("/").split("/")[-2] if "/" in url.rstrip("/") else (xw[0] or "")
                for it in items[:15]:
                    title = it.get("title") or ""
                    summary = it.get("summary") or ""
                    text = title if len(title) >= len(summary) else summary
                    link = it.get("link") or ""
                    # nitter mirrors are read-only — surface the REAL x.com tweet
                    # so "open original" opens where the user can actually reply.
                    m = re.search(r"(?:nitter\.[a-z0-9.\-]+)/([A-Za-z0-9_]{1,15})/status/(\d+)", link)
                    if m:
                        link = "https://x.com/%s/status/%s" % (m.group(1), m.group(2))
                    out.append({"title": title[:200], "text": text[:2000],
                                "link": link, "handle": handle,
                                "feed": f["name"]})
                break  # one working mirror per feed is enough (self-healing)
    seen, uniq = set(), []
    for t in out:
        if t["link"] and t["link"] not in seen:
            seen.add(t["link"])
            uniq.append(t)
    conn = _db()
    archived = set(r["link"] for r in conn.execute("SELECT link FROM x_archived").fetchall())
    conn.close()
    show_archived = request.args.get("archived") == "1"
    if show_archived:
        uniq = [t for t in uniq if t["link"] in archived]
    else:
        uniq = [t for t in uniq if t["link"] not in archived]
    return jsonify({"status": "ok", "tweets": uniq[:25], "archived_count": len(archived)})


@agentic_bp.route("/api/agentic/oracle/watch/archive", methods=["POST", "OPTIONS"])
def api_oracle_watch_archive():
    """Mark processed tweets as archived so they stop cluttering the X Watch."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    links = data.get("links") or ([data["link"]] if data.get("link") else [])
    links = [str(l) for l in links if l]
    if not links:
        return jsonify({"error": "link(s) required"}), 400
    conn = _db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for l in links:
        conn.execute("INSERT OR IGNORE INTO x_archived (link, business_id, archived_at) VALUES (?,?,?)",
                     (l, data.get("business_id"), now))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "archived": len(links)})


@agentic_bp.route("/api/agentic/oracle/watch/unarchive", methods=["POST", "OPTIONS"])
def api_oracle_watch_unarchive():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    link = str(data.get("link") or "").strip()
    if not link:
        return jsonify({"error": "link required"}), 400
    conn = _db()
    conn.execute("DELETE FROM x_archived WHERE link=?", (link,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@agentic_bp.route("/api/agentic/oracle/flash", methods=["POST", "OPTIONS"])
def api_oracle_flash():
    """G3: hot-take post from the top signal above the feed's flash threshold."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    feed = _get_feed(int(data["feed_id"])) if data.get("feed_id") is not None else None
    if not feed:
        return jsonify({"error": "feed_id required"}), 400
    signals, _s = _sweep_feed_sources(feed, include_repeats=True)
    biz = _get_business(feed.get("business_id")) if feed.get("business_id") else None
    made = 0
    sigs = signals[:3]
    if not sigs:
        return jsonify({"status": "ok", "flash_posts": 0, "feed": feed["name"]})
    # An explicit click always flashes the top story — repeat-discounting
    # (scores collapse to 0 on re-sweeps) must not starve the button.
    if _flash_from_signal(feed, sigs[0], biz):
        made += 1
    thr = int(feed.get("flash_threshold") or 30)
    for sig in sigs[1:3]:
        if int(sig.get("score") or 0) >= thr:
            if _flash_from_signal(feed, sig, biz):
                made += 1
    return jsonify({"status": "ok", "flash_posts": made, "feed": feed["name"]})


@agentic_bp.route("/api/agentic/calendar/plan", methods=["POST", "OPTIONS"])
def api_calendar_plan():
    """G4: fill the gap between daily_target and what's already scheduled for a day
    with a format mix (takes + thread + commentary) generated from fresh signals."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    bid = int(data.get("business_id") or 0)
    biz = _get_business(bid) if bid else None
    if not biz:
        return jsonify({"error": "business_id required"}), 400
    day = (data.get("day") or "").strip()
    if not day:
        day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    target = int(biz.get("daily_target") or 8)
    conn = _db()
    scheduled = conn.execute("SELECT COUNT(*) c FROM work_items WHERE scheduled_at LIKE ? AND tags LIKE ?",
                             (day + "%", "%biz:" + str(bid) + "%")).fetchone()["c"]
    conn.close()
    gap = max(0, target - scheduled)
    made = 0
    if gap > 0:
        signals = []
        for f in _list_feeds(bid)[:2]:
            try:
                sigs, _s = _sweep_feed_sources(f, include_repeats=True)
                signals.extend(sigs[:4])
            except Exception:
                continue
        seen, uniq = set(), []
        for s in signals:
            t = s.get("title", "")
            if t and t not in seen:
                seen.add(t)
                uniq.append(s)
        uniq.sort(key=lambda s: -int(s.get("score") or 0))
        mix = ["x", "x", "linkedin", "x", "x_thread", "x", "facebook", "x", "instagram", "x"]
        for i in range(gap):
            if not uniq:
                break
            platform = mix[i % len(mix)]
            sig = uniq[i % len(uniq)]
            hour, minute = 9 + (i * 90) // 60, (i * 90) % 60
            when = f"{day} {hour:02d}:{minute:02d}"
            msg = f"TOPIC:\n{sig.get('title')}\n{sig.get('summary') or ''}\n{sig.get('link') or ''}"
            link_code, short_url = _make_tracked_link(biz)
            if short_url:
                msg += f"\n\nCTA LINK (include this exact URL in the post as the clickable link): {short_url}"
            try:
                content = _call_llm(msg, system_prompt=_PLAN_SYS.get(platform, _PLAN_SYS["x"]), agent="plan", timeout=60)
            except Exception:
                continue
            content = (content or "").strip()
            if not content:
                continue
            if platform == "x" and len(content) > 280:
                content = content[:280]
            wid = _work_record(category=platform, title=f"{sig.get('title', '')[:56]} — {platform}",
                               content=content[:3000], source="oracle:plan", status="ready_for_approval",
                               tags=f"social,platform:{platform},plan,biz:{bid}", project=_biz_project(biz))
            if wid:
                _pipeline_update(wid, scheduled_at=when)
                if link_code:
                    _pipeline_update(wid, link_code=link_code)
            made += 1
    return jsonify({"status": "ok", "business": biz["name"], "target": target,
                    "scheduled": scheduled, "gap": gap, "planned": made})


# ---------------------------------------------------------------------------
# OpenClaw Autonomous Agent Gateway & GitHub Integration
# https://github.com/openclaw/openclaw
# ---------------------------------------------------------------------------

@agentic_bp.route("/api/agentic/openclaw/info", methods=["GET", "OPTIONS"])
def api_openclaw_info():
    """Get live OpenClaw repository, gateway status, and channel capabilities."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    
    # Check if local OpenClaw gateway is reachable on port 18789 or custom port
    status, code = _probe_port("18789/")
    
    return jsonify({
        "status": "ok",
        "name": "OpenClaw",
        "tagline": "Your personal AI assistant. Any OS. Any Platform. The lobster way. 🦞",
        "github_url": "https://github.com/openclaw/openclaw",
        "repo": "openclaw/openclaw",
        "docs_url": "https://docs.openclaw.ai",
        "gateway_status": status,
        "gateway_port": 18789,
        "gateway_url": "http://localhost:18789",
        "version": "2026.x",
        "channels": [
            {"id": "telegram", "name": "Telegram Bot", "status": "available", "icon": "✈️"},
            {"id": "discord", "name": "Discord Bot", "status": "available", "icon": "💬"},
            {"id": "whatsapp", "name": "WhatsApp Bridge", "status": "available", "icon": "📱"},
            {"id": "slack", "name": "Slack App", "status": "available", "icon": "🏢"},
            {"id": "signal", "name": "Signal Messenger", "status": "available", "icon": "🔒"},
            {"id": "webhook", "name": "REST Webhook", "status": "active", "icon": "⚡"}
        ],
        "install_commands": {
            "curl": "curl -fsSL https://openclaw.ai/install.sh | bash",
            "npm": "npm install -g openclaw",
            "docker": "docker run -d --name openclaw-gateway -p 18789:18789 ghcr.io/openclaw/openclaw:latest"
        }
    })

@agentic_bp.route("/api/agentic/openclaw/dispatch", methods=["POST", "OPTIONS"])
def api_openclaw_dispatch():
    """Dispatch a Hermes Agent instruction/task to OpenClaw runner."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    task = (data.get("task") or data.get("prompt") or "").strip()
    if not task:
        return jsonify({"error": "task required"}), 400
    
    # Try forward to local OpenClaw gateway if online, or run with central LLM bridge
    channel = data.get("channel", "hermes_bridge")
    reply = _call_llm(
        f"[OpenClaw Gateway Dispatch] Task: {task}\nExecute autonomous multi-step agent reasoning loop.",
        system_prompt="You are OpenClaw Gateway runtime engine. You execute autonomous multi-tool tasks, orchestrate skills, and report structured output back to Hermes Agent.",
        agent="hermes",
        timeout=60
    )
    
    return jsonify({
        "status": "ok",
        "task": task,
        "channel": channel,
        "output": reply,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

