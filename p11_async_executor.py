#!/usr/bin/env python3
"""Mission Loop v3b: async executor — tasks run in daemon threads so one slow
LLM call never blocks the tick or the other missions."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()

    # 1. _mission_run_task helper (before _mission_tick)
    old_tick = 'def _mission_tick():\n    """Executor tick: advance each active mission by one task. Returns event strings."""\n    out = []'
    assert old_tick in src, 'tick anchor missing'
    helper = """def _mission_run_task(mission, task):
    \"\"\"Run one task and persist the outcome (daemon thread).\"\"\"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        ok, ref, err = _mission_execute(mission, task)
    except Exception as e:
        ok, ref, err = False, None, str(e)[:300]
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


"""
    src = src.replace(old_tick, helper + old_tick, 1)

    # 2. async dispatch inside the tick (replace the sync execution block)
    old_exec = """            conn.execute("UPDATE mission_tasks SET status='running', updated=? WHERE id=?", (now, chosen["id"]))
            conn.commit()
            conn.close()
            ok, ref, err = _mission_execute(mdict, chosen)
            conn = _db()
            if ok:
                conn.execute("UPDATE mission_tasks SET status='verified', verified=1, result_ref=?, last_error=NULL, updated=? WHERE id=?",
                             (ref, now, chosen["id"]))
                out.append("M%d T%d ok" % (m["id"], chosen["id"]))
            else:
                attempts = chosen["attempts"] + 1
                if attempts >= 3:
                    conn.execute("UPDATE mission_tasks SET status='blocked', attempts=?, last_error=?, updated=? WHERE id=?",
                                 (attempts, str(err)[:300], now, chosen["id"]))
                    out.append("M%d T%d blocked: %s" % (m["id"], chosen["id"], str(err)[:80]))
                else:
                    conn.execute("UPDATE mission_tasks SET status='queued', attempts=?, last_error=?, updated=? WHERE id=?",
                                 (attempts, str(err)[:300], now, chosen["id"]))
                    out.append("M%d T%d retry(%d)" % (m["id"], chosen["id"], attempts))
            conn.commit()
            conn.close()
            conn = _db()"""
    new_exec = """            conn.execute("UPDATE mission_tasks SET status='running', updated=? WHERE id=?", (now, chosen["id"]))
            conn.commit()
            conn.close()
            # async: run in a daemon thread so the tick never blocks on LLM calls
            try:
                threading.Thread(target=_mission_run_task, args=(mdict, chosen), daemon=True).start()
            except Exception:
                _mission_run_task(mdict, chosen)
            out.append("M%d T%d started" % (m["id"], chosen["id"]))
            conn = _db()"""
    assert old_exec in src, 'sync exec block missing'
    src = src.replace(old_exec, new_exec, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)

    # re-splice
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('async executor spliced + ast.parse OK')

if __name__ == '__main__':
    main()
