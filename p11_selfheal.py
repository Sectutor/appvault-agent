#!/usr/bin/env python3
"""Self-heal: sync stale 'running'+verified tasks; protect worker DB writes."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()

    # 1. _mission_run_task: wrap DB writes in try/except
    old = """    conn = _db()
    if ok:
        conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                     (ref, now, task["id"]))
        conn.commit()
        conn.close()
        return
    attempts = (task.get("attempts") or 0) + 1
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
    new = """    try:
        conn = _db()
        if ok:
            conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                         (ref, now, task["id"]))
            conn.commit()
            conn.close()
            return
        attempts = (task.get("attempts") or 0) + 1
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
        return out_note
    except Exception as e:
        try:
            conn = _db()
            conn.execute("UPDATE mission_tasks SET last_error=?, updated=? WHERE id=?",
                         ("worker-db-error: %s" % str(e)[:200], now, task["id"]))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return None"""
    assert old in src, 'run_task db block missing'
    src = src.replace(old, new, 1)

    # 2. tick: self-heal stale running+verified
    old2 = """            for t in tasks:
                tdict = dict(t)
                if tdict["status"] != "queued":
                    continue"""
    new2 = """            for t in tasks:
                tdict = dict(t)
                # self-heal: outcome landed but status went stale (thread race)
                if tdict["status"] == "running" and tdict["verified"]:
                    conn.execute("UPDATE mission_tasks SET status='verified', updated=? WHERE id=?", (now, tdict["id"]))
                    conn.commit()
                    out.append("M%d T%d healed" % (m["id"], tdict["id"]))
                    continue
                if tdict["status"] != "queued":
                    continue"""
    assert old2 in src, 'pick loop anchor missing'
    src = src.replace(old2, new2, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('self-heal + worker db-guard spliced + ast.parse OK')

if __name__ == '__main__':
    main()
