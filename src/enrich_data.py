"""
src/enrich_data.py — Track + artist + (Phase 2) MusicBrainz enrichment passes.

The GDPR export lacks duration, release date, and genre, so each unique track
and artist is looked up once via the Spotify API and cached to
data/enriched/*.json (never re-fetched unless force=True). See DESIGN.md
"API Enrichment Layer".

Resolution path for a play's genres:
    play.track_uri -> track_metadata.artist_ids -> artist_metadata.genres

Entry points:
    enrich_tracks(plays, access_token)   -> track_metadata.json   (/tracks,  batch 50)
    enrich_artists(track_cache, access_token) -> artist_metadata.json (/artists, batch 50)
"""
import json
import os
import time

import requests

from src import config


# --- cache helpers ---

def _load_cache(path):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️  Cache file {path} unreadable; starting fresh.")
    return {}


def _save_cache(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _batched(items, n=config.API_BATCH_SIZE):
    """Yield successive n-sized chunks from a list."""
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _uri_to_id(uri):
    """'spotify:track:ABC' -> 'ABC'. Returns None for falsy input."""
    return uri.rsplit(':', 1)[-1] if uri else None


def _get_with_retry(url, access_token, params, max_retries=5):
    """
    GET with Spotify 429 rate-limit handling. On 429, honor the Retry-After
    header and retry. Raises ConnectionError on non-recoverable failures.
    """
    headers = {'Authorization': f"Bearer {access_token}"}
    for attempt in range(max_retries):
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            wait = int(response.headers.get('Retry-After', 1)) + 1
            print(f"   Rate limited; waiting {wait}s...")
            time.sleep(wait)
            continue
        raise ConnectionError(f"{url} -> {response.status_code}: {response.text}")
    raise ConnectionError(f"{url}: still rate-limited after {max_retries} retries.")


# --- track enrichment ---

def enrich_tracks(plays, access_token, force=False):
    """
    Fetch + cache metadata for every unique music track URI in `plays`.

    Cached per track URI: duration_ms, release_date, album_name, album_id,
    popularity, artist_ids, artist_names. The artist_ids drive artist
    enrichment (genres live on the artist, not the track).
    """
    cache = {} if force else _load_cache(config.TRACK_METADATA_FILE)

    # Unique music-track URIs not already cached (skip podcasts + nulls).
    uris = {
        p['spotify_track_uri'] for p in plays
        if (p.get('spotify_track_uri') or '').startswith(config.TRACK_URI_PREFIX)
    }
    missing = sorted(u for u in uris if u not in cache)
    if not missing:
        print(f"✅ Track metadata: {len(cache)} cached, nothing to fetch.")
        return cache

    print(f"Track metadata: {len(missing)} new tracks to fetch "
          f"({len(uris)} unique, {len(cache)} already cached)...")

    for batch in _batched(missing):
        ids = ','.join(_uri_to_id(u) for u in batch)
        data = _get_with_retry(config.TRACKS_URL, access_token, {'ids': ids})
        for uri, track in zip(batch, data.get('tracks', [])):
            if track is None:  # Spotify returns null for unresolvable IDs
                continue
            album = track.get('album', {})
            cache[uri] = {
                'duration_ms': track.get('duration_ms'),
                'release_date': album.get('release_date'),
                'album_name': album.get('name'),
                'album_id': album.get('id'),
                'popularity': track.get('popularity'),
                'artist_ids': [a['id'] for a in track.get('artists', [])],
                'artist_names': [a['name'] for a in track.get('artists', [])],
            }

    _save_cache(config.TRACK_METADATA_FILE, cache)
    print(f"✅ Track metadata: {len(cache)} total cached.")
    return cache


# --- artist enrichment ---

def enrich_artists(track_cache, access_token, force=False):
    """
    Fetch + cache metadata for every unique artist ID referenced by the track
    cache. Cached per artist ID: name, genres, popularity, followers, uri.
    """
    cache = {} if force else _load_cache(config.ARTIST_METADATA_FILE)

    artist_ids = {aid for t in track_cache.values() for aid in t.get('artist_ids', [])}
    missing = sorted(a for a in artist_ids if a not in cache)
    if not missing:
        print(f"✅ Artist metadata: {len(cache)} cached, nothing to fetch.")
        return cache

    print(f"Artist metadata: {len(missing)} new artists to fetch "
          f"({len(artist_ids)} unique, {len(cache)} already cached)...")

    for batch in _batched(missing):
        ids = ','.join(batch)
        data = _get_with_retry(config.ARTISTS_URL, access_token, {'ids': ids})
        for aid, artist in zip(batch, data.get('artists', [])):
            if artist is None:
                continue
            cache[aid] = {
                'name': artist.get('name'),
                'genres': artist.get('genres', []),
                'popularity': artist.get('popularity'),
                'followers': artist.get('followers', {}).get('total'),
                'uri': artist.get('uri'),
            }

    _save_cache(config.ARTIST_METADATA_FILE, cache)
    print(f"✅ Artist metadata: {len(cache)} total cached.")
    return cache


# --- country enrichment (Phase 2) ---

def enrich_countries(track_cache, artist_cache, force=False):
    """
    Phase 2: fuzzy-match artist names to country of origin via MusicBrainz,
    cached to country_metadata.json. Not yet implemented — see DESIGN.md
    "Phase 2 Notes" (rate-limited; run offline, never as a live fetch).
    """
    raise NotImplementedError("MusicBrainz country enrichment is Phase 2.")
