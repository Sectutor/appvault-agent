#!/usr/bin/env python3
"""Splice NAV CONFIG block into agentic_plane.py. Idempotent (marker-guarded)."""
import io, ast

PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'
BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/nav_config_block.py'
MARKER = 'NAV CONFIG (2026-08-09)'

def read(p):
    with io.open(p, 'r', encoding='utf-8') as f:
        return f.read()

def main():
    src = read(PLANE)
    if MARKER in src:
        print('ALREADY APPLIED — skipping')
        return
    block = read(BLOCK)
    # strip the fence comment lines (kept for readability in the file itself)
    body = '\n'.join(l for l in block.split('\n') if not l.startswith('# ---'))
    anchor = '    return jsonify({"status": "ok", "settings": _memory_settings()})\n\n\n@agentic_bp.route("/api/agentic/memory/<int:mid>/tier", methods=["POST", "OPTIONS"])'
    assert anchor in src, 'anchor missing'
    src = src.replace(anchor, '    return jsonify({"status": "ok", "settings": _memory_settings()})\n\n' + body + '\n\n@agentic_bp.route("/api/agentic/memory/<int:mid>/tier", methods=["POST", "OPTIONS"])', 1)
    ast.parse(src)  # parse in memory BEFORE writing
    with io.open(PLANE, 'w', encoding='utf-8', newline='\n') as f:
        f.write(src)
    print('SPLICED + ast.parse OK')

if __name__ == '__main__':
    main()
