#!/usr/bin/env python3
"""
setup_auth.py - Create auth_config.json with hashed credentials.

Usage:
  python3 setup_auth.py

Prompts for username and password, then writes auth_config.json.
Run this once before starting auth_proxy.py.
"""

import hashlib
import json
import os
import secrets
import sys
import getpass


def hash_password(password, salt):
    return hashlib.sha256((salt + password).encode()).hexdigest()


def create_user(username, password):
    salt = secrets.token_hex(16)
    hashed = hash_password(password, salt)
    return {
        "username": username,
        "salt": salt,
        "hash": hashed
    }


def main():
    config_file = "auth_config.json"

    # Load existing config if present
    config = {"users": [], "session_max_age": 86400}
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            config = json.load(f)
        print(f"Existing config found with {len(config.get('users', []))} user(s)")

    print("\n=== Quantra Terminal - Auth Setup ===\n")

    if len(sys.argv) >= 3:
        # Non-interactive: python3 setup_auth.py <username> <password>
        username = sys.argv[1]
        password = sys.argv[2]
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

    if not password or len(password) < 4:
        print("Password must be at least 4 characters")
        sys.exit(1)

    # Remove existing user with same name
    config["users"] = [u for u in config.get("users", []) if u["username"] != username]

    # Add new user
    user = create_user(username, password)
    config["users"].append(user)

    # Generate session secret if not present
    if "session_secret" not in config:
        config["session_secret"] = secrets.token_hex(32)

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nUser '{username}' configured.")
    print(f"Config saved to {config_file}")
    print(f"\nNext steps:")
    print(f"  1. Change ws_server.py port from 8080 to 8081")
    print(f"  2. Restart ws_server.py")
    print(f"  3. Start auth proxy: nohup venv/bin/python3 auth_proxy.py >> auth_proxy.log 2>&1 &")


if __name__ == "__main__":
    main()
