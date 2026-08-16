#!/usr/bin/env python3
"""Adaptive regeneration: build executor gets the previous verify error as feedback."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    old = """def _mission_build(mission, task, ctx):
    spec = _read_dep_result(task) or ""
    topic = mission.get("title") or "tool"
    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\\n"
              "Write the COMPLETE, syntactically valid Python 3 script implementing this spec. "
              "Output ONLY the code (no markdown fences, no explanation).\\n\\nSpec:\\n%s"
              % (ctx["voice"], spec[:3000]))"""
    new = """def _mission_build(mission, task, ctx):
    spec = _read_dep_result(task) or ""
    topic = mission.get("title") or "tool"
    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\\n"
              "Write the COMPLETE, syntactically valid Python 3 script implementing this spec. "
              "Output ONLY the code (no markdown fences, no explanation).\\n\\nSpec:\\n%s"
              % (ctx["voice"], spec[:3000]))
    # adaptive feedback: previous verification failure
    try:
        conn = _db()
        v = conn.execute(
            "SELECT last_error FROM mission_tasks WHERE mission_id=? AND task_type='verify_build' "
            "AND last_error IS NOT NULL AND last_error LIKE '%SYNTAX%' ORDER BY id DESC LIMIT 1",
            (mission.get("id"),)).fetchone()
        conn.close()
        if v and v["last_error"]:
            prompt += ("\\n\\nIMPORTANT: your previous attempt was REJECTED by the compiler with:\\n%s\\n"
                       "Fix that exact issue. Output a single, self-contained, compilable Python file." % v["last_error"])
    except Exception:
        pass"""
    assert old in src, 'build anchor missing'
    src = src.replace(old, new, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('adaptive feedback spliced + ast.parse OK')

if __name__ == '__main__':
    main()
