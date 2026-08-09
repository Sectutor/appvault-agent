#!/usr/bin/env python3
"""Stale-run recovery: re-queue tasks stuck in 'running' longer than 15 min."""
import io, ast

PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(PLANE, 'r', encoding='utf-8').read()
    old = """        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for m in missions:
            mdict = dict(m)
            tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (m["id"],)).fetchall()"""
    new = """        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stale_cutoff = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        # recovery: a task stuck in 'running' (crashed/hung execution) is re-queued
        conn.execute("UPDATE mission_tasks SET status='queued', updated=? WHERE status='running' AND updated < ?",
                     (now, stale_cutoff))
        conn.commit()
        for m in missions:
            mdict = dict(m)
            tasks = conn.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (m["id"],)).fetchall()"""
    if 'stale_cutoff' in src:
        print('already applied')
        return
    assert old in src, 'anchor missing'
    src = src.replace(old, new, 1)
    ast.parse(src)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(src)
    print('stale-running recovery added + ast.parse OK')

if __name__ == '__main__':
    main()
