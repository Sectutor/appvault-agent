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
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
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
    sys_prompt = system_prompt or cfg.get("system_prompt") or AGENT_PROMPTS.get(agent, DEFAULT_LLM_CONFIG["system_prompt"])
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
        messages = _get_conversation(agent_id)
        messages.append({"sender": "User", "role": "user",
                         "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": user_msg})
        agent_name = "Hermes Agent" if agent_id == "hermes" else f"{agent_id.capitalize()} Agent"
        try:
            ctx = _memory_context(user_msg)
            reply = _call_llm(user_msg + (("\n\n" + ctx) if ctx else ""), agent=agent_id)

        except Exception as e:
            reply = (f"⚠️ {agent_name} could not reach any LLM backend. Configure one at "
                     f"`/api/agentic/config` (or the ⚙️ LLM Settings drawer). Detail: {str(e)[:200]}")
        messages.append({"sender": agent_name, "role": "agent",
                         "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": reply})
        _save_conversation(agent_id, messages)
        threading.Thread(target=_distill_facts, args=(user_msg, reply, agent_name), daemon=True).start()

        conn = _db()
        conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                     (datetime.now().strftime("%H:%M LOCAL"), agent_name, "Conversation",
                      f"User: {user_msg[:60]} | Reply: {reply[:60]}"))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "agent_id": agent_id, "conversation": messages})
    return jsonify({"status": "ok", "agent_id": agent_id, "conversation": _get_conversation(agent_id)})

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
            try:
                ctx = _memory_context(user_msg)
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
        try:
            ctx = _memory_context(user_msg)
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

