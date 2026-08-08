"""Splice agentic_compounding.py into agentic_plane.py (append + marker guard)."""
import ast
import sys

plane_path = r"D:\DATA_INTELLFENCE\WebDev\AppVault\agent\agentic_plane.py"
extra_path = r"D:\DATA_INTELLFENCE\WebDev\AppVault\agent\agentic_compounding.py"
MARKER = "# COMPOUNDING LAYER (2026-08-08)"

src = open(plane_path, encoding="utf-8").read()
extra = open(extra_path, encoding="utf-8").read()

if MARKER in src:
    print("ALREADY SPLICED — skipping")
    sys.exit(0)

# sanity: extra file must parse standalone
ast.parse(extra)

merged = src.rstrip() + "\n\n\n" + extra + "\n"
ast.parse(merged)  # parse BEFORE writing (write-then-parse footgun rule)

with open(plane_path, "w", encoding="utf-8", newline="") as f:
    f.write(merged)

print(f"OK — appended {len(extra.splitlines())} lines; total {len(merged.splitlines())} lines; ast.parse clean")
