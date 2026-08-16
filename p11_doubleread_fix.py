#!/usr/bin/env python3
"""Fix: verify_build/ship double-read — _read_dep_result already returns content."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()

    old1 = """def _mission_verify_build(mission, task, ctx):
    ref = _read_dep_result(task) or ""
    code = _read_ref(ref) or ""
    if not code:
        return (False, None, "no artifact to verify")"""
    new1 = """def _mission_verify_build(mission, task, ctx):
    code = _read_dep_result(task) or ""
    if not code:
        return (False, None, "no artifact to verify")"""
    assert old1 in src, 'verify anchor missing'
    src = src.replace(old1, new1, 1)

    old2 = """def _mission_ship(mission, task, ctx):
    ref = _read_dep_result(task) or ""
    code = _read_ref(ref) or ""
    slug = _mission_slug(mission.get("title") or "artifact")"""
    new2 = """def _mission_ship(mission, task, ctx):
    code = _read_dep_result(task) or ""
    slug = _mission_slug(mission.get("title") or "artifact")"""
    assert old2 in src, 'ship anchor missing'
    src = src.replace(old2, new2, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('double-read fixed + re-spliced + ast.parse OK')

if __name__ == '__main__':
    main()
