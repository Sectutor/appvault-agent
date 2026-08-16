#!/usr/bin/env python3
"""Fix ship: read the BUILD artifact (by task type), not the verify summary."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    old = """def _mission_ship(mission, task, ctx):
    code = _read_dep_result(task) or ""
    slug = _mission_slug(mission.get("title") or "artifact")"""
    new = """def _mission_ship(mission, task, ctx):
    build_ref = _mission_result_by_type(mission.get("id"), "build")
    code = _read_ref(build_ref) or ""
    if not code:
        return (False, None, "no build artifact found to ship")
    slug = _mission_slug(mission.get("title") or "artifact")"""
    assert old in src, 'ship anchor missing'
    src = src.replace(old, new, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('ship fixed + re-spliced + ast.parse OK')

if __name__ == '__main__':
    main()
