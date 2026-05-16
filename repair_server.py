#!/usr/bin/env python3
"""
repair_server.py - Strip broken auth patch from ws_server.py.
The original patch_auth.py inserted module-level auth code inside a class
method, breaking indentation. This script removes all injected auth code
and restores ws_server.py to a working state.

Run: cd ~/deploy && python3 repair_server.py
Then restart: pkill -f ws_server.py; sleep 2; nohup venv/bin/python3 ws_server.py >> server.log 2>&1 &
"""
import re, os, shutil

SERVER_FILE = "ws_server.py"

if not os.path.exists(SERVER_FILE):
    print(f"ERROR: {SERVER_FILE} not found in current directory")
    exit(1)

# Backup first
backup = SERVER_FILE + ".broken_backup"
shutil.copy2(SERVER_FILE, backup)
print(f"Backup saved to {backup}")

with open(SERVER_FILE, 'r') as f:
    code = f.read()

original_len = len(code)
fixes = []

# ── 1. Remove injected imports ──────────────────────────────
for imp in ['import hashlib', 'import secrets', 'import time as time_module']:
    if imp in code:
        code = code.replace('\n' + imp, '')
        fixes.append(f"Removed: {imp}")

# ── 2. Remove AUTH MODULE block ─────────────────────────────
# Find start marker
auth_start = code.find('# ============================================================\n# AUTH MODULE')
if auth_start == -1:
    auth_start = code.find('# AUTH MODULE')

if auth_start > 0:
    # Walk backwards to find the true start (skip any whitespace/newlines before marker)
    while auth_start > 0 and code[auth_start - 1] in ' \t\n':
        auth_start -= 1

    # Find where original routes resume - look for app.router.add_get with self.handle_index
    # It may be at col 0 (broken) or col 8 (if somehow preserved)
    resume_pattern = re.compile(r'\n\s{0,8}app\.router\.add_get\(\s*"/"\s*,\s*self\.handle_index\)')
    resume_match = resume_pattern.search(code, auth_start)

    if resume_match:
        auth_end = resume_match.start()
        removed = code[auth_start:auth_end]
        code = code[:auth_start] + code[auth_end:]
        fixes.append(f"Removed AUTH MODULE block ({len(removed)} chars, ~{removed.count(chr(10))} lines)")
    else:
        print("WARNING: Could not find end of AUTH MODULE block (app.router.add_get '/' self.handle_index)")
        print("Trying alternative end detection...")
        # Try to find end by looking for the last line of auth_middleware
        alt_end = code.find('return await handler(request)\n', auth_start)
        if alt_end > 0:
            # Find the next non-blank line after this
            alt_end = code.find('\n', alt_end) + 1
            while alt_end < len(code) and code[alt_end] in ' \t\n':
                alt_end += 1
            removed = code[auth_start:alt_end]
            code = code[:auth_start] + '\n' + code[alt_end:]
            fixes.append(f"Removed AUTH MODULE block (alt method, {len(removed)} chars)")
else:
    fixes.append("No AUTH MODULE block found (already clean or different format)")

# ── 3. Fix first route indentation ─────────────────────────
# The line `app.router.add_get("/", self.handle_index)` may be at col 0
# It needs to be at 8 spaces (inside class method)
if '\napp.router.add_get("/", self.handle_index)' in code:
    code = code.replace(
        '\napp.router.add_get("/", self.handle_index)',
        '\n        app.router.add_get("/", self.handle_index)'
    )
    fixes.append("Restored 8-space indent on first route line")

# ── 4. Remove injected auth route registrations ─────────────
auth_route_patterns = [
    r"\n[ \t]*# Auth routes[ \t]*",
    r"\n[ \t]*app\.router\.add_post\('/api/auth/login',\s*handle_login\)[ \t]*",
    r"\n[ \t]*app\.router\.add_post\('/api/auth/verify',\s*handle_verify\)[ \t]*",
    r"\n[ \t]*app\.router\.add_get\('/api/auth/logout',\s*handle_logout\)[ \t]*",
    r"\n[ \t]*app\.router\.add_get\('/login',\s*serve_login_page\)[ \t]*",
]
for pat in auth_route_patterns:
    match = re.search(pat, code)
    if match:
        code = code[:match.start()] + code[match.end():]
        fixes.append(f"Removed auth route: {pat[:50]}...")

# ── 5. Remove middlewares=[auth_middleware] from app creation ──
if 'auth_middleware' in code:
    # Pattern: middlewares=[auth_middleware] with optional comma/space
    code = re.sub(r',\s*middlewares\s*=\s*\[auth_middleware\]', '', code)
    code = re.sub(r'middlewares\s*=\s*\[auth_middleware\]\s*,?\s*', '', code)
    fixes.append("Removed auth_middleware from app creation")

# ── 6. Remove charset signal if injected ────────────────────
charset_pat = r'\n# Force UTF-8 charset on HTML responses\nasync def charset_signal\(request,\s*response\):[^\n]*\n[^\n]*\n[^\n]*\napp\.on_response_prepare\.append\(charset_signal\)'
match = re.search(charset_pat, code)
if match:
    code = code[:match.start()] + code[match.end():]
    fixes.append("Removed charset_signal block")
else:
    # Try a broader pattern
    if 'charset_signal' in code:
        code = re.sub(r'\n# Force UTF-8 charset[^\n]*\n(?:.*\n)*?app\.on_response_prepare\.append\(charset_signal\)\n?', '\n', code)
        fixes.append("Removed charset_signal block (broad match)")

# ── 7. Clean up straggling auth references ──────────────────
# Remove any remaining standalone auth function definitions that might be left
stray_patterns = [
    r'\n_auth_sessions\s*=\s*\{\}[^\n]*',
    r'\ndef _load_auth_config\(\):[^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\ndef _hash_password\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\ndef _create_session\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\ndef _validate_session\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\ndef _get_token_from_request\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\nasync def handle_login\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\nasync def handle_verify\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\nasync def handle_logout\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\nasync def serve_login_page\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
    r'\n@web\.middleware\nasync def auth_middleware\([^\n]*(?:\n(?:    [^\n]*|\s*))*',
]
for pat in stray_patterns:
    m = re.search(pat, code)
    if m:
        code = code[:m.start()] + code[m.end():]
        func_name = pat.split('def ')[-1].split('(')[0] if 'def ' in pat else pat[:40]
        fixes.append(f"Removed straggling: {func_name}")

# ── 8. Clean up excessive blank lines ───────────────────────
code = re.sub(r'\n{4,}', '\n\n\n', code)
# Remove lines that are only whitespace
code = re.sub(r'\n[ \t]+\n', '\n\n', code)

with open(SERVER_FILE, 'w') as f:
    f.write(code)

print(f"\n{'='*50}")
print(f"REPAIR COMPLETE")
print(f"{'='*50}")
print(f"File: {SERVER_FILE}")
print(f"Size: {original_len:,} -> {len(code):,} bytes (removed {original_len - len(code):,})")
print(f"\nFixes applied ({len(fixes)}):")
for i, fix in enumerate(fixes, 1):
    print(f"  {i}. {fix}")
print(f"\nBackup: {backup}")
print(f"\nNext: restart the server:")
print(f"  pkill -f ws_server.py; sleep 2; nohup venv/bin/python3 ws_server.py >> server.log 2>&1 &")
print(f"  sleep 3 && curl -sI http://localhost:8080/ | head -5")
