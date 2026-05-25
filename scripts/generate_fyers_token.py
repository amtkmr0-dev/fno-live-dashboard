#!/usr/bin/env python3
"""
Fyers API v3 Access Token Generator (Zero-Dependency)
===================================================
A standalone, interactive utility to generate your daily Fyers Access Token
and save it directly to your config.env file.

No SDKs or third-party dependencies required!
"""

import sys
import os
import hashlib
import urllib.request
import urllib.parse
import json
from pathlib import Path

# Try to load existing client settings from config.env
def get_env_value(key: str) -> str:
    cfg = Path(__file__).parent.parent / "config.env"
    if cfg.exists():
        with open(cfg, "r") as f:
            for line in f:
                if line.strip().startswith(key):
                    try:
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
                    except IndexError:
                        pass
    return ""

def update_config_env(key: str, val: str):
    cfg = Path(__file__).parent.parent / "config.env"
    lines = []
    found = False
    
    if cfg.exists():
        with open(cfg, "r") as f:
            for line in f:
                if line.strip().startswith(key):
                    lines.append(f"{key}={val}\n")
                    found = True
                else:
                    lines.append(line)
                    
    if not found:
        # Append to the end
        lines.append(f"\n# Added by Fyers Token Generator\n{key}={val}\n")
        
    with open(cfg, "w") as f:
        f.writelines(lines)
    print(f"✅ Successfully saved {key} to config.env!")

def main():
    print("=" * 60)
    print("🚀 FYERS API v3 - ACCESS TOKEN GENERATOR")
    print("=" * 60)
    print("Make sure you have created a Fyers App at: https://myapi.fyers.in/")
    print("Set your App's Redirect URI to: https://trade.fyers.in/api-login/redirect-uri/index.html")
    print("-" * 60)

    # 1. Resolve App ID and Secret Key
    app_id = get_env_value("FYERS_APP_ID")
    secret_key = get_env_value("FYERS_SECRET_KEY")
    redirect_uri = get_env_value("FYERS_REDIRECT_URI") or "https://fyers-vercel-redirect.vercel.app/redirect"

    # Only offer to use saved credentials if both App ID and Secret Key are non-empty
    if app_id and secret_key:
        use_saved = input(f"Found saved FYERS_APP_ID ({app_id[:6]}...). Use this? (Y/n): ").strip().lower()
        if use_saved == 'n':
            app_id = ""
            secret_key = ""
    else:
        # If one is missing, reset so we prompt for both
        app_id = ""
        secret_key = ""
            
    while not app_id:
        app_id = input("Enter your Fyers App ID (client_id): ").strip()
    
    while not secret_key:
        secret_key = input("Enter your Fyers App Secret Key: ").strip()

    print(f"\nCurrent Redirect URI configured: {redirect_uri}")
    change_redirect = input("Do you want to use a different Redirect URI? (y/N): ").strip().lower()
    if change_redirect == 'y':
        redirect_uri = input("Enter your custom Redirect URI: ").strip()

    # Save details if they entered them
    save_creds = input("\nSave these App details to config.env for future use? (Y/n): ").strip().lower()
    if save_creds != 'n':
        update_config_env("FYERS_APP_ID", app_id)
        update_config_env("FYERS_SECRET_KEY", secret_key)
        update_config_env("FYERS_REDIRECT_URI", redirect_uri)

    if not app_id or not secret_key:
        print("❌ Error: App ID and Secret Key are required.")
        sys.exit(1)

    # 2. Generate authorization URL
    # Fyers v3 auth code login url
    state = "dashboard_p2"
    auth_params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state
    }
    auth_url = f"https://api-t1.fyers.in/api/v3/generate-authcode?{urllib.parse.urlencode(auth_params)}"

    print("\n👉 STEP 1: Click or open this authorization URL in your browser:")
    print("-" * 60)
    print(auth_url)
    print("-" * 60)
    print("Log in to your Fyers account. Once logged in, your browser will redirect to a page that looks like a login success screen.")
    print("Look at the address bar of that redirected page. It will have a URL containing '?auth_code=...' or '?code=...'\n")

    # 3. Prompt for Auth Code
    auth_code = ""
    while not auth_code:
        redirected_input = input("👉 STEP 2: Paste the redirected URL or the auth_code/code here: ").strip()
        if not redirected_input:
            continue

        # Parse code if they pasted the full URL
        if "code=" in redirected_input:
            parsed = urllib.parse.urlparse(redirected_input)
            params = urllib.parse.parse_qs(parsed.query)
            auth_code = params.get("code", [""])[0] or params.get("auth_code", [""])[0]
        elif "auth_code=" in redirected_input:
            parsed = urllib.parse.urlparse(redirected_input)
            params = urllib.parse.parse_qs(parsed.query)
            auth_code = params.get("auth_code", [""])[0]
        elif redirected_input.startswith("http://") or redirected_input.startswith("https://"):
            print("\n⚠️  WARNING: You pasted the clean redirect URL, but it does NOT contain the authorization code.")
            print("When Fyers redirects you after login, look at your browser's address bar. The URL must contain a code parameter, like:")
            print("  https://fyers-vercel-redirect.vercel.app/redirect?auth_code=abc123xyz...")
            print("Please copy the ENTIRE URL from your address bar and paste it here!\n")
            auth_code = ""
        else:
            auth_code = redirected_input

    if not auth_code:
        print("❌ Error: Could not extract authentication code.")
        sys.exit(1)

    print(f"Parsed auth_code: {auth_code[:10]}...")

    # 4. Exchange Auth Code for Access Token
    print("\n👉 STEP 3: Requesting Access Token from Fyers...")
    
    # Calculate appIdHash = sha256(app_id + ":" + secret_key)
    hash_payload = f"{app_id}:{secret_key}".encode("utf-8")
    app_id_hash = hashlib.sha256(hash_payload).hexdigest()

    token_url = "https://api.fyers.in/api/v3/token"
    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code
    }
    
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_body = response.read().decode("utf-8")
            res_data = json.loads(res_body)
            
            if res_data.get("s") == "ok" and "access_token" in res_data:
                access_token = res_data["access_token"]
                print("\n🎉 SUCCESS! Fyers Access Token generated successfully!")
                print("-" * 60)
                print(f"Token: {access_token[:20]}... [length {len(access_token)}]")
                print("-" * 60)
                
                # Ask to save directly to config.env
                save_token = input("Save this token as FYERS_ACCESS_TOKEN to config.env? (Y/n): ").strip().lower()
                if save_token != 'n':
                    update_config_env("FYERS_ACCESS_TOKEN", access_token)
                    print("\n💡 Tip: Restart the backend servers to start using the Fyers backup feed!")
            else:
                print(f"❌ Fyers Error: {res_data.get('message', 'Unknown response structure')}")
                print(f"Full response: {res_data}")
                
    except urllib.error.HTTPError as err:
        error_body = err.read().decode("utf-8")
        print(f"❌ HTTP Error {err.code}: {error_body}")
    except Exception as exc:
        print(f"❌ General Error: {exc}")

if __name__ == "__main__":
    main()
