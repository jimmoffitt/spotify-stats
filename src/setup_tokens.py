"""
src/setup_tokens.py — One-time Spotify OAuth setup script.

Run this interactively before the first pipeline execution to complete the
Authorization Code flow: it prints an authorization URL, prompts for the code
returned on the redirect, exchanges it for access + refresh tokens, and writes
the result (with a derived expires_at) to data/spotify_tokens.json. Requires
SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, and SPOTIFY_REDIRECT_URI in
.local.env. Only needs to run once — fetch_data.get_access_token() refreshes
automatically on every subsequent run.

Usage:  python -m src.setup_tokens
"""
import urllib.parse

import requests

from src import config
from src.fetch_data import _basic_auth_header, save_tokens

config.validate_config()

# 1. Build the authorization URL
print("--- Spotify Auth Setup ---")
print(f"Target file: {config.TOKEN_FILE}\n")

auth_params = {
    'client_id': config.CLIENT_ID,
    'response_type': 'code',
    'redirect_uri': config.REDIRECT_URI,
    'scope': config.SCOPES,
    'show_dialog': 'true',
}
auth_url = f"{config.AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

print("1. Open this URL in your browser and authorize the app:\n")
print(auth_url)

# 2. Collect the redirect URL / code
print("\n2. After authorizing you'll be redirected to a localhost URL that "
      "fails to load — that's expected.")
print("   Copy either the full redirected URL or just the 'code' query value.")
pasted = input("\n   Paste it here: ").strip()

# Accept a full redirect URL or a bare code.
if 'code=' in pasted:
    query = urllib.parse.urlparse(pasted).query
    auth_code = urllib.parse.parse_qs(query).get('code', [pasted])[0]
else:
    auth_code = pasted

# 3. Exchange the code for tokens
print("\n3. Exchanging code for tokens...")
response = requests.post(
    config.TOKEN_URL,
    headers=_basic_auth_header(config.CLIENT_ID, config.CLIENT_SECRET),
    data={
        'grant_type': 'authorization_code',
        'code': auth_code,
        'redirect_uri': config.REDIRECT_URI,
    },
)

if response.status_code == 200:
    save_tokens(response.json(), config.TOKEN_FILE)
    print(f"\n✅ SUCCESS! Tokens saved to '{config.TOKEN_FILE}'")
else:
    print("\n❌ Error exchanging token:")
    print(response.text)
