#!/usr/bin/env python3
"""Backend: extend nav-config whitelist with expanded_defaults + groups."""
import io, ast

PLANE = 'D:/DATA_INTELLFENCE/WebDev/AppVault/agent/agentic_plane.py'

def main():
    src = io.open(PLANE, 'r', encoding='utf-8').read()
    old = '''NAV_CONFIG_DEFAULTS = {
    "hidden_items": [],
    "hidden_sections": [],
    "section_order": [],
    "item_order": {},
    "pinned_defaults": [],
}'''
    new = '''NAV_CONFIG_DEFAULTS = {
    "hidden_items": [],
    "hidden_sections": [],
    "section_order": [],
    "item_order": {},
    "pinned_defaults": [],
    "expanded_defaults": [],
    "groups": {},
}'''
    if 'expanded_defaults' in src:
        print('already applied')
        return
    assert old in src, 'NAV_CONFIG_DEFAULTS anchor missing'
    src = src.replace(old, new, 1)
    ast.parse(src)
    io.open(PLANE, 'w', encoding='utf-8', newline='\n').write(src)
    print('backend defaults extended + ast.parse OK')

if __name__ == '__main__':
    main()
