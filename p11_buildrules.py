#!/usr/bin/env python3
"""Builder instructions: ban from __future__ imports (recurring LLM quirk)."""
import io, ast

BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(BLOCK, 'r', encoding='utf-8').read()
    old = """    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\\n"
              "Write the COMPLETE, syntactically valid Python 3 script implementing this spec. "
              "Output ONLY the code (no markdown fences, no explanation).\\n\\nSpec:\\n%s"
              % (ctx["voice"], spec[:3000]))"""
    new = """    prompt = ("You are the engineer arm of an autonomous business agent. Voice: %s\\n"
              "Write the COMPLETE, syntactically valid Python 3 script implementing this spec. "
              "Rules: output ONLY code (no markdown fences, no explanation); a single self-contained file; "
              "no comments naming other files; NO 'from __future__' imports (the compiler rejects them here).\\n\\nSpec:\\n%s"
              % (ctx["voice"], spec[:3000]))"""
    assert old in src, 'build prompt anchor missing'
    src = src.replace(old, new, 1)

    old2 = """            prompt += ("\\n\\nIMPORTANT: your previous attempt was REJECTED by the compiler with:\\n%s\\n"
                       "Fix that exact issue. Output a single, self-contained, compilable Python file." % v["last_error"])"""
    new2 = """            prompt += ("\\n\\nIMPORTANT: your previous attempt was REJECTED by the compiler with:\\n%s\\n"
                       "Fix that exact issue, and remember: NO 'from __future__' imports, single file, "
                       "output only code." % v["last_error"])"""
    assert old2 in src, 'feedback anchor missing'
    src = src.replace(old2, new2, 1)

    io.open(BLOCK, 'w', encoding='utf-8', newline='\n').write(src)
    plane = io.open(PLANE, 'r', encoding='utf-8').read()
    idx = plane.find('# MISSION LOOP (2026-08-09)')
    assert idx > 0
    hdr = plane.rfind('# ---', 0, idx)
    new_plane = plane[:hdr].rstrip('\n') + '\n\n' + src
    ast.parse(new_plane)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_plane)
    print('builder rules updated + re-spliced + ast.parse OK')

if __name__ == '__main__':
    main()
