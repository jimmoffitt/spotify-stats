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

# Incrementally-synced plays (from recently-played), kept separate from the
# pristine GDPR export in data/raw/ but merged with it when building plays.parquet.
SYNCED_PLAYS_FILE = os.path.join(DATA_DIR, 'synced_plays.json')

# State + preferences
LAST_SYNC_FILE = os.path.join(DATA_DIR, 'last_sync.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
# Per-year artist exclusions (e.g. shared-account years). Maps a year string
# (or "*" for all years) to a list of artist names to drop from all stats.
EXCLUSIONS_FILE = os.path.join(DATA_DIR, 'exclusions.json')
# Saved band groups (e.g. "New Zealand" -> list of artist names). Powers the
# group summaries on the Bands tab. Keyed by artist name (see load_groups).
GROUPS_FILE = os.path.join(DATA_DIR, 'groups.json')

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

# 6. Demo mode — read-only deploy backed by the sanitized dataset that
# make_demo_data.py writes to data/demo/ (the only play data tracked in git;
# the real archive/cache/processed files are gitignored). Enabled explicitly
# via SPOTIFY_STATS_DEMO=1, or implicitly on a fresh clone where the real
# processed parquet is absent but the demo dataset is present (e.g. Streamlit
# Community Cloud — no secrets or env config needed). The app hides the Sync
# button in this mode; runtime writes (settings, exclusions, groups) land in
# data/demo/, which is gitignored apart from the tracked demo dataset itself.
DEMO_DIR = os.path.join(DATA_DIR, 'demo')
DEMO_PLAYS_FILE = os.path.join(DEMO_DIR, 'plays.parquet')
DEMO_MODE = (
    os.getenv('SPOTIFY_STATS_DEMO', '').strip().lower() in ('1', 'true', 'yes')
    or (not os.path.exists(PLAYS_FILE) and os.path.exists(DEMO_PLAYS_FILE))
)
if DEMO_MODE:
    PLAYS_FILE = DEMO_PLAYS_FILE
    SETTINGS_FILE = os.path.join(DEMO_DIR, 'settings.json')
    EXCLUSIONS_FILE = os.path.join(DEMO_DIR, 'exclusions.json')
    GROUPS_FILE = os.path.join(DEMO_DIR, 'groups.json')
    LAST_SYNC_FILE = os.path.join(DEMO_DIR, 'last_sync.json')
    os.makedirs(DEMO_DIR, exist_ok=True)

# 7. Defaults used when settings.json doesn't exist yet
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
