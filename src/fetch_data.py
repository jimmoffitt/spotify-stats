"""
src/fetch_data.py — Spotify API client and data loaders.

Two responsibilities:

  1. OAuth + live API sync. get_access_token() reads data/spotify_tokens.json
     and transparently refreshes the access token via the refresh_token when it
     is within 5 minutes of expiry. fetch_recently_played() pulls the most
     recent plays (up to 50) for incremental updates after the GDPR backbone is
     loaded. The one-time authorization flow lives in src/setup_tokens.py;
     refresh here is automatic on every subsequent run.

  2. GDPR export loader. load_gdpr_export() reads every
     StreamingHistory_music_*.json file from data/raw/, merges them into a
     single play list, and deduplicates by (ts, track_uri).

Called by run_pipeline.py and by app.py's Sync Now button.
"""
import base64
import glob
import json
import os
import time

import requests

from src import config


# --- OAuth token management ---

def _basic_auth_header(client_id, client_secret):
    """Spotify's token endpoint authenticates with a Basic base64(id:secret) header."""
    raw = f"{client_id}:{client_secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def save_tokens(tokens, token_file):
    """
    Persist a token payload, deriving the absolute 'expires_at' from Spotify's
    relative 'expires_in' (seconds). A refresh response may omit refresh_token;
    callers should merge into the existing dict before saving so it is retained.
    """
    if 'expires_in' in tokens:
        tokens['expires_at'] = int(time.time()) + int(tokens['expires_in'])
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    with open(token_file, 'w') as f:
        json.dump(tokens, f, indent=2)
    return tokens


def get_access_token(token_file=config.TOKEN_FILE,
                     client_id=config.CLIENT_ID,
                     client_secret=config.CLIENT_SECRET):
    """
    Return a valid access token, refreshing it if it expires within 5 minutes.

    Unlike Strava, a Spotify refresh response often does NOT include a new
    refresh_token, so we merge the response into the stored tokens to preserve
    the original refresh_token and scope.
    """
    if not os.path.exists(token_file):
        raise FileNotFoundError(
            f"ERROR: '{token_file}' not found. Run `python -m src.setup_tokens` first.")

    with open(token_file, 'r') as f:
        tokens = json.load(f)

    if tokens.get('expires_at', 0) < time.time() + 300:
        print("Token expired. Refreshing...")
        response = requests.post(
            config.TOKEN_URL,
            headers=_basic_auth_header(client_id, client_secret),
            data={
                'grant_type': 'refresh_token',
                'refresh_token': tokens['refresh_token'],
            },
        )
        if response.status_code != 200:
            raise ConnectionError(f"Error refreshing token: {response.text}")
        # Merge so refresh_token / scope survive when the response omits them.
        tokens.update(response.json())
        save_tokens(tokens, token_file)

    return tokens['access_token']


def get_client_credentials_token(client_id=config.CLIENT_ID, client_secret=config.CLIENT_SECRET):
    """
    Return an app-only access token via the Client Credentials flow. This token
    works for catalog endpoints that need no user scope — /tracks and /artists,
    used by the enrichment layer — and avoids the interactive OAuth setup.
    It does NOT work for /me/* endpoints (e.g. recently-played); those still
    require the user-authorized token from get_access_token().
    """
    response = requests.post(
        config.TOKEN_URL,
        headers=_basic_auth_header(client_id, client_secret),
        data={'grant_type': 'client_credentials'},
    )
    if response.status_code != 200:
        raise ConnectionError(f"Error getting client-credentials token: {response.text}")
    return response.json()['access_token']


# --- Live API: incremental sync ---

def fetch_recently_played(access_token, after_ts=None, limit=config.RECENTLY_PLAYED_LIMIT):
    """
    Fetch the most recent plays from /me/player/recently-played (max 50).

    after_ts: optional Unix timestamp in MILLISECONDS; only plays strictly after
              it are returned. Pass the last_sync timestamp for incremental pulls.

    Returns the raw list of play-history items (each has 'track' and 'played_at').
    NOTE: this endpoint caps at 50 items — if more than 50 plays occurred since
    the last sync, the gap cannot be recovered from this endpoint (see DESIGN.md).
    """
    params = {'limit': min(limit, config.RECENTLY_PLAYED_LIMIT)}
    if after_ts is not None:
        params['after'] = int(after_ts)

    response = requests.get(
        config.RECENTLY_PLAYED_URL,
        headers={'Authorization': f"Bearer {access_token}"},
        params=params,
    )
    if response.status_code != 200:
        raise ConnectionError(
            f"Error fetching recently-played: {response.status_code} {response.text}")
    return response.json().get('items', [])


# --- GDPR export loader ---

def load_gdpr_export(raw_dir=config.RAW_DIR):
    """
    Load and merge every StreamingHistory_music_*.json file in raw_dir.

    Returns a list of play dicts deduplicated by (ts, spotify_track_uri) and
    sorted chronologically. Records missing a timestamp are skipped. The fields
    are passed through untouched; column derivation happens in process_data.py.
    """
    pattern = os.path.join(raw_dir, config.GDPR_GLOB)
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(
            f"No GDPR export files matching '{config.GDPR_GLOB}' in '{raw_dir}'. "
            "Place your Spotify 'Download your data' JSON files in data/raw/.")

    seen = set()
    plays = []
    for path in files:
        with open(path, 'r', encoding='utf-8') as f:
            try:
                records = json.load(f)
            except json.JSONDecodeError:
                print(f"⚠️  Skipping unreadable file: {path}")
                continue
        kept = 0
        for rec in records:
            ts = rec.get('ts')
            if not ts:
                continue
            key = (ts, rec.get('spotify_track_uri'))
            if key in seen:
                continue
            seen.add(key)
            plays.append(rec)
            kept += 1
        print(f"   Loaded {kept} new plays from {os.path.basename(path)}")

    plays.sort(key=lambda r: r['ts'])
    print(f"✅ GDPR export: {len(plays)} unique plays from {len(files)} file(s).")
    return plays
