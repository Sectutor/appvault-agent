#!/usr/bin/env python3
"""Replace the mission block tail of agentic_plane.py with the updated block."""
import io, ast

PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'
BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
MARKER = '# MISSION LOOP (2026-08-09)'

def main():
    src = io.open(PLANE, 'r', encoding='utf-8').read()
    block = io.open(BLOCK, 'r', encoding='utf-8').read()
    idx = src.find(MARKER)
    assert idx > 0, 'mission marker not found'
    # find the section header line start
    hdr = src.rfind('# ---', 0, idx)
    new_src = src[:hdr].rstrip('\n') + '\n\n' + block
    ast.parse(new_src)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(new_src)
    print('mission block replaced + ast.parse OK')

if __name__ == '__main__':
    main()
