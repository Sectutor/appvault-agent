# ---------------------------------------------------------------------------
# NAV CONFIG (2026-08-09) — server-driven sidebar overrides (Phase 3)
# hide sections/items, reorder sections/items, pinned defaults. Zero code edits.
# ---------------------------------------------------------------------------
NAV_CONFIG_DEFAULTS = {
    "hidden_items": [],
    "hidden_sections": [],
    "section_order": [],
    "item_order": {},
    "pinned_defaults": [],
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
# ---------------------------------------------------------------------------
