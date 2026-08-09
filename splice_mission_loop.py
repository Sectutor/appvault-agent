#!/usr/bin/env python3
"""Append the MISSION LOOP block to agentic_plane.py (end of file). Idempotent."""
import io, ast

PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'
BLOCK = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/mission_loop_block.py'
MARKER = 'MISSION LOOP (2026-08-09)'

def main():
    src = io.open(PLANE, 'r', encoding='utf-8').read()
    if MARKER in src:
        print('ALREADY APPLIED')
        return
    block = io.open(BLOCK, 'r', encoding='utf-8').read()
    combined = src.rstrip('\n') + '\n\n' + block
    ast.parse(combined)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(combined)
    print('MISSION LOOP spliced + ast.parse OK')

if __name__ == '__main__':
    main()
