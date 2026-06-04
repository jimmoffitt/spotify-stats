# spotify-stats — Design Document

A personal Spotify listening history dashboard built in Streamlit, modeled after
[strava-stats](https://github.com/jimmoffitt/strava-stats). Answers the questions
Spotify Wrapped doesn't: how has my taste changed year over year? What countries
dominate my listening? When do I listen, and what decades am I pulling from?

---

## Data Sources

### Primary — GDPR Export (historical backbone)

Request via: Spotify Account → Privacy Settings → Download your data.
Arrives as one or more JSON files: `StreamingHistory_music_0.json`, `_1.json`, etc.

Each record:

```json
{
  "ts": "2023-11-14T08:23:45Z",
  "ms_played": 237000,
  "master_metadata_track_name": "Rosalita",
  "master_metadata_album_artist_name": "Bruce Springsteen",
  "master_metadata_album_album_name": "The Wild, the Innocent & the E Street Shuffle",
  "spotify_track_uri": "spotify:track:4TmWmCGSeg1sWQWoJFMKBP",
  "reason_start": "trackdone",
  "reason_end": "trackdone",
  "shuffle": false,
  "skipped": false
}
```

Key notes:
- `ms_played` distinguishes full listens from skips
- `spotify_track_uri` is the bridge to API enrichment
- No genre, country, or release year in the raw export — all require enrichment
- Plays under ~30 seconds should be flagged or filtered as incidental

### Secondary — Spotify Web API (incremental sync)

- `/me/player/recently-played` — up to 50 most recent plays with timestamps
- Used for incremental updates after the GDPR backbone is loaded
- Gap risk: if more than ~50 plays occur between syncs, history has holes
- Last sync timestamp stored in `data/last_sync.json`; surface sync age prominently in Settings tab

### Optional — MusicBrainz API (country enrichment)

- Free, no auth required
- Fuzzy match by artist name → country of origin
- Enables the choropleth map in the Artists tab
- Recommended as Phase 2; country field is nullable until enrichment runs

---

## API Enrichment Layer

The GDPR export requires two enrichment passes. Results are cached locally and
never re-fetched unless forced — same pattern as `gear_map.json` in strava-stats.

### Track metadata (`data/enriched/track_metadata.json`)

Keyed by `spotify_track_uri`. Fetched via `/tracks` endpoint (batch up to 50).

Fields to cache:
- `duration_ms` — needed to compute `full_listen`
- `release_date` — used to derive `decade`
- `album_name`, `album_id`
- `popularity` (snapshot; not historical)

### Artist metadata (`data/enriched/artist_metadata.json`)

Keyed by artist name or Spotify artist URI. Fetched via `/artists` endpoint (batch up to 50).

Fields to cache:
- `genres` — Spotify genre tags live on the artist, not the track
- `spotify_artist_uri`
- `popularity`, `followers`

### Country metadata (`data/enriched/country_metadata.json`) — Phase 2

Keyed by artist name. Fetched from MusicBrainz.

Fields to cache:
- `country` — ISO country code
- `mb_artist_id` — MusicBrainz ID for future lookups

---

## Project Structure

```
spotify-stats/
├── app.py                        # Streamlit dashboard — all tab render functions
├── run_pipeline.py               # CLI: load GDPR → enrich → process → save
│
├── src/
│   ├── config.py                 # Env vars, file paths, constants, defaults
│   ├── fetch_data.py             # Spotify OAuth, token refresh, incremental sync
│   ├── enrich_data.py            # Track + artist + MusicBrainz enrichment passes
│   ├── process_data.py           # pandas aggregations: by year, month, artist, genre
│   └── charts.py                 # Plotly figure factories (one function per chart type)
│
└── data/                         # All local data — not committed to git
    ├── raw/                      # GDPR export files (StreamingHistory_music_*.json)
    ├── enriched/
    │   ├── track_metadata.json   # Keyed by spotify_track_uri
    │   ├── artist_metadata.json  # Keyed by artist name / spotify_artist_uri
    │   └── country_metadata.json # Keyed by artist name (Phase 2)
    ├── processed/
    │   └── plays.parquet         # Merged, cleaned, fully enriched play log
    ├── last_sync.json            # Last API sync timestamp and play count
    └── settings.json             # User preferences and filter defaults
```

---

## Core DataFrame (`plays.parquet`)

One row per play. Built by `process_data.py` from raw + enriched sources.

| Column | Type | Source | Notes |
|---|---|---|---|
| `ts` | datetime64 (UTC) | GDPR | Timestamp of play |
| `ts_local` | datetime64 | derived | Converted to local time for hour/day analysis |
| `ms_played` | int | GDPR | Milliseconds played |
| `minutes_played` | float | derived | `ms_played / 60000` |
| `track_name` | str | GDPR | |
| `artist_name` | str | GDPR | |
| `album_name` | str | GDPR | |
| `track_uri` | str | GDPR | `spotify:track:xxx` |
| `skipped` | bool | GDPR | Spotify's own skip flag |
| `full_listen` | bool | derived | `ms_played > 0.8 * duration_ms` |
| `year` | int | derived | |
| `month` | int | derived | |
| `hour` | int | derived | Local time hour (0–23) |
| `day_of_week` | int | derived | 0=Monday, 6=Sunday |
| `release_year` | int | track enrichment | |
| `decade` | int | derived | `(release_year // 10) * 10` |
| `genres` | list[str] | artist enrichment | Multi-valued; explode for aggregation |
| `country` | str | MusicBrainz (Phase 2) | ISO code; nullable |
| `duration_ms` | int | track enrichment | Full track length |

### Key derived field notes

- **`full_listen`** requires `duration_ms` from track enrichment. Define threshold as
  configurable (default 80%) in `settings.json`.
- **`genres`** is list-valued. All genre aggregations require a `.explode('genres')` step.
- **`ts_local`** conversion requires knowing the user's timezone. Store in `settings.json`;
  default to system timezone on first run.
- **`country`** is nullable until MusicBrainz enrichment runs. Map tab gracefully handles
  missing values.

---

## Tab Structure

### 🎸 Artists

Answers: who do I listen to most, over what time periods, and where are they from?

- All-time top artists — sortable by play count or total minutes
- Top 5 per year — heatmap or small-multiples bar charts
- Top 5 per month — seasonal pattern view
- Country of origin choropleth map (requires Phase 2 MusicBrainz enrichment; degrades gracefully)
- Genre breakdown for top artists

### 🎵 Tracks

Answers: what specific songs define my listening?

- All-time top tracks by play count and total minutes
- Top tracks per year / per month
- Obsession detector — tracks played heavily in a short window then dropped
  (high play count within a 30-day window, low plays outside it)

### 💿 Albums

Answers: what albums do I actually listen to start-to-finish?

- All-time top albums by play count and total minutes
- Top albums per year
- Note: Spotify's own app neglects album-level analysis — good differentiator

### 🎼 Genres

Answers: what kind of music do I listen to, and how has that changed?

- Top genres all-time (bar chart)
- Genre drift — stacked area chart showing genre mix shift year over year
- Genre × decade cross-tab (e.g. do you listen to 90s rock but modern pop?)

### 📅 Decades

Answers: what era of music dominates my listening?

- Decade breakdown all-time
- Decade mix by year — stacked bar (are you drifting older or newer?)
- Oldest and newest average release years per calendar year

### 🗓️ Wrapped

Answers: what defined my listening in a given period?

- Pick any window: last 30 days, a specific year, all-time
- Outputs: top artist / track / album / genre, total plays, total minutes,
  listening days, mini play-count timeline
- Mirrors the Strava Wrapped tab pattern exactly

### 🕐 Patterns

Answers: when do I listen, and how consistent am I?

- Time-of-day heatmap — hour (0–23) × day-of-week grid, colored by play count
- Listening by month/season across all years
- Streaks — longest consecutive days with at least one play
- Gaps — longest silences (often revealing)
- Note: no equivalent in Spotify's own app — most unique tab in the dashboard

### 🔍 Explore

- Full-text search across all plays (track, artist, album)
- Filters: date range, artist, genre, decade, country
- Results table with CSV download

### 📤 Export

- Annual summaries, monthly breakdowns, full play log
- PNG and ZIP download (mirrors strava-stats Export tab)

### ⚙️ Settings

- Data sync status: last sync timestamp, total plays loaded, GDPR export date
- Sync Now button — incremental fetch from live API
- API connection status indicator
- User timezone (for local-time hour/day-of-week analysis)
- `full_listen` threshold (default 80%)
- Data source paths (GDPR export location)

---

## Data Flow

```
1. run_pipeline.py (or first app load)
   └── Load all StreamingHistory_music_*.json from data/raw/
   └── Merge into single plays list, deduplicate by ts + track_uri
   └── enrich_data.py:
       └── Batch fetch track metadata (duration, release_date, album)
       └── Batch fetch artist metadata (genres)
       └── [Phase 2] Fetch country from MusicBrainz
       └── Save to data/enriched/*.json
   └── process_data.py:
       └── Merge raw plays with enriched metadata
       └── Derive all computed columns
       └── Save to data/processed/plays.parquet

2. app.py (Streamlit, on load)
   └── load_plays() — cached with @st.cache_data
       └── Read plays.parquet
       └── Return DataFrame
   └── Each tab render function:
       └── Calls aggregation helpers from process_data.py
       └── Passes results to Plotly figure factories in charts.py

3. Sync Now (sidebar button)
   └── fetch_data.py: pull /me/player/recently-played (up to 50)
   └── Deduplicate against existing plays by ts + track_uri
   └── Append new rows, re-enrich if new tracks/artists found
   └── Re-save plays.parquet
   └── Update last_sync.json
   └── Clear @st.cache_data, reload
```

---

## Configuration

### Credentials — `.local.env`

```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8888/callback
```

### OAuth token — `data/spotify_tokens.json`

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1234567890,
  "token_type": "Bearer",
  "scope": "user-read-recently-played user-top-read"
}
```

### User preferences — `data/settings.json`

```json
{
  "timezone": "America/Denver",
  "full_listen_threshold": 0.80,
  "gdpr_export_date": "2024-01-15",
  "data_paths": {
    "raw": "data/raw",
    "enriched": "data/enriched",
    "processed": "data/processed"
  }
}
```

---

## Phase 2 Notes

- **MusicBrainz country enrichment** — unlocks the Artists choropleth map. Slow to run
  (rate-limited API), so treat as an offline enrichment step in `run_pipeline.py`, not
  a live fetch.
- **Podcast filtering** — GDPR export may include podcast plays. Filter by checking
  `spotify_track_uri` prefix (`spotify:episode:` vs `spotify:track:`).
- **Multi-user support** — not in scope; this is a personal dashboard.

---

## Relationship to strava-stats

| strava-stats | spotify-stats |
|---|---|
| `fetch_data.py` — Strava OAuth + incremental sync | `fetch_data.py` — Spotify OAuth + incremental sync |
| `process_data.py` — pandas aggregations | `process_data.py` — same pattern |
| `charts.py` — Plotly figure factories | `charts.py` — same pattern |
| `data/raw/my_strava_activities.json` | `data/raw/StreamingHistory_music_*.json` |
| `data/gear_map.json` | `data/enriched/track_metadata.json` + `artist_metadata.json` |
| `settings.json` — goals + conversions | `settings.json` — timezone + thresholds |
| Wrapped tab | Wrapped tab — identical pattern |
| Explore tab | Explore tab — identical pattern |
| Export tab | Export tab — identical pattern |
| Settings tab | Settings tab — extended with sync status |

The primary new complexity vs. strava-stats is the **enrichment layer** — Strava
activities arrive pre-enriched with all needed fields, while Spotify's GDPR export
requires API lookups to add genre, release year, duration, and (optionally) country.
