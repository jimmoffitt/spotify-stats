"""
src/config.py — Centralized configuration and constants.

Loads credentials from .local.env, defines every file path used across the
project, and declares the constants (API endpoints, OAuth scopes, batch sizes,
play-time thresholds) plus the DEFAULT_SETTINGS dict that backs the Settings
tab. All other modules import from here rather than hardcoding paths or values.
Call validate_config() at startup to catch missing credentials early.
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv(dotenv_path='.local.env')

# 1. Define Directories
DATA_DIR = 'data'
RAW_DIR = os.path.join(DATA_DIR, 'raw')              # GDPR export JSON files
ENRICHED_DIR = os.path.join(DATA_DIR, 'enriched')    # cached API lookups
PROCESSED_DIR = os.path.join(DATA_DIR, 'processed')  # plays.parquet

for d in [DATA_DIR, RAW_DIR, ENRICHED_DIR, PROCESSED_DIR]:
    os.makedirs(d, exist_ok=True)

# 2. Define File Paths
TOKEN_FILE = os.getenv('SPOTIFY_TOKEN_FILE', os.path.join(DATA_DIR, 'spotify_tokens.json'))

# Enriched metadata caches (see DESIGN.md "API Enrichment Layer")
TRACK_METADATA_FILE = os.path.join(ENRICHED_DIR, 'track_metadata.json')
ARTIST_METADATA_FILE = os.path.join(ENRICHED_DIR, 'artist_metadata.json')
COUNTRY_METADATA_FILE = os.path.join(ENRICHED_DIR, 'country_metadata.json')  # Phase 2

# Processed, fully-enriched play log
PLAYS_FILE = os.path.join(PROCESSED_DIR, 'plays.parquet')

# State + preferences
LAST_SYNC_FILE = os.path.join(DATA_DIR, 'last_sync.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
# Per-year artist exclusions (e.g. shared-account years). Maps a year string
# (or "*" for all years) to a list of artist names to drop from all stats.
EXCLUSIONS_FILE = os.path.join(DATA_DIR, 'exclusions.json')

# 3. Secrets / OAuth
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'http://localhost:8888/callback')

# Scopes needed for incremental sync (recently-played) and top-read analytics
SCOPES = 'user-read-recently-played user-top-read'

# 4. Spotify API endpoints
AUTH_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
API_BASE = 'https://api.spotify.com/v1'
RECENTLY_PLAYED_URL = f'{API_BASE}/me/player/recently-played'
TRACKS_URL = f'{API_BASE}/tracks'    # batch up to 50
ARTISTS_URL = f'{API_BASE}/artists'  # batch up to 50

# Max IDs per batch request for the /tracks and /artists endpoints
API_BATCH_SIZE = 50
# Max items per recently-played page (Spotify hard cap)
RECENTLY_PLAYED_LIMIT = 50

# 5. Data constants
# URI prefixes — used to separate music tracks from podcast episodes
TRACK_URI_PREFIX = 'spotify:track:'
EPISODE_URI_PREFIX = 'spotify:episode:'

# Plays under this many ms are incidental (skips / accidental taps); flag or filter.
INCIDENTAL_PLAY_MS = 30_000  # ~30 seconds

# GDPR export filename glob. Spotify's "Extended Streaming History" export uses
# Streaming_History_Audio_YYYY[_N].json (the richer per-play schema with ts,
# ms_played, spotify_track_uri). The older basic export used
# StreamingHistory_music_*.json. We target the extended audio files and
# deliberately exclude Streaming_History_Video_*.json.
GDPR_GLOB = 'Streaming_History_Audio_*.json'

# 6. Defaults used when settings.json doesn't exist yet
DEFAULT_SETTINGS = {
    'timezone': None,                 # None = fall back to system timezone on first run
    'full_listen_threshold': 0.80,    # ms_played > threshold * duration_ms => full_listen
    'gdpr_export_date': None,         # ISO date the GDPR export was generated
    'data_paths': {
        'raw': RAW_DIR,
        'enriched': ENRICHED_DIR,
        'processed': PROCESSED_DIR,
    },
}


def validate_config():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("❌ ERROR: Credentials not found in .local.env "
                         "(need SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)")
    if not REDIRECT_URI:
        raise ValueError("❌ ERROR: SPOTIFY_REDIRECT_URI is not set.")
