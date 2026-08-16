#!/usr/bin/env python3
"""Auto-unblock: when a producer (build/draft) verifies, re-queue its blocked verifier."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    old = """        if ok:
            conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                         (ref, now, task["id"]))
            conn.commit()
            conn.close()
            return"""
    new = """        if ok:
            conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                         (ref, now, task["id"]))
            # a verified producer un-blocks its verifier so the new artifact gets checked
            if task.get("task_type") in ("build", "draft"):
                conn.execute("UPDATE mission_tasks SET status='queued', attempts=0, last_error=NULL, updated=? "
                             "WHERE mission_id=? AND task_type IN ('verify_build','qa') AND status='blocked'",
                             (now, task.get("mission_id")))
            conn.commit()
            conn.close()
            return"""
    assert old in src, 'verified branch anchor missing'
    src = src.replace(old, new, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('auto-unblock spliced + ast.parse OK')

if __name__ == '__main__':
    main()
