#!/usr/bin/env python3
"""Self-correction: failed verify_build/qa re-queues the producer task (regenerate)."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    old = """        attempts = (task.get("attempts") or 0) + 1
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
        return out_note"""
    new = """        attempts = (task.get("attempts") or 0) + 1
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
        return out_note"""
    assert old in src, 'failure branch anchor missing'
    src = src.replace(old, new, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('self-correction loop spliced + ast.parse OK')

if __name__ == '__main__':
    main()
