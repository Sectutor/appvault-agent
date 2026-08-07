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
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
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
        ts TEXT, agent TEXT, tag TEXT, content TEXT
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
    """)
    conn.commit()
    conn.close()

_init_db()

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

def _call_llm(user_msg, system_prompt=None, agent="hermes", timeout=25):
    """Single dispatch for every agent conversation + crew role. Real LLM call."""
    cfg = _get_llm_config()
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
        cur = conn.execute(
            "INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
            (data.get("timestamp") or datetime.now().strftime("%H:%M LOCAL"),
             data.get("agent", "System"), data.get("tag", "General"), content))
        conn.commit()
        row = conn.execute("SELECT * FROM memory WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        # Sync to vault inbox when present
        vault = _vault_path()
        inbox = os.path.join(vault, "01_Inbox")
        if os.path.isdir(inbox):
            try:
                with open(os.path.join(inbox, "Agentic_Memory_Feed.md"), "a", encoding="utf-8") as f:
                    f.write(f"\n### [{row['ts']}] {row['agent']} ({row['tag']})\n{row['content']}\n")
            except Exception:
                pass
        return jsonify({"status": "ok", "entry": dict(row)})
    conn = _db()
    rows = conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"status": "ok", "memory": [dict(r) for r in rows]})

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
    conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                 (datetime.now().strftime("%H:%M LOCAL"), "Hermes Oracle Core", "Radar Signal",
                  f"Live sweep completed for '{query}'. Generated `{os.path.basename(signal_file) if signal_file else 'in-memory'}` with {len(stories)} signals."))
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
            reply = _call_llm(user_msg, agent=agent_id)
        except Exception as e:
            reply = (f"⚠️ {agent_name} could not reach any LLM backend. Configure one at "
                     f"`/api/agentic/config` (or the ⚙️ LLM Settings drawer). Detail: {str(e)[:200]}")
        messages.append({"sender": agent_name, "role": "agent",
                         "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": reply})
        _save_conversation(agent_id, messages)

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
                reply = _call_llm(user_msg, agent="hermes")
            except Exception as e:
                reply = f"⚠️ Hermes could not reach any LLM backend. Detail: {str(e)[:200]}"
            sess["messages"].append({"sender": "Hermes Agent", "role": "agent",
                                     "timestamp": datetime.now().strftime("%H:%M LOCAL"), "text": reply})
            _save_session(session_id, sess["title"], sess["messages"])
            conn = _db()
            conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), "Hermes Agent", "Conversation",
                          f"User: {user_msg[:60]} | Reply: {reply[:60]}"))
            conn.commit()
            conn.close()
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
            reply = _call_llm(user_msg, agent="hermes")
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
            conn.execute("INSERT INTO memory (ts, agent, tag, content) VALUES (?,?,?,?)",
                         (datetime.now().strftime("%H:%M LOCAL"), "Hermes Agent", "Conversation",
                          f"User: {user_msg[:60]} | Reply: {reply[:60]}"))
            conn.commit()
            conn.close()
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
    
    vault_base = os.environ.get("OBSIDIAN_VAULT_PATH", "/vault" if os.path.exists("/vault") else "D:/ObsidianVault")
    if not os.path.exists(vault_base) and os.path.exists("D:/ObsidianVault"):
        vault_base = "D:/ObsidianVault"
        
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
        
    vault_base = os.environ.get("OBSIDIAN_VAULT_PATH", "/vault" if os.path.exists("/vault") else "D:/ObsidianVault")
    if not os.path.exists(vault_base) and os.path.exists("D:/ObsidianVault"):
        vault_base = "D:/ObsidianVault"
        
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

