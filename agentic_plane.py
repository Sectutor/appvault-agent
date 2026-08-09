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
    """stdlib HTTP helper (the agent image has NO requests module)."""
    data = None
    hdrs = {"User-Agent": "AppVault-Agent/1.0"}
    if headers:
        hdrs.update(headers)
    if json_data is not None:
        data = json.dumps(json_data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body), resp.status
            except Exception:
                return {"raw": body[:2000]}, resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace")), e.code
        except Exception:
            return {"error": f"HTTP {e.code}"}, e.code
    except Exception as e:
        return {"error": str(e)}, 0

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
    sys_prompt = system_prompt or cfg.get("system_prompt") or _get_agent_prompt(agent)
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
FEEDS = [
    ("Google News AI", "https://news.google.com/rss/search?q=AI+agent&hl=en-US&gl=US&ceid=US:en"),
    ("Google News LLM", "https://news.google.com/rss/search?q=LLM+model&hl=en-US&gl=US&ceid=US:en"),
    ("HN LLM", "https://hnrss.org/newest?q=LLM"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("BBC Tech", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
]

KEYWORDS = {
    "agent": 5, "ai": 3, "llm": 5, "model": 2, "openai": 5, "anthropic": 5, "google": 3,
    "xai": 4, "grok": 4, "nvidia": 3, "microsoft": 3, "meta": 3, "startup": 2, "funding": 2,
    "regulation": 3, "safety": 3, "robot": 3, "research": 2, "chip": 3, "semiconductor": 3,
    "wordpress": 3, "autonomous": 3, "reasoning": 2, "open source": 2, "open-source": 2,
}

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
    for kw, w in KEYWORDS.items():
        if kw in text:
            score += w
    return min(100, score * 7 + (10 if any(c.isdigit() for c in title) else 0))

def _sweep_feeds(limit=5):
    stories = []
    for name, url in FEEDS:
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
    if tier:
        rows = conn.execute("SELECT * FROM memory WHERE tier=? ORDER BY id DESC LIMIT 60", (tier,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT 60").fetchall()
    conn.close()
    return jsonify({"status": "ok", "memory": [dict(r) for r in rows]})

@agentic_bp.route("/api/agentic/memory/<int:mid>", methods=["DELETE", "OPTIONS"])
def api_memory_delete(mid):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
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
    conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                 (datetime.now().strftime("%H:%M LOCAL"), "Hermes Oracle Core", "Radar Signal",
                  f"Live sweep for '{query}' found {len(stories)} signals -> `{os.path.basename(signal_file) if signal_file else 'in-memory'}`:\n{top_titles}"))
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
    return {
        "rss_urls": [u for _, u in FEEDS],
        "subreddits": ["artificial", "LocalLLaMA", "MachineLearning"],
        "hn_query": "AI agent",
        "github_query": "ai agent framework",
        "youtube_channels": [],
        "sources": ["rss", "reddit", "hn", "github", "youtube"],
        "skip_repeats": 1,
    }

def _feed_row_to_dict(r):
    try:
        sources = json.loads(r["sources"] or "[]") if r["sources"] else []
    except Exception:
        sources = []
    if not sources:
        sources = _feed_defaults()["sources"]
    return {
        "id": r["id"], "name": r["name"], "query": r["query"],
        "rss_urls": json.loads(r["rss_urls"] or "[]"),
        "subreddits": json.loads(r["subreddits"] or "[]"),
        "hn_query": r["hn_query"] or "", "github_query": r["github_query"] or "",
        "youtube_channels": json.loads(r["youtube_channels"] or "[]"),
        "sources": sources,
        "skip_repeats": bool(r["skip_repeats"]) if r["skip_repeats"] is not None else True,
        "created": r["created"],
    }

def _list_feeds():
    conn = _db()
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


def _sweep_feed_sources(feed):
    """Parallel multi-source sweep, engagement-weighted scores, dedup/momentum.
    Returns (stories, source_stats). Each source runs in its own thread."""
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
    if "rss" in enabled: jobs.append(threading.Thread(target=_rss))
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
            if is_repeat and skip_repeats and delta < 100:
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
        return jsonify({"status": "ok", "feeds": _list_feeds()})
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
    cur = conn.execute(
        "INSERT INTO oracle_feeds (name, query, rss_urls, subreddits, hn_query, github_query, youtube_channels, sources, skip_repeats, created)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, (data.get("query") or name).strip(),
         json.dumps(_arr(data.get("rss_urls"))), json.dumps(_arr(data.get("subreddits"))),
         (data.get("hn_query") or "").strip(), (data.get("github_query") or "").strip(),
         json.dumps(_arr(data.get("youtube_channels"))),
         json.dumps(_arr(data.get("sources")) or _feed_defaults()["sources"]),
         1 if data.get("skip_repeats", True) else 0,
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
    srcs = _arr(data.get("sources")) if data.get("sources") is not None else (json.loads(row["sources"] or "[]") if row["sources"] else _feed_defaults()["sources"])
    if not srcs:
        srcs = _feed_defaults()["sources"]
    skip = int(data.get("skip_repeats", row["skip_repeats"] if row["skip_repeats"] is not None else 1) is True or data.get("skip_repeats") == 1 or (row["skip_repeats"] and data.get("skip_repeats") is None))
    conn.execute(
        "UPDATE oracle_feeds SET name=?, query=?, rss_urls=?, subreddits=?, hn_query=?, github_query=?, youtube_channels=?, sources=?, skip_repeats=?"
        " WHERE id=?",
        (name, query, json.dumps(rss), json.dumps(subs), hn, gh, json.dumps(yt), json.dumps(srcs), skip, feed_id))
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
            "query": data.get("query") or "AI agent orchestration & LLM frameworks",
            "rss_urls": data.get("rss_urls") or [u for _, u in FEEDS],
            "subreddits": data.get("subreddits") or _feed_defaults()["subreddits"],
            "hn_query": data.get("hn_query") or "AI agent",
            "github_query": data.get("github_query") or "ai agent framework",
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
    if platform not in ("linkedin", "x", "blog"):
        return jsonify({"error": "platform must be linkedin|x|blog"}), 400
    feed = _get_feed(int(data["feed_id"])) if data.get("feed_id") is not None else None
    if not feed:
        return jsonify({"error": "feed_id required"}), 400
    signals = _sweep_feed_sources(feed)

    sig_lines = "\n".join(
        f"- {s.get('title','')} [{s.get('source','')} | score {s.get('score',0)}] {s.get('link','')}"
        for s in signals[:6])

    if platform == "x":
        sys_prompt = ("You write X/Twitter posts about AI. Output ONLY the post text (max 280 chars), "
                      "no preamble, no hashtag spam. Hook + one sharp insight from the signals.")
    elif platform == "blog":
        sys_prompt = ("You are a tech journalist. Write a 350-500 word blog article in markdown with a title "
                      "(# Heading), an intro, 2-3 sections with real substance drawn from the signals, and a "
                      "conclusion. Cite the source links inline.")
    else:
        sys_prompt = ("You are a LinkedIn content strategist for an AI tools company. Write a professional "
                      "LinkedIn post (200-320 words) with: a bold hook line, 3 concrete takeaways from the "
                      "signals, and a question to drive comments. Plain text, short paragraphs, no emoji "
                      "overuse, no hashtag spam. Output ONLY the post body.")

    try:
        content = _call_llm(
            f"Feed topic: {feed['query']}\n\nTop research signals (last 30 days):\n{sig_lines}\n\n"
            f"Write the {platform} post now.", system_prompt=sys_prompt, agent="oracle", timeout=60)
    except Exception as e:
        return jsonify({"status": "error", "error": f"LLM generation failed: {str(e)[:200]}"}), 502

    title = signals[0]["title"][:80] if signals else feed["name"]
    conn = _db()
    cur = conn.execute(
        "INSERT INTO oracle_posts (feed_id, platform, title, content, status, created) VALUES (?,?,?,?,?,?)",
        (feed["id"], platform, title, content, "draft", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return jsonify({"status": "ok", "post_id": post_id, "platform": platform,
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
    """Dispatch a crew: 3 REAL per-role LLM calls, results collected + logged."""
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    data = request.get_json() or {}
    crew_name = data.get("crew", "Full-Stack Dev Crew")
    task = data.get("task", "Audit & refactor codebase for memory efficiency")
    job_id = f"job-{int(time.time())}"

    roles = [("Architect", "crew-architect"), ("Lead Engineer", "crew-engineer"), ("Code Reviewer", "crew-reviewer")]
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

def _get_conversation(agent_id):
    conn = _db()
    row = conn.execute("SELECT messages FROM conversations WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    return json.loads(row["messages"]) if row else [
        {"sender": f"{agent_id.capitalize()} Agent", "role": "agent",
         "timestamp": "NOW", "text": f"Agent **{agent_id.capitalize()}** online. Connected to the Agentic OS control plane."}
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
]


@agentic_bp.route("/api/agentic/crews", methods=["GET", "OPTIONS"])
def api_crews_presets():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok", "crews": CREW_PRESETS})


def _dispatch_crew(crew_name, task, roles=None):
    """Run a crew: N real per-role LLM calls. Shared by /crew and pipelines."""
    if not roles:
        roles = [("Architect", "crew-architect"), ("Lead Engineer", "crew-engineer"), ("Code Reviewer", "crew-reviewer")]
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
    """ )
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
    if row:
        try:
            return json.loads(row["messages"])
        except Exception:
            pass
    conn = _db()
    row = conn.execute("SELECT messages FROM conversations WHERE agent_id=?", (agent_id,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["messages"])
        except Exception:
            pass
    return [{"sender": f"{agent_id.capitalize()} Agent", "role": "agent",
             "timestamp": "NOW",
             "text": f"Agent **{agent_id.capitalize()}** online. Connected to the Agentic OS control plane."}]


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
                 status="draft", url="", tags="", wid=None):
    try:
        import uuid as _uuid
        wid = wid or _uuid.uuid4().hex[:12]
        conn = _db()
        conn.execute("""INSERT OR IGNORE INTO work_items
            (id, category, title, content, image_url, source, status, url, tags, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))""",
            (wid, category, title[:4000], content or "", image_url or "", source,
             status, url or "", tags or ""))
        conn.commit(); conn.close()
        return wid
    except Exception:
        return None

def _work_seed_if_empty():
    """First-run backfill so the Completed Work page isn't empty: vault outputs
    (research/article drafts) + anything already published via WordPress."""
    try:
        conn = _db()
        n = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        if n > 0:
            conn.close(); return 0
        seeded = 0
        # vault outputs -> research/article drafts
        vault = _vault_path()
        for sub in ("04_Projects/Outputs", "02_Agent_Logs"):
            d = os.path.join(vault, sub)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d))[:12]:
                if not f.lower().endswith(".md"):
                    continue
                p = os.path.join(d, f)
                try:
                    body = open(p, encoding="utf-8", errors="replace").read()[:3000]
                except Exception:
                    continue
                _work_record(category="research", title=f.replace(".md", "").replace("_", " ")[:120],
                             content=body, source="backfill", status="draft",
                             tags=sub.split("/")[-1], wid="seed-" + f[:24])
                seeded += 1
        conn.commit(); conn.close()
        return seeded
    except Exception:
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
        wid = _work_record(category=(data.get("category") or "other"),
                           title=title, content=data.get("content") or "",
                           image_url=data.get("image_url") or "",
                           source=data.get("source") or "manual",
                           status=data.get("status") or "draft",
                           tags=data.get("tags") or "")
        _audit("store", "work.add", f"{title[:60]}")
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

@agentic_bp.route("/api/agentic/work/<wid>", methods=["GET", "DELETE", "OPTIONS"])
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
