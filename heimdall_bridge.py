"""
Heimdall Bridge — auto-configures Heimdall to integrate with AppVault agent.
- Injects custom.js into Heimdall's database
- Adds tiles for installed apps
"""

import os
import sqlite3
import json
import urllib.request
from datetime import datetime

HEIMDALL_DB = os.getenv("HEIMDALL_DB_PATH", "/heimdall-config/www/app.sqlite")
CUSTOM_JS_URL = os.getenv("CUSTOM_JS_URL", "http://localhost:8086/custom.js")

def setup_heimdall_custom_js():
    """Inject the custom.js script tag into Heimdall's Custom JavaScript setting."""
    if not os.path.exists(HEIMDALL_DB):
        print(f"[heimdall] DB not found at {HEIMDALL_DB}, skipping")
        return False
    
    try:
        conn = sqlite3.connect(HEIMDALL_DB)
        cur = conn.cursor()
        
        # Check if settings table exists
        tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        
        if "settings" not in table_names:
            print(f"[heimdall] No 'settings' table found")
            conn.close()
            return False
        
        # Read current custom JS
        cur.execute("SELECT value FROM settings WHERE key='custom_js'")
        row = cur.fetchone()
        
        script_tag = f'<script src="{CUSTOM_JS_URL}"></script>'
        
        if row:
            current = row[0]
            if script_tag not in current:
                # Append to existing custom JS
                new_js = current + "\n" + script_tag
                cur.execute("UPDATE settings SET value=? WHERE key='custom_js'", (new_js,))
                print(f"[heimdall] Custom JS updated (appended script tag)")
            else:
                print(f"[heimdall] Custom JS already configured")
        else:
            # Insert new setting
            cur.execute("INSERT INTO settings (key, value) VALUES ('custom_js', ?)", (script_tag,))
            print(f"[heimdall] Custom JS setting created")
        
        conn.commit()
        conn.close()
        return True
        
    except Exception as e:
        print(f"[heimdall] Setup error: {e}")
        return False

def add_heimdall_tile(name, url, app_id=None, description=""):
    """Add a tile to Heimdall dashboard."""
    if not os.path.exists(HEIMDALL_DB):
        return False
    
    try:
        conn = sqlite3.connect(HEIMDALL_DB)
        cur = conn.cursor()
        
        # Check if tile already exists
        cur.execute("SELECT id FROM items WHERE url=? AND deleted_at IS NULL", (url,))
        if cur.fetchone():
            conn.close()
            return True  # Already exists
        
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        
        # Add to items table
        cur.execute("""
            INSERT INTO items (title, url, description, appid, class, icon, pinned, created_at, updated_at, type, user_id)
            VALUES (?, ?, ?, ?, NULL, NULL, 1, ?, ?, 0, 1)
        """, (name, url, description, app_id or "", now, now))
        
        item_id = cur.lastrowid
        
        # Link to dashboard tag
        cur.execute("INSERT INTO item_tag (item_id, tag_id) VALUES (?, 0)", (item_id,))
        
        conn.commit()
        conn.close()
        print(f"[heimdall] Tile added: {name} -> {url}")
        return True
        
    except Exception as e:
        print(f"[heimdall] Add tile error: {e}")
        return False

def remove_heimdall_tile(url):
    """Remove a tile from Heimdall dashboard."""
    if not os.path.exists(HEIMDALL_DB):
        return False
    
    try:
        conn = sqlite3.connect(HEIMDALL_DB)
        cur = conn.cursor()
        
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("UPDATE items SET deleted_at=? WHERE url=? AND deleted_at IS NULL", (now, url))
        conn.commit()
        conn.close()
        print(f"[heimdall] Tile removed for: {url}")
        return True
        
    except Exception as e:
        print(f"[heimdall] Remove tile error: {e}")
        return False

if __name__ == "__main__":
    setup_heimdall_custom_js()
