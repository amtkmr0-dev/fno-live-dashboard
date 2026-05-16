#!/usr/bin/env python3
"""
setup_auth.py - Create auth_config.json with hashed credentials.

Usage:
  python3 setup_auth.py <username> <password> [role]
  python3 setup_auth.py --set-chat-key <perplexity-api-key>

  role: "admin" (default) or "user"

Examples:
  python3 setup_auth.py amit MyPass123 admin          # admin account
  python3 setup_auth.py guest ViewOnly1 user          # restricted user
  python3 setup_auth.py --set-chat-key pplx-xxxxxx    # set Perplexity API key

Run before starting auth_proxy.py. Can be run multiple times to add users.
"""

import hashlib
import json
import os
import secrets
import sys
import getpass


VALID_ROLES = {"admin", "user"}


def hash_password(password, salt):
    return hashlib.sha256((salt + password).encode()).hexdigest()


def create_user(username, password, role="admin"):
    salt = secrets.token_hex(16)
    hashed = hash_password(password, salt)
    return {
        "username": username,
        "salt": salt,
        "hash": hashed,
        "role": role
    }


def load_config(config_file):
    """Load existing config, handling corrupt/old formats gracefully."""
    config = {"users": [], "session_max_age": 86400}
    if os.path.exists(config_file):
        try:
            with open(config_file, "r") as f:
                loaded = json.load(f)
            # Validate structure
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), list):
                # Validate each user is a dict with 'username' key
                valid_users = [u for u in loaded["users"] if isinstance(u, dict) and "username" in u]
                config = loaded
                config["users"] = valid_users
                print(f"Existing config: {len(valid_users)} valid user(s)")
            else:
                print("Old/corrupt config found - starting fresh")
        except (json.JSONDecodeError, KeyError):
            print("Corrupt config found - starting fresh")
    return config


def main():
    config_file = "auth_config.json"
    config = load_config(config_file)

    print("\n=== Quantra Terminal - Auth Setup ===\n")

    # Handle --set-chat-key flag
    if len(sys.argv) >= 3 and sys.argv[1] == "--set-chat-key":
        api_key = sys.argv[2].strip()
        if not api_key:
            print("API key cannot be empty")
            sys.exit(1)
        config["perplexity_api_key"] = api_key
        if "session_secret" not in config:
            config["session_secret"] = secrets.token_hex(32)
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)
        masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        print(f"Perplexity API key set: {masked}")
        print(f"Restart auth_proxy.py to pick up the change.")
        return

    if len(sys.argv) >= 3:
        # Non-interactive: python3 setup_auth.py <username> <password> [role]
        username = sys.argv[1]
        password = sys.argv[2]
        role = sys.argv[3] if len(sys.argv) >= 4 else "admin"
    else:
        # Interactive
        username = input("Username: ").strip()
        if not username:
            print("Username cannot be empty")
            sys.exit(1)
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords don't match")
            sys.exit(1)
        role = input("Role (admin/user) [admin]: ").strip().lower() or "admin"

    if role not in VALID_ROLES:
        print(f"Invalid role '{role}'. Must be: {', '.join(VALID_ROLES)}")
        sys.exit(1)

    if not password or len(password) < 4:
        print("Password must be at least 4 characters")
        sys.exit(1)

    # Remove existing user with same name
    config["users"] = [u for u in config.get("users", []) if u["username"] != username]

    # Add new user
    user = create_user(username, password, role)
    config["users"].append(user)

    # Generate session secret if not present
    if "session_secret" not in config:
        config["session_secret"] = secrets.token_hex(32)

    # Ensure admin_paths config exists
    if "admin_paths" not in config:
        config["admin_paths"] = ["/admin", "/divergence"]

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"User '{username}' configured as {role.upper()}.")
    print(f"Config saved to {config_file}")

    # Show all users
    print(f"\nAll users:")
    for u in config["users"]:
        r = u.get("role", "admin")
        print(f"  - {u['username']} ({r})")

    print(f"\nAdmin-only pages: {', '.join(config['admin_paths'])}")
    print(f"\nNext: restart auth_proxy.py to pick up changes")
    print(f"  pkill -f auth_proxy.py; sleep 1; nohup venv/bin/python3 auth_proxy.py >> auth_proxy.log 2>&1 &")


if __name__ == "__main__":
    main()
