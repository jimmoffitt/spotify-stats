"""
src/setlistfm.py — setlist.fm API client + persistent cache for the
Concerts feature.

Looks up each library artist's real, crowd-sourced past shows in a given
city via GET /rest/1.0/search/setlists (api.setlist.fm/docs/1.0). This is
ground-truth event data, independent of proc.artist_concert_warmups()'s
listening-pattern heuristic — it catches shows for artists you saw live but
never binged on Spotify around the date (the heuristic's blind spot).

Auth is a flat x-api-key header (config.SETLISTFM_API_KEY), not OAuth — no
token refresh/callback flow needed. Free tier: non-commercial use only,
2 requests/sec / 1,440/day (see README for key setup).

Results are cached to disk (config.SETLISTFM_SHOWS_CACHE_FILE), keyed by
(city, artist name as queried) — same shape and same "fetch once, keep
forever unless asked again" philosophy as src/enrich_data.py's track/artist
metadata caches. Nothing here refetches on a schedule or a TTL; app.py only
ever calls fetch_shows() from an explicit button click (see
render_concert_settings / render_concert_search), and it's incremental by
default — only (artist, city) pairs not already cached hit the network,
so restarting the app or reloading the page never re-does work.
"""
import json
import os
import time
from datetime import datetime

import requests

from src import config


# --- disk cache ---

def load_cache(path=config.SETLISTFM_SHOWS_CACHE_FILE):
    """{city: {artist_name: [show dicts]}}, event_date as an ISO string on
    disk. Missing/unreadable file -> {}."""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache, path=config.SETLISTFM_SHOWS_CACHE_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _parse_cached(entries):
    return [{**s, 'event_date': datetime.fromisoformat(s['event_date']).date()}
            for s in entries]


def shows_for(cache, artist_names, cities):
    """Every cached show across these artist/city combinations, flattened
    — purely a cache read, no network calls. A combination not yet in the
    cache is silently skipped; call fetch_shows() first to fill gaps."""
    out = []
    for city in cities:
        by_artist = cache.get(city, {})
        for artist in artist_names:
            entry = by_artist.get(artist)
            if entry:
                out.extend(_parse_cached(entry))
    return out


# --- API client ---

def _get_with_retry(params, max_retries=5):
    """GET with 429 handling. setlist.fm doesn't document a Retry-After
    header, so back off with fixed exponential pauses rather than trusting
    one. A 404 means "no setlists for this artist/city" — not an error."""
    if not config.SETLISTFM_API_KEY:
        raise RuntimeError(
            "SETLISTFM_API_KEY is not set. Add it to .local.env — see "
            "README for how to request a free key at "
            "https://www.setlist.fm/settings/api.")
    headers = {'x-api-key': config.SETLISTFM_API_KEY, 'Accept': 'application/json'}
    for attempt in range(max_retries):
        response = requests.get(config.SETLISTFM_SEARCH_URL, headers=headers,
                                params=params)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return {'setlist': []}
        if response.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        raise ConnectionError(f"{config.SETLISTFM_SEARCH_URL} -> "
                              f"{response.status_code}: {response.text}")
    raise ConnectionError(f"{config.SETLISTFM_SEARCH_URL}: still rate-limited "
                          f"after {max_retries} retries.")


def artist_shows(artist_name, city):
    """Real, logged shows for `artist_name` in `city`, straight from the
    API (no cache involved — use fetch_shows()/shows_for() for the cached
    path). Returns a list of {artist_name, event_date (date), venue_name,
    city_name} dicts — event_date parsed from setlist.fm's dd-MM-yyyy into
    a plain date so it can be compared against listening timestamps.
    artist_name in each result is setlist.fm's own matched name, which can
    differ from the query (fuzzy matching — e.g. querying "Spoon" can turn
    up a "Spoon Benders" show too). Empty list if the artist has no logged
    shows there (not an error) — most of the library, most of the time."""
    data = _get_with_retry({'artistName': artist_name, 'cityName': city})
    shows = []
    for entry in data.get('setlist', []):
        try:
            event_date = datetime.strptime(entry['eventDate'], '%d-%m-%Y').date()
        except (KeyError, ValueError):
            continue  # malformed/missing date — skip rather than guess
        venue = entry.get('venue', {})
        shows.append({
            'artist_name': entry.get('artist', {}).get('name', artist_name),
            'event_date': event_date,
            'venue_name': venue.get('name'),
            'city_name': venue.get('city', {}).get('name'),
        })
    return shows


def fetch_shows(artist_names, cities, cache=None, force=False, progress_cb=None):
    """Incrementally fill `cache` (loaded fresh from disk if None) with
    artist_shows() for every (artist, city) pair — skipping any pair
    already cached unless force=True (for re-checking in case a new show
    was logged on setlist.fm since your last check). Saves to disk after
    every actual fetch, not just at the end, so a page reload or an error
    partway through never loses progress already made.

    progress_cb(done, total, artist, city, hit_network), if given, is
    called after every pair (cached or freshly fetched) for a caller-side
    progress bar.

    Returns (cache, fetched_count) — fetched_count is how many pairs
    actually hit the network (0 if everything was already cached and
    force=False, i.e. a no-op incremental check)."""
    if cache is None:
        cache = load_cache()
    pairs = [(a, c) for c in cities for a in artist_names]
    fetched_count = 0
    for i, (artist, city) in enumerate(pairs):
        already_cached = artist in cache.get(city, {})
        if already_cached and not force:
            if progress_cb:
                progress_cb(i + 1, len(pairs), artist, city, False)
            continue
        shows = artist_shows(artist, city)
        cache.setdefault(city, {})[artist] = [
            {**s, 'event_date': s['event_date'].isoformat()} for s in shows]
        fetched_count += 1
        save_cache(cache)
        if progress_cb:
            progress_cb(i + 1, len(pairs), artist, city, True)
        time.sleep(config.SETLISTFM_REQUEST_INTERVAL)
    return cache, fetched_count
