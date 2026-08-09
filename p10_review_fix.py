#!/usr/bin/env python3
"""Fix: sqlite3.Row has no .get() — review loop used t.get('last_error')."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    old = """        lines = ["# Mission review: %s" % mission.get("title", "")]
        for t in tasks:
            mark = "✅" if t["verified"] else ("⚠️" if t["status"] == "blocked" else "⏳")
            lines.append("%s %s (%s)%s" % (mark, t["title"], t["status"],
                                           (" — " + str(t["last_error"])) if t.get("last_error") else ""))"""
    new = """        lines = ["# Mission review: %s" % mission.get("title", "")]
        for t in [dict(x) for x in tasks]:
            mark = "✅" if t["verified"] else ("⚠️" if t["status"] == "blocked" else "⏳")
            lines.append("%s %s (%s)%s" % (mark, t["title"], t["status"],
                                           (" — " + str(t.get("last_error"))) if t.get("last_error") else ""))"""
    assert old in src, 'anchor missing'
    src = src.replace(old, new, 1)
    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)

    # re-splice into the plane
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('fixed + re-spliced + ast.parse OK')

if __name__ == '__main__':
    main()
