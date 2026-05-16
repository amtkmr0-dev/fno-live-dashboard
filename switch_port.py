#!/usr/bin/env python3
"""
switch_port.py - Change ws_server.py listening port.

Usage:
  python3 switch_port.py 8081   # Switch to port 8081 (for auth proxy mode)
  python3 switch_port.py 8080   # Switch back to port 8080 (standalone mode)
"""

import re
import sys
import shutil

if len(sys.argv) < 2:
    print("Usage: python3 switch_port.py <port>")
    print("  python3 switch_port.py 8081  -- for auth proxy mode")
    print("  python3 switch_port.py 8080  -- for standalone mode (no auth)")
    sys.exit(1)

new_port = int(sys.argv[1])
server_file = "ws_server.py"

with open(server_file, "r") as f:
    code = f.read()

# Backup
shutil.copy2(server_file, server_file + ".port_backup")

# Common patterns for port in aiohttp servers
# Pattern 1: port=8080 or port=8081
# Pattern 2: PORT = 8080
# Pattern 3: host="0.0.0.0", port=8080

changes = 0

# Try run_app pattern: web.run_app(app, host=..., port=NNNN)
new_code = re.sub(
    r'(web\.run_app\([^)]*port\s*=\s*)\d+',
    lambda m: m.group(1) + str(new_port),
    code
)
if new_code != code:
    changes += code.count('web.run_app')
    code = new_code

# Try PORT = NNNN constant
new_code = re.sub(
    r'^(PORT\s*=\s*)\d+',
    lambda m: m.group(1) + str(new_port),
    code,
    flags=re.MULTILINE
)
if new_code != code:
    changes += 1
    code = new_code

# Try port=NNNN in runner
new_code = re.sub(
    r'(\.run\([^)]*port\s*=\s*)\d+',
    lambda m: m.group(1) + str(new_port),
    code
)
if new_code != code:
    changes += 1
    code = new_code

if changes == 0:
    print(f"WARNING: Could not find port pattern in {server_file}")
    print("You may need to manually change the port.")
    print(f"Look for 'port' or '8080' in {server_file}")
    # Try brute force: replace 8080 with new port (risky but last resort)
    if '8080' in code and new_port != 8080:
        count = code.count('8080')
        print(f"Found {count} occurrence(s) of '8080' - replacing all")
        code = code.replace('8080', str(new_port))
        changes = count
    elif '8081' in code and new_port != 8081:
        count = code.count('8081')
        print(f"Found {count} occurrence(s) of '8081' - replacing all")
        code = code.replace('8081', str(new_port))
        changes = count

with open(server_file, "w") as f:
    f.write(code)

print(f"Port changed to {new_port} in {server_file} ({changes} edit(s))")
print(f"Backup: {server_file}.port_backup")
print(f"\nRestart: pkill -f ws_server.py; sleep 2; nohup venv/bin/python3 ws_server.py >> server.log 2>&1 &")
