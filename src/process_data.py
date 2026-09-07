"""
src/process_data.py — Merge raw plays with enriched metadata into plays.parquet.

build_plays_df() produces the one-row-per-play DataFrame described in DESIGN.md
"Core DataFrame": it filters podcasts/incomplete records, derives full_listen,
the time columns (year/month/hour/day_of_week in local time), release_year /
decade, and the exploded-ready genres list, then save_plays() writes
data/processed/plays.parquet. The remaining functions are the aggregation
helpers the Streamlit tabs call.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src import config


# --- settings ---

def load_settings():
    """Load data/settings.json, filling any missing keys from DEFAULT_SETTINGS.
    Merges one level deep for dict-valued settings (e.g. 'concert_warmup')
    rather than a flat dict.update() — otherwise adding a new sub-key to
    DEFAULT_SETTINGS (like concert_warmup['top_n']) would KeyError on any
    settings.json saved before that sub-key existed, instead of just
    filling it in alongside the user's already-saved values."""
    settings = dict(config.DEFAULT_SETTINGS)
    if os.path.exists(config.SETTINGS_FILE):
        with open(config.SETTINGS_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(settings.get(key), dict):
                settings[key] = {**settings[key], **value}
            else:
                settings[key] = value
    return settings


def save_settings(settings):
    os.makedirs(os.path.dirname(config.SETTINGS_FILE), exist_ok=True)
    with open(config.SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)


def load_exclusions(path=config.EXCLUSIONS_FILE):
    """
    Load artist exclusions. Schema is artist-centric — an "exclude" object maps
    each artist name to a rule describing which years to drop:

        {
          "exclude": {
            "Meghan Trainor": true,            # all years
            "Taylor Swift":   {"before": 2019},# years < 2019 (keep 2019+)
            "Some Artist":    {"after": 2020}, # years > 2020
            "Other Artist":   {"years": [2015, 2016]}
          }
        }

    Bounds accept a year ("2019") or a year-month ("2019-06") for monthly
    resolution. An optional "keep" fraction (0..1) claims only a share of the
    period's plays — e.g. {"before": 2020, "keep": 0.5} keeps half of the
    pre-2020 plays (a shared-account split). Missing file -> {}. before/after/
    years are OR'd (union semantics).
    """
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_exclusions(exclusions, path=config.EXCLUSIONS_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(exclusions, f, indent=2, ensure_ascii=False)


def load_groups(path=config.GROUPS_FILE):
    """
    Load saved band groups. Schema maps a group name to a list of artist names:

        {"New Zealand": ["Crowded House", "Lorde", "Fat Freddy's Drop"]}

    Keyed by artist name (matches df['artist_name']). Missing file -> {}.
    """
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_groups(groups, path=config.GROUPS_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)


def load_warmup_false_positives(path=config.WARMUP_FALSE_POSITIVES_FILE):
    """Artist names dismissed from the Concert warm-up table (see
    render_concert_warmups) — a flat JSON list, e.g. bands whose 'spike then
    crash' pattern the user has confirmed was never actually a show they
    went to. Missing file -> []."""
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_warmup_false_positives(names, path=config.WARMUP_FALSE_POSITIVES_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(sorted(set(names)), f, indent=2, ensure_ascii=False)


def load_confirmed_concerts(path=config.CONFIRMED_CONCERTS_FILE):
    """Concert candidates promoted via checkbox in render_concert_matches —
    a flat JSON list of {artist_name, event_date, venue_name, city_name}
    dicts (event_date as an ISO string). Missing file -> []."""
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_confirmed_concerts(concerts, path=config.CONFIRMED_CONCERTS_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(concerts, f, indent=2, ensure_ascii=False)






def concert_match_candidates(df, shows, correlation_days=3):
    """Rank every real setlist.fm show for a library artist by how
    elevated the artist's listening was in the days around it, compared
    to that artist's own normal (baseline) daily rate elsewhere in their
    history — the same "elevated vs. your normal rate" idea
    artist_concert_warmups() uses for a listening-detected spike window,
    applied here to a real show date instead. All shows are returned, not
    just correlated ones: setlist.fm's authority that a show happened
    doesn't depend on whether it left a mark on your listening history, so
    a show with zero nearby listening still appears — just ranked at the
    bottom (elevation 0) instead of dropped.

    One row per (artist_name, event_date, venue_name, city_name):
    nearby_minutes, nearby_plays (within +/- correlation_days), elevation
    (nearby daily rate / baseline daily rate — 0 if there's no nearby
    listening, or the artist has no listening history at all), sorted by
    elevation descending then nearby_minutes descending."""
    cols = ['artist_name', 'event_date', 'venue_name', 'city_name',
            'nearby_minutes', 'nearby_plays', 'elevation']
    if not shows:
        return pd.DataFrame(columns=cols)

    # tz_localize(None) first for the same reason as _concert_night_signal:
    # comparing a still-tz-aware series round-trips through its UTC instant,
    # which would shift the window boundaries by the local UTC offset.
    ts_local = df['ts_local'].dt.tz_localize(None)
    window = pd.Timedelta(days=correlation_days)
    window_days = max(2 * correlation_days, 1)

    # Baseline daily rate per artist referenced by `shows`, computed once
    # per artist (not per show — an artist can have several candidate
    # shows) from their *whole* history, not excluding any one show's
    # window. A simplification: an artist with one real listening bump
    # will have that bump nudge their own baseline up slightly, which very
    # lightly understates elevation — acceptable for a ranking signal, not
    # worth a per-show-excluded baseline's added complexity here.
    baseline_daily = {}
    for artist in {s['artist_name'] for s in shows}:
        g = df.loc[df['artist_name'] == artist]
        if g.empty:
            baseline_daily[artist] = 0.0
            continue
        span_days = max((g['ts'].max() - g['ts'].min()) / np.timedelta64(1, 'D'), 1.0)
        baseline_daily[artist] = g['minutes_played'].sum() / span_days

    rows = []
    for show in shows:
        event_ts = pd.Timestamp(show['event_date'])
        mask = ((df['artist_name'] == show['artist_name']) &
                (ts_local >= event_ts - window) & (ts_local <= event_ts + window))
        sub = df.loc[mask]
        nearby_minutes = sub['minutes_played'].sum()
        nearby_plays = len(sub)
        base = baseline_daily.get(show['artist_name'], 0.0)
        elevation = (nearby_minutes / window_days / base) if base > 0 else 0.0
        rows.append((show['artist_name'], show['event_date'], show['venue_name'],
                    show['city_name'], nearby_minutes, nearby_plays, elevation))
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(['elevation', 'nearby_minutes'], ascending=[False, False])
            .reset_index(drop=True))


def concert_nominations(warmups, matches, false_positives, match_window_days=14):
    """Merge the two concert-detection signals into one reviewable
    nomination queue for render_concert_review(): the listening-pattern
    warm-up heuristic (artist_concert_warmups() — one row per artist, a
    *guessed* show date) and the setlist.fm-ranked real show list
    (concert_match_candidates() — one row per real show, a *real* date).

    When an artist has both, the warm-up's guessed date (concert_night if
    detected, else spike_end) is matched to the closest real setlist.fm
    show within +/- match_window_days and folded into one row
    (signal='both', using the real date/venue) rather than shown as two
    separate rows that are almost certainly the same event. Everything
    else keeps its own row: warm-up-only candidates (no known venue, since
    the heuristic has no venue data) and setlist.fm shows with no matching
    warm-up spike (signal='setlistfm').

    `false_positives` (artist names) are excluded from both signals before
    merging — a dismissal applies to the artist across every source, not
    just one row.

    One row per nomination: artist_name, event_date, venue_name,
    city_name, signal, warmup_score, elevation, nearby_minutes. Ranked
    reverse-chronologically (most recent show date first); a confidence
    tier (both signals > setlist.fm with some listening support > warm-up
    only > setlist.fm with none) and score only break same-date ties."""
    cols = ['artist_name', 'event_date', 'venue_name', 'city_name', 'signal',
            'warmup_score', 'elevation', 'nearby_minutes']
    if not warmups.empty:
        warmups = warmups[~warmups['artist_name'].isin(false_positives)]
    if not matches.empty:
        matches = matches[~matches['artist_name'].isin(false_positives)]
    if warmups.empty and matches.empty:
        return pd.DataFrame(columns=cols)

    matches_by_artist = ({artist: g for artist, g in matches.groupby('artist_name')}
                         if not matches.empty else {})

    rows = []
    consumed = set()  # (artist_name, event_date) already folded into a 'both' row
    if not warmups.empty:
        for _, w in warmups.iterrows():
            artist = w['artist_name']
            guess_date = pd.Timestamp(
                w['concert_night'] if w['has_concert_night'] else w['spike_end']).date()
            best, best_diff = None, None
            for _, m in matches_by_artist.get(artist, pd.DataFrame()).iterrows():
                diff = abs((m['event_date'] - guess_date).days)
                if diff <= match_window_days and (best_diff is None or diff < best_diff):
                    best, best_diff = m, diff
            if best is not None:
                rows.append((artist, best['event_date'], best['venue_name'],
                            best['city_name'], 'both', w['warmup_score'],
                            best['elevation'], best['nearby_minutes']))
                consumed.add((artist, best['event_date']))
            else:
                rows.append((artist, guess_date, None, None, 'warmup',
                            w['warmup_score'], None, None))

    if not matches.empty:
        for _, m in matches.iterrows():
            if (m['artist_name'], m['event_date']) in consumed:
                continue
            rows.append((m['artist_name'], m['event_date'], m['venue_name'],
                        m['city_name'], 'setlistfm', None, m['elevation'],
                        m['nearby_minutes']))

    out = pd.DataFrame(rows, columns=cols)

    def _tier(row):
        if row['signal'] == 'both':
            return 0
        if row['signal'] == 'setlistfm':
            return 1 if (row['elevation'] or 0) > 0 else 3
        return 2  # warmup-only

    out['_tier'] = out.apply(_tier, axis=1)
    out['_score'] = out['elevation'].fillna(0) + out['warmup_score'].fillna(0)
    out['_date_sort'] = pd.to_datetime(out['event_date'])
    return (out.sort_values(['_date_sort', '_tier', '_score'],
                            ascending=[False, True, False])
            .drop(columns=['_tier', '_score', '_date_sort']).reset_index(drop=True))


def _month_index(year, month):
    """Map a (year, month) to a single comparable integer (months since year 0)."""
    return int(year) * 12 + (int(month) - 1)


def _bound_index(value, *, end):
    """
    Parse a 'YYYY' or 'YYYY-MM' bound into a month index. A year-only value
    resolves to December of that year for an upper (after) bound and January for
    a lower (before) bound, so 'before 2019' keeps all of 2019 and 'after 2020'
    keeps all of 2020. Returns None for unparseable input.
    """
    try:
        s = str(value).strip()
        if '-' in s:
            y, m = s.split('-')[:2]
            return _month_index(int(y), int(m))
        return _month_index(int(s), 12 if end else 1)
    except (ValueError, TypeError):
        return None


def _entry_match(entry, year, month_idx):
    """Boolean Series matching a 'years' entry — a year ('2019') or month ('2019-06')."""
    try:
        s = str(entry).strip()
        if '-' in s:
            y, m = s.split('-')[:2]
            return month_idx == _month_index(int(y), int(m))
        return year == int(s)
    except (ValueError, TypeError):
        return None


# Seed for reproducible partial-share sampling, so a "keep 50%" split drops the
# same rows on every run (stable play counts).
_EXCLUSION_SEED = 42


def apply_exclusions(df, exclusions):
    """
    Drop plays matching the artist exclusion rules (see load_exclusions for the
    schema). Matching is case-insensitive and resolves to the month. Returns a
    filtered copy; df is unchanged.

    For each artist the rule defines a *period* (True/"all" = every year;
    otherwise before / after / years OR'd together) and an optional `keep`
    fraction in [0, 1] — the share of that period's plays to keep. keep defaults
    to 0 (drop the whole period, the normal exclusion). keep=0.5 keeps half
    (a shared-account split); keep>=1 drops nothing.
    """
    if not exclusions or df.empty:
        return df
    rules = exclusions.get('exclude', {})
    if not rules:
        return df

    artist_lower = df['artist_name'].str.lower()
    year = df['year']
    month_idx = df['year'] * 12 + (df['month'] - 1)
    drop = pd.Series(False, index=df.index)

    # One O(n) hash-based grouping pass instead of a fresh O(n) string-
    # equality scan per rule. Profiled at ~1.2s for 286 real rules over
    # 232k rows before this change (94% of the function's time) — pandas
    # has no vectorized fast path for object-dtype string equality
    # (comp_method_OBJECT_ARRAY), so `artist_lower == name` cost roughly
    # the same whether or not that artist even appears in the data.
    # .indices maps each lowercased name to its row positions in one pass;
    # everything downstream is unchanged, just fed a mask built from a
    # dict lookup instead of a full re-scan.
    group_positions = artist_lower.groupby(artist_lower, sort=False).indices

    for artist, rule in rules.items():
        positions = group_positions.get(str(artist).lower())
        if positions is None:
            continue
        is_artist = pd.Series(False, index=df.index)
        is_artist.iloc[positions] = True
        if not is_artist.any():
            continue

        keep = 0.0
        if rule is True or rule == 'all':
            period = is_artist
        elif isinstance(rule, dict):
            keep = max(0.0, min(1.0, float(rule.get('keep') or 0.0)))
            window_keys = any(k in rule for k in ('before', 'after', 'years'))
            cond = pd.Series(False, index=df.index)
            before = _bound_index(rule.get('before'), end=False) if rule.get('before') is not None else None
            if before is not None:
                cond |= month_idx < before
            after = _bound_index(rule.get('after'), end=True) if rule.get('after') is not None else None
            if after is not None:
                cond |= month_idx > after
            for entry in (rule.get('years') or []):
                m = _entry_match(entry, year, month_idx)
                if m is not None:
                    cond |= m
            # No window keys at all => whole-artist period; window keys present
            # but unparseable => empty period (drop nothing), which is safe.
            period = is_artist if not window_keys else (is_artist & cond)
        else:
            continue

        if keep >= 1:
            continue  # keep everything in the period
        if keep <= 0:
            drop |= period
        else:
            period_idx = df.index[period]
            sampled = pd.Series(period_idx).sample(
                frac=1.0 - keep, random_state=_EXCLUSION_SEED).values
            drop |= df.index.isin(sampled)

    return df[~drop]


def resolve_timezone(settings):
    """
    Return a tzinfo for local-time conversion. Uses the IANA name in settings
    when present; otherwise falls back to the system's current local timezone.
    """
    tz_name = settings.get('timezone')
    if tz_name:
        return ZoneInfo(tz_name)
    return datetime.now().astimezone().tzinfo


# --- core DataFrame ---

def build_plays_df(plays, track_cache, artist_cache, settings=None):
    """
    Build the fully-enriched, one-row-per-play DataFrame.

    plays:        raw GDPR records (from fetch_data.load_gdpr_export)
    track_cache:  enrich_data track_metadata (keyed by track URI)
    artist_cache: enrich_data artist_metadata (keyed by artist ID)
    """
    settings = settings or load_settings()
    threshold = settings.get('full_listen_threshold',
                             config.DEFAULT_SETTINGS['full_listen_threshold'])
    tz = resolve_timezone(settings)

    # 1. Base frame from raw records, keeping only music tracks with a URI/name.
    rows = [
        {
            'ts': p['ts'],
            'ms_played': p.get('ms_played', 0),
            'track_name': p.get('master_metadata_track_name'),
            'artist_name': p.get('master_metadata_album_artist_name'),
            'album_name': p.get('master_metadata_album_album_name'),
            'track_uri': p.get('spotify_track_uri'),
            'skipped': bool(p.get('skipped', False)),
        }
        for p in plays
        if (p.get('spotify_track_uri') or '').startswith(config.TRACK_URI_PREFIX)
        and p.get('master_metadata_track_name')
    ]
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # 2. Timestamps. ts is UTC (ISO 'Z'); ts_local drives hour/day analysis.
    df['ts'] = pd.to_datetime(df['ts'], utc=True)
    df['ts_local'] = df['ts'].dt.tz_convert(tz)
    df['minutes_played'] = df['ms_played'] / 60000.0

    # 3. Track enrichment: duration, release year, decade, full_listen.
    df['duration_ms'] = pd.to_numeric(
        df['track_uri'].map(lambda u: (track_cache.get(u) or {}).get('duration_ms')),
        errors='coerce')
    df['full_listen'] = (
        df['duration_ms'].notna()
        & (df['ms_played'] > threshold * df['duration_ms'].fillna(0))
    )
    df['release_year'] = df['track_uri'].map(
        lambda u: _release_year((track_cache.get(u) or {}).get('release_date')))
    df['decade'] = (df['release_year'] // 10 * 10).astype('Int64')
    df['album_id'] = df['track_uri'].map(
        lambda u: (track_cache.get(u) or {}).get('album_id'))
    df['album_image_url'] = df['track_uri'].map(
        lambda u: (track_cache.get(u) or {}).get('album_image_url'))

    # 4. Artist enrichment: genres (union across the track's artists), image
    # (the *primary* artist's — unlike genres, an image can't be usefully
    # unioned across multiple artists on a track).
    df['genres'] = df['track_uri'].map(
        lambda u: _genres_for_track(track_cache.get(u), artist_cache))
    df['artist_image_url'] = df['track_uri'].map(
        lambda u: _primary_artist_image(track_cache.get(u), artist_cache))

    # 5. Derived time columns (local).
    df['year'] = df['ts_local'].dt.year
    df['month'] = df['ts_local'].dt.month
    df['hour'] = df['ts_local'].dt.hour
    df['day_of_week'] = df['ts_local'].dt.dayofweek  # 0=Mon .. 6=Sun

    # 6. Country is Phase 2 (nullable until MusicBrainz enrichment runs).
    df['country'] = pd.NA

    return df


def _release_year(release_date):
    """
    Parse a Spotify release_date ('YYYY', 'YYYY-MM', or 'YYYY-MM-DD') to a year.
    Implausible years are treated as missing — Spotify uses placeholders like
    '0000' and '1900' for unknown dates, which otherwise pollute the decade view.
    """
    if not release_date:
        return pd.NA
    try:
        year = int(str(release_date)[:4])
    except ValueError:
        return pd.NA
    if year < 1920 or year > datetime.now().year + 1:
        return pd.NA
    return year


def _genres_for_track(track_meta, artist_cache):
    """Union of genres across all of a track's artists, de-duplicated, ordered."""
    if not track_meta:
        return []
    seen, genres = set(), []
    for aid in track_meta.get('artist_ids', []):
        for g in (artist_cache.get(aid) or {}).get('genres', []):
            if g not in seen:
                seen.add(g)
                genres.append(g)
    return genres


def _primary_artist_image(track_meta, artist_cache):
    """Image URL for a track's *primary* (first-listed) artist."""
    if not track_meta:
        return None
    artist_ids = track_meta.get('artist_ids') or []
    if not artist_ids:
        return None
    return (artist_cache.get(artist_ids[0]) or {}).get('image_url')


# --- parquet I/O ---

def save_plays(df, path=config.PLAYS_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path, index=False)
    print(f"✅ Wrote {len(df)} plays to {path}")


def load_plays(path=config.PLAYS_FILE):
    return pd.read_parquet(path)


# --- aggregation helpers (used by the dashboard tabs) ---

def _agg_counts(df, group_col, metric='plays'):
    """Plays + total minutes per group, sorted by `metric` ('plays'/'minutes') desc."""
    out = (df.groupby(group_col)
             .agg(plays=('ts', 'size'),
                  minutes=('minutes_played', 'sum'))
             .reset_index())
    out['minutes'] = out['minutes'].round(1)
    return out.sort_values(metric, ascending=False)


def top_artists(df, n=20, metric='plays'):
    return _agg_counts(df, 'artist_name', metric).head(n)


def _top_by_key_with_mode_label(df, key_cols, label_col, n, metric):
    """Aggregate plays/minutes over `key_cols`, labeling each group with its
    most-common `label_col` value (the mode) — without pandas' generic
    per-group Python path, which is what actually made top_tracks/top_albums
    slow (~1-2s each over the full archive, dominated by calling .mode() once
    per group). Grouping by (*key_cols, label_col) first and picking the
    label with the most rows via a vectorized idxmax is the same result,
    computed ~10x faster."""
    counts = (df.groupby([*key_cols, label_col])
                .agg(plays=('ts', 'size'), minutes=('minutes_played', 'sum'))
                .reset_index())
    winners = counts.loc[counts.groupby(key_cols)['plays'].idxmax(),
                         [*key_cols, label_col]]
    agg = (counts.groupby(key_cols)
                 .agg(plays=('plays', 'sum'), minutes=('minutes', 'sum'))
                 .reset_index())
    out = agg.merge(winners, on=key_cols)
    out['minutes'] = out['minutes'].round(1)
    # key_cols[0] (track_uri / album_id) is kept alongside the display label
    # — needed to look up e.g. album art for a top_tracks() row without a
    # second, separately-keyed query.
    return out.sort_values(metric, ascending=False).head(n)[
        [key_cols[0], label_col, 'artist_name', 'plays', 'minutes']]


def top_tracks(df, n=20, metric='plays'):
    """Top tracks by `metric`, grouped by track_uri rather than track name.
    Spotify sometimes edits a track's display title after release (e.g. a
    remaster relabeled '2010 Remastered' -> 'Remastered 2010', or a curly vs.
    straight quote in the name) — grouping by name would silently split one
    song's plays across near-duplicate rows."""
    return _top_by_key_with_mode_label(
        df, ['track_uri', 'artist_name'], 'track_name', n, metric)


def top_albums(df, n=20, metric='plays'):
    """Top albums by `metric`, grouped by album_id rather than album name — the
    same rationale as top_tracks: reissues/deluxe editions relabel the album
    name (e.g. 'Stoney' vs 'Stoney - Deluxe') without changing the tracks."""
    return _top_by_key_with_mode_label(
        df.dropna(subset=['album_id']), ['album_id', 'artist_name'],
        'album_name', n, metric)


def _first_image_by_key(df, key_col, image_col):
    """A {key: first non-null image URL} lookup — the shared piece behind
    all three top_*_images() functions below. Empty (not a KeyError) for a
    plays.parquet built before image_col existed — same backward-
    compatibility tolerance as artist_facts()'s image_url."""
    if image_col not in df.columns:
        return pd.Series(dtype=object)
    return (df.dropna(subset=[image_col])
              .drop_duplicates(subset=[key_col])
              .set_index(key_col)[image_col])


def top_artist_images(df, n=10, metric='plays'):
    """(label, image_url) pairs for the top-N artists — the image banner
    atop the Artists tab. Artists with no captured image are skipped
    rather than shown with a blank tile, so the banner is always full."""
    top = top_artists(df, n=n * 2, metric=metric)  # headroom for skips
    lookup = _first_image_by_key(df, 'artist_name', 'artist_image_url')
    out = [(row['artist_name'], lookup.get(row['artist_name']))
           for _, row in top.iterrows()]
    return [(label, url) for label, url in out if pd.notna(url)][:n]


def top_track_images(df, n=10, metric='plays'):
    """(label, image_url) pairs for the top-N tracks' *album art* (a track
    has no image of its own) — the banner atop the Tracks tab."""
    top = top_tracks(df, n=n * 2, metric=metric)
    lookup = _first_image_by_key(df, 'track_uri', 'album_image_url')
    out = [(f"{row['track_name']} — {row['artist_name']}", lookup.get(row['track_uri']))
           for _, row in top.iterrows()]
    return [(label, url) for label, url in out if pd.notna(url)][:n]


def top_album_images(df, n=10, metric='plays'):
    """(label, image_url) pairs for the top-N albums — the banner atop the
    Albums tab."""
    top = top_albums(df, n=n * 2, metric=metric)
    lookup = _first_image_by_key(df, 'album_id', 'album_image_url')
    out = [(f"{row['album_name']} — {row['artist_name']}", lookup.get(row['album_id']))
           for _, row in top.iterrows()]
    return [(label, url) for label, url in out if pd.notna(url)][:n]


def top_genres(df, n=20, metric='plays'):
    """Explode the list-valued genres column before aggregating."""
    exploded = df.explode('genres').dropna(subset=['genres'])
    return _agg_counts(exploded, 'genres', metric).head(n)


def top_artists_by_genre(df, genre, n=10, metric='plays', is_macro=False):
    """Top-N artists tagged with `genre` — the Genres tab's per-genre band
    leaderboard. `genre=None` returns top artists overall (the "All"
    option), same ranking as top_artists() but with a rank column added
    for direct table display.

    `is_macro=False` (default) matches `genre` exactly against a raw
    micro-genre tag (e.g. 'indie rock'). `is_macro=True` matches it
    against a genre *family* (e.g. 'Rock / Indie') via the same
    keyword classification the treemap/pie chart use — a band counts if
    any of its tags fall in that family, not just one specific tag."""
    if not genre:
        sub = df
    elif is_macro:
        exploded = _explode_with_macro_genre(df)
        sub = exploded[exploded['macro_genre'] == genre]
    else:
        exploded = df.explode('genres').dropna(subset=['genres'])
        sub = exploded[exploded['genres'] == genre]
    out = _agg_counts(sub, 'artist_name', metric).head(n).reset_index(drop=True)
    out.insert(0, 'rank', out.index + 1)
    return out


# Spotify's genre taxonomy is hundreds of narrow micro-genres (e.g. 'jangle
# pop', 'power pop', 'dream pop', 'art pop' all separately) that fragment any
# flat ranking. This buckets them into ~8 broad families by keyword, checked
# in this order so overlapping words resolve sensibly (e.g. 'folk punk' hits
# Punk before Folk, 'alt country' hits Folk / Americana before Rock / Indie).
# The order here is search priority only — display color is assigned by
# _GENRE_MACRO_COLOR_ORDER below, independent of this list, so a genre family
# keeps the same color regardless of how big it is in any given date range.
_GENRE_MACRO_RULES = [
    ('Hip-Hop / R&B', ['hip hop', 'rap', 'r&b', 'soul', 'trap', 'grime']),
    ('Electronic', ['edm', 'house', 'techno', 'electro', 'synth', 'idm',
                     'dubstep', 'drum and bass', 'trance', 'downtempo', 'disco']),
    ('World / Reggae / Jazz', ['reggae', 'dub', 'ska', 'ragga', 'dancehall',
                                'rocksteady', 'jazz', 'classical', 'blues',
                                'latin', 'world music', 'gospel']),
    ('Metal', ['metal', 'doom', 'grindcore', 'sludge']),
    ('Punk', ['punk', 'riot grrrl', 'hardcore']),
    ('Folk / Americana', ['country', 'americana', 'bluegrass',
                           'southern gothic', 'roots', 'honky', 'folk']),
    ('Rock / Indie', ['rock', 'indie', 'new wave', 'grunge', 'shoegaze',
                       'madchester']),
    ('Pop', ['pop']),
]
_GENRE_MACRO_OTHER = 'Other'

# Fixed display order/color slots (see charts.genre_treemap) — a family's
# color never changes when a filter shrinks or grows the data.
GENRE_MACRO_COLOR_ORDER = [
    'Rock / Indie', 'Pop', 'Folk / Americana', 'Punk',
    'Hip-Hop / R&B', 'Electronic', 'Metal', 'World / Reggae / Jazz',
]


def _macro_genre(genre):
    low = genre.lower()
    for macro, keywords in _GENRE_MACRO_RULES:
        if any(k in low for k in keywords):
            return macro
    return _GENRE_MACRO_OTHER


def _explode_with_macro_genre(df):
    """Shared first step for genre_group_treemap_data/macro_genre_breakdown:
    explode the list-valued genres column, then classify each row's macro
    genre family. Classifies via a lookup built from the *unique* genre
    strings rather than df['genres'].map(_macro_genre) directly — profiled
    at 0.43s for 251k exploded rows vs. 0.01s building+applying a ~580-
    entry dict first (same keyword-matching logic, just run once per
    distinct genre instead of once per row)."""
    exploded = df.explode('genres').dropna(subset=['genres']).copy()
    macro_lookup = {g: _macro_genre(g) for g in exploded['genres'].unique()}
    exploded['macro_genre'] = exploded['genres'].map(macro_lookup)
    return exploded


def genre_group_treemap_data(df, metric='plays', top_micro_per_macro=6):
    """Two-level treemap data: macro genre family -> its top micro-genres.
    Returns a tidy frame with one row per node (macro rows have
    parent_id=''), ready for charts.genre_treemap(). Each macro's own
    value is its *full* total (all micro-genres, not just the ones shown
    as children), so the macro block sizes reflect true totals even though
    only the top few micro-genres are broken out inside it."""
    exploded = _explode_with_macro_genre(df)

    micro = (exploded.groupby(['macro_genre', 'genres'])
                     .agg(plays=('ts', 'size'), minutes=('minutes_played', 'sum'))
                     .reset_index())
    micro['minutes'] = micro['minutes'].round(1)
    micro = micro.sort_values(['macro_genre', metric], ascending=[True, False])
    micro['rank'] = micro.groupby('macro_genre').cumcount() + 1
    micro_top = micro[micro['rank'] <= top_micro_per_macro]

    macro = (micro.groupby('macro_genre')
                  .agg(plays=('plays', 'sum'), minutes=('minutes', 'sum'))
                  .reset_index())
    macro['minutes'] = macro['minutes'].round(1)

    rows = [
        {'id': m['macro_genre'], 'label': m['macro_genre'], 'parent_id': '',
         'value': m[metric]}
        for m in macro.to_dict('records')
    ]
    rows += [
        {'id': f"{r['macro_genre']}::{r['genres']}", 'label': r['genres'],
         'parent_id': r['macro_genre'], 'value': r[metric]}
        for r in micro_top.to_dict('records')
    ]
    return pd.DataFrame(rows)


def macro_genre_breakdown(df, metric='plays'):
    """Macro genre family totals only (no micro-genre breakout) — the pie
    slice data for the Genres tab, and the top level of
    genre_group_treemap_data's tree. Rows are ordered by
    GENRE_MACRO_COLOR_ORDER (with 'Other' last) rather than by size, so a
    family's slice/legend position — and therefore its assigned color —
    stays fixed regardless of which is biggest in the current date range."""
    exploded = _explode_with_macro_genre(df)
    agg = (exploded.groupby('macro_genre')
                   .agg(plays=('ts', 'size'), minutes=('minutes_played', 'sum'))
                   .reset_index())
    agg['minutes'] = agg['minutes'].round(1)
    order = {name: i for i, name in enumerate(GENRE_MACRO_COLOR_ORDER + [_GENRE_MACRO_OTHER])}
    agg['_order'] = agg['macro_genre'].map(order)
    agg = agg.sort_values('_order').drop(columns='_order').reset_index(drop=True)
    total = agg[metric].sum()
    agg['pct'] = (agg[metric] / total * 100).round(1) if total else 0.0
    return agg


def _sliding_window_peaks(df, group_cols, window_days=7):
    """For each group, find the [window_days]-day window (a true sliding
    window ending at some play — not calendar-aligned bins, so a binge
    spanning a week boundary isn't split and undercounted) with the max
    summed minutes_played. One row per group: peak_hours, peak_start,
    peak_end, plays_in_window, lifetime_plays, total_hours, concentration
    (peak_hours / total_hours — how much of the group's entire relationship
    with you happened in that one window).

    Implemented per-group with cumsum + np.searchsorted (each a single
    vectorized call over that group's plays) rather than a per-row Python
    loop — validated on the real archive at ~1.3s for ~40k track groups."""
    window = np.timedelta64(window_days, 'D')
    rows = []
    for key, g in df.sort_values('ts').groupby(group_cols, sort=False, observed=True):
        ts = g['ts'].values
        minutes = g['minutes_played'].values
        cum = np.concatenate(([0.0], np.cumsum(minutes)))
        left_idx = np.searchsorted(ts, ts - window, side='right')
        n = len(ts)
        idx = np.arange(1, n + 1)
        window_sum = cum[idx] - cum[left_idx]
        window_count = idx - left_idx
        i = int(np.argmax(window_sum))
        total_hours = cum[-1] / 60.0
        peak_hours = window_sum[i] / 60.0
        rows.append((*(key if isinstance(key, tuple) else (key,)),
                     peak_hours, ts[left_idx[i]], ts[i], int(window_count[i]),
                     n, total_hours, peak_hours / total_hours if total_hours > 0 else 0.0))
    cols = list(group_cols) if isinstance(group_cols, list) else [group_cols]
    return pd.DataFrame(rows, columns=cols + ['peak_hours', 'peak_start', 'peak_end',
                                               'plays_in_window', 'lifetime_plays',
                                               'total_hours', 'concentration'])


def track_binges(df, window_days=7):
    """Every track's binge-peak stats, sorted by binge_score (peak_hours x
    concentration) descending — a short-lived spike outranks an all-time
    favorite that merely had one good week. Grouped by (track_uri,
    artist_name), same title-drift rationale as top_tracks, with a
    representative track_name via the same most-common-label lookup."""
    peaks = _sliding_window_peaks(df, ['track_uri', 'artist_name'], window_days)
    name_lookup = (df.groupby(['track_uri', 'track_name']).size()
                     .reset_index(name='n').sort_values('n', ascending=False)
                     .drop_duplicates('track_uri').set_index('track_uri')['track_name'])
    peaks['track_name'] = peaks['track_uri'].map(name_lookup)
    peaks['binge_score'] = peaks['peak_hours'] * peaks['concentration']
    return peaks.sort_values('binge_score', ascending=False).reset_index(drop=True)


def artist_binges(df, window_days=7):
    """Same as track_binges, grouped by artist_name."""
    peaks = _sliding_window_peaks(df, 'artist_name', window_days)
    peaks['binge_score'] = peaks['peak_hours'] * peaks['concentration']
    return peaks.sort_values('binge_score', ascending=False).reset_index(drop=True)


def plays_by_year(df):
    return _agg_counts(df, 'year').sort_values('year')


def top_artists_per_year(df, n=10, metric='plays'):
    """
    Top-N artists for every year, as a tidy long-format DataFrame with columns
    [year, rank, artist_name, plays, minutes]. `metric` ('plays' or 'minutes')
    chooses the ranking dimension; ties are broken alphabetically so the
    ranking is deterministic.
    """
    counts = (df.groupby(['year', 'artist_name'])
                .agg(plays=('ts', 'size'),
                     minutes=('minutes_played', 'sum'))
                .reset_index())
    counts['minutes'] = counts['minutes'].round(1)
    counts = counts.sort_values(['year', metric, 'artist_name'],
                                ascending=[True, False, True])
    counts['rank'] = counts.groupby('year').cumcount() + 1
    out = counts[counts['rank'] <= n]
    return out[['year', 'rank', 'artist_name', 'plays', 'minutes']].reset_index(drop=True)


def top_artists_wide(df, n=10, metric='minutes', show_values=False):
    """
    Wide 'rank chart' view: rows are ranks 1..n, columns are years, each cell is
    the artist holding that rank that year (ranked by `metric`). With
    show_values=True the cell becomes 'Artist (1,234)' using the metric value.
    """
    long = top_artists_per_year(df, n=n, metric=metric)
    if show_values:
        long = long.copy()
        long['cell'] = long.apply(
            lambda r: f"{r['artist_name']} ({r[metric]:,.0f})", axis=1)
        value_col = 'cell'
    else:
        value_col = 'artist_name'
    wide = long.pivot(index='rank', columns='year', values=value_col)
    wide.columns = [str(c) for c in wide.columns]
    return wide


def wide_to_markdown(wide, title=None):
    """Render a wide rank-chart DataFrame as a Markdown table (Rank | year | ...)."""
    years = [str(c) for c in wide.columns]
    lines = []
    if title:
        lines += [f"# {title}", ""]
    lines.append("| Rank | " + " | ".join(years) + " |")
    lines.append("|" + "------|" * (len(years) + 1))
    for rank in wide.index:
        cells = ["" if pd.isna(wide.loc[rank, y]) else str(wide.loc[rank, y])
                 for y in wide.columns]
        lines.append(f"| {rank} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def decade_breakdown(df):
    """Plays + minutes per release decade (drops plays lacking release data)."""
    return _agg_counts(df.dropna(subset=['decade']), 'decade').sort_values('decade')


def _top_per_group_with_mode_label(df, group_col, key_cols, label_col, n, metric):
    """Same title-drift-safe dedup as _top_by_key_with_mode_label (grouping by
    an id column plus a mode-picked display label, e.g. track_uri -> track_name),
    but ranked separately within each value of `group_col` (e.g. one top-N
    per decade) rather than once globally."""
    counts = (df.groupby([group_col, *key_cols, label_col])
                .agg(plays=('ts', 'size'), minutes=('minutes_played', 'sum'))
                .reset_index())
    winners = counts.loc[counts.groupby([group_col, *key_cols])['plays'].idxmax(),
                         [group_col, *key_cols, label_col]]
    agg = (counts.groupby([group_col, *key_cols])
                 .agg(plays=('plays', 'sum'), minutes=('minutes', 'sum'))
                 .reset_index())
    out = agg.merge(winners, on=[group_col, *key_cols])
    out['minutes'] = out['minutes'].round(1)
    out = out.sort_values([group_col, metric, label_col], ascending=[True, False, True])
    out['rank'] = out.groupby(group_col).cumcount() + 1
    return out[out['rank'] <= n][
        [group_col, 'rank', label_col, *key_cols, 'plays', 'minutes']].reset_index(drop=True)


def top_artists_per_decade(df, n=10, metric='plays', min_decade=None):
    """Top-N artists for every release decade, tidy long format (like
    top_artists_per_year but grouped by release decade instead of play year).
    `min_decade` (e.g. 1960) drops earlier decades — mostly placeholder/junk
    release dates rather than real listening."""
    sub = df.dropna(subset=['decade'])
    if min_decade is not None:
        sub = sub[sub['decade'] >= min_decade]
    counts = (sub.groupby(['decade', 'artist_name'])
                .agg(plays=('ts', 'size'), minutes=('minutes_played', 'sum'))
                .reset_index())
    counts['minutes'] = counts['minutes'].round(1)
    counts = counts.sort_values(['decade', metric, 'artist_name'],
                                ascending=[True, False, True])
    counts['rank'] = counts.groupby('decade').cumcount() + 1
    out = counts[counts['rank'] <= n]
    return out[['decade', 'rank', 'artist_name', 'plays', 'minutes']].reset_index(drop=True)


def _decade_wide(long, cell_col, min_decade):
    """Shared pivot for the decade rank-chart tables: rows are ranks 1..n,
    columns are decades ('1960s', '1970s', ...) ascending from `min_decade`."""
    if long.empty:
        return long
    wide = long.pivot(index='rank', columns='decade', values=cell_col)
    wide = wide[sorted(wide.columns)]
    wide.columns = [f"{int(c)}s" for c in wide.columns]
    return wide


def top_artists_by_decade_wide(df, n=10, metric='plays', min_decade=1960):
    """Wide rank-chart view of top_artists_per_decade — ranks down the side,
    decades across the top, same layout as top_artists_wide()."""
    long = top_artists_per_decade(df, n=n, metric=metric, min_decade=min_decade)
    return _decade_wide(long, 'artist_name', min_decade)


def top_tracks_per_decade(df, n=10, metric='plays', min_decade=None):
    """Top-N tracks for every release decade, grouped by track_uri (not name)
    for the same title-drift reasons as top_tracks()."""
    sub = df.dropna(subset=['decade'])
    if min_decade is not None:
        sub = sub[sub['decade'] >= min_decade]
    return _top_per_group_with_mode_label(
        sub, 'decade', ['track_uri', 'artist_name'], 'track_name', n, metric)


def top_tracks_by_decade_wide(df, n=10, metric='plays', min_decade=1960):
    """Wide rank-chart view of top_tracks_per_decade; each cell is
    'Track — Artist' since track titles alone can be ambiguous/repeated."""
    long = top_tracks_per_decade(df, n=n, metric=metric, min_decade=min_decade)
    if long.empty:
        return long
    long = long.copy()
    long['cell'] = long['track_name'] + ' — ' + long['artist_name']
    return _decade_wide(long, 'cell', min_decade)


def _concert_night_signal(ts, hour_local, date_local, minutes, spike_start, spike_end,
                          late_start=22, late_end=1, aft_start=15, aft_end=18):
    """Within one artist's [spike_start, spike_end] window, find the best
    candidate 'show night': the calendar day whose late_start-late_end
    local-time window (22:00-01:00 by default — the classic drive-home-
    from-a-show pattern) has the most listening, plus that same evening's
    aft_start-aft_end minutes (15:00-18:00 — a pre-show session). A play
    between midnight and `late_end` belongs to the *previous* evening's
    show night (you left the venue before midnight), so its date is
    shifted back a day before grouping.

    Returns (concert_night_date, late_night_minutes, afternoon_minutes);
    (None, 0.0, 0.0) if there's no late-night listening in the window."""
    in_window = (ts >= spike_start) & (ts <= spike_end)
    if not in_window.any():
        return None, 0.0, 0.0
    w_hour, w_minutes, w_date = hour_local[in_window], minutes[in_window], date_local[in_window]
    show_date = np.where(w_hour < late_end, w_date - np.timedelta64(1, 'D'), w_date)
    late_mask = (w_hour >= late_start) | (w_hour < late_end)
    if not late_mask.any():
        return None, 0.0, 0.0
    late_by_day = pd.Series(w_minutes[late_mask]).groupby(show_date[late_mask]).sum()
    concert_night = late_by_day.idxmax()
    late_night_minutes = late_by_day.max()
    aft_mask = (w_hour >= aft_start) & (w_hour < aft_end) & (show_date == concert_night)
    afternoon_minutes = w_minutes[aft_mask].sum()
    return pd.Timestamp(concert_night), late_night_minutes, afternoon_minutes


def artist_concert_warmups(df, spike_days=14, cooldown_days=2,
                           min_spike_hours=2.0, elevation_ratio=3.0):
    """Bands with a "charge up, then crash" listening shape: a concentrated
    burst over a `spike_days`-day window, followed by a sharp drop in the
    `cooldown_days` days right after — the pattern of hyping up for a show,
    then coming back down from it (as opposed to track/artist "binges",
    which just rank the single most concentrated window regardless of what
    follows it). `cooldown_days` defaults to 2, not spike_days, since the
    crash is usually a same-day/next-day thing, not a slow fade.

    For each artist, finds the spike_days-day sliding window (same
    cumsum/searchsorted approach as _sliding_window_peaks) with the most
    listening. Two gates keep this from just returning "biggest window
    ever" for whichever artist you play the most in general:
      - `min_spike_hours`: the window must clear this many total hours.
      - `elevation_ratio`: the window's daily rate must be at least this
        many times the artist's *own* baseline daily rate (their total
        listening outside that window, divided by the days outside it) —
        an artist you always play a lot would otherwise trivially have a
        "biggest window ever" that isn't actually elevated for them.
    Artists whose most recent play is within cooldown_days of their spike
    (no runway to measure a "return to normal") are dropped — can't tell a
    crash from "still going".

    This compiles the candidate list and its warmup_score exactly as
    before — that part is intentionally untouched. Each candidate's own
    selected window is additionally checked for a concert_night_signal
    (see above): a late-night (10pm-1am) listening cluster, the classic
    "drove home from a show" pattern, optionally corroborated by a same-
    day 3-6pm pre-show session. That's returned as extra columns
    (concert_night, late_night_minutes, afternoon_minutes, has_concert_night)
    for the caller to use as a *re-ranking* signal on top of this list —
    it doesn't filter or change warmup_score/the default sort.

    One row per qualifying artist: spike_hours, spike_start, spike_end,
    cooldown_hours, drop_pct (share of the spike's daily rate lost right
    after — 1.0 is a full stop, 0 is no change), warmup_score = spike_hours
    * drop_pct, sorted descending."""
    window = np.timedelta64(spike_days, 'D')
    cooldown = np.timedelta64(cooldown_days, 'D')
    latest_overall = df['ts'].values.max()  # numpy datetime64, matching per-group `ts` below
    rows = []
    for artist, g in df.sort_values('ts').groupby('artist_name', sort=False, observed=True):
        ts = g['ts'].values
        minutes = g['minutes_played'].values
        cum = np.concatenate(([0.0], np.cumsum(minutes)))
        left_idx = np.searchsorted(ts, ts - window, side='right')
        n = len(ts)
        idx = np.arange(1, n + 1)
        window_sum = cum[idx] - cum[left_idx]
        i = int(np.argmax(window_sum))
        spike_start, spike_end = ts[left_idx[i]], ts[i]
        spike_hours = window_sum[i] / 60.0
        if spike_hours < min_spike_hours or latest_overall - spike_end < cooldown:
            continue

        span_days = (ts[-1] - ts[0]) / np.timedelta64(1, 'D')
        baseline_days = max(span_days - spike_days, 1.0)
        baseline_hours = max(minutes.sum() / 60.0 - spike_hours, 0.0)
        baseline_daily = baseline_hours / baseline_days
        spike_daily = spike_hours / spike_days
        if baseline_daily > 0 and spike_daily < baseline_daily * elevation_ratio:
            continue  # not actually elevated vs. how much you normally play them

        cool_mask = (ts > spike_end) & (ts <= spike_end + cooldown)
        cooldown_hours = minutes[cool_mask].sum() / 60.0
        cooldown_daily = cooldown_hours / cooldown_days
        drop_pct = max(0.0, 1 - cooldown_daily / spike_daily)

        hour_local = g['ts_local'].dt.hour.values
        # tz_localize(None) first: normalize()/.values on a still-tz-aware
        # series round-trips through its UTC instant, so the grouping key
        # (and the displayed concert_night date) would be off by the local
        # UTC offset. Stripping tz here keeps it a plain local calendar date.
        date_local = g['ts_local'].dt.tz_localize(None).dt.normalize().values
        concert_night, late_night_minutes, afternoon_minutes = _concert_night_signal(
            ts, hour_local, date_local, minutes, spike_start, spike_end)

        rows.append((artist, spike_hours, spike_start, spike_end,
                     cooldown_hours, drop_pct, spike_hours * drop_pct,
                     concert_night, late_night_minutes, afternoon_minutes))
    out = pd.DataFrame(rows, columns=['artist_name', 'spike_hours', 'spike_start',
                                       'spike_end', 'cooldown_hours', 'drop_pct',
                                       'warmup_score', 'concert_night',
                                       'late_night_minutes', 'afternoon_minutes'])
    if not out.empty:
        # >= 15 min of late-night listening on the best candidate night —
        # roughly a couple of tracks, not one stray skip-through — counts as
        # corroborating "drove home from a show" evidence.
        out['has_concert_night'] = out['late_night_minutes'] >= 15.0
    return out.sort_values('warmup_score', ascending=False).reset_index(drop=True)


def top_hours(df, n=24, metric='plays'):
    """Total plays/minutes per hour-of-day (0-23), sorted by `metric` desc —
    the ranked-hours list under the Patterns heatmap."""
    return _agg_counts(df, 'hour', metric).head(n)


def top_times_of_week(df, n=5, metric='plays'):
    """Total plays/minutes per (day_of_week, hour) cell — specific times of
    week (e.g. 'Friday 4pm'), sorted by `metric` desc. More granular than
    top_hours, which aggregates a given hour across every day of the week."""
    out = (df.groupby(['day_of_week', 'hour'])
             .agg(plays=('ts', 'size'), minutes=('minutes_played', 'sum'))
             .reset_index())
    out['minutes'] = out['minutes'].round(1)
    return out.sort_values(metric, ascending=False).head(n)


def patterns_heatmap(df):
    """day_of_week (0-6) x hour (0-23) play-count grid for the Patterns tab."""
    grid = (df.pivot_table(index='day_of_week', columns='hour',
                           values='ts', aggfunc='size', fill_value=0)
              .reindex(index=range(7), columns=range(24), fill_value=0))
    return grid


# --- Single-artist / group / all-time summaries (Bands + Wrapped tabs) ---

def list_artists(df, metric='plays'):
    """Artist names sorted by `metric` desc — for pickers and multiselects."""
    if df.empty:
        return []
    return _agg_counts(df, 'artist_name', metric)['artist_name'].tolist()


def list_artists_recent(df, days=90, metric='minutes'):
    """Like list_artists(), but restricted to plays within the last `days`
    days of the archive's latest play — surfaces artists with a recent
    listening bump that an all-time ranking would miss entirely (a newer
    favorite you've been bingeing lately, even if their lifetime total
    is nowhere near your all-time top artists). Used alongside
    list_artists() to build the setlist.fm query pool in
    render_concert_settings — an all-time favorite might tour again
    without a recent spike, and a recent obsession might not have enough
    lifetime plays yet, so neither ranking alone covers both cases."""
    if df.empty:
        return []
    cutoff = df['ts'].max() - pd.Timedelta(days=days)
    return list_artists(df[df['ts'] >= cutoff], metric=metric)


def artist_rankings(df, metric='plays'):
    """All artists ranked by `metric`, with a 1-based 'rank' column."""
    out = _agg_counts(df, 'artist_name', metric).reset_index(drop=True)
    out['rank'] = out.index + 1
    return out


def _consecutive_day_streak(dates):
    """Longest run of consecutive calendar days among an iterable of dates."""
    days = sorted(set(dates))
    if not days:
        return 0
    longest = run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    return longest


def _longest_gap(dates):
    """Longest run of consecutive calendar days with *no* listening at all
    — the biggest break from Spotify. Returns (gap_days, start, end): the
    empty stretch strictly between two active days (both dates excluded
    from the count, e.g. listened Jan 1 and Jan 11 -> a 9-day gap), or
    (0, None, None) if there are fewer than two distinct active days."""
    days = sorted(set(dates))
    if len(days) < 2:
        return 0, None, None
    best_gap, best_start, best_end = 0, None, None
    for prev, cur in zip(days, days[1:]):
        gap = (cur - prev).days - 1
        if gap > best_gap:
            best_gap, best_start, best_end = gap, prev, cur
    return best_gap, best_start, best_end


def sidebar_time_stats(df):
    """Cheap headline time-commitment stats for the sidebar (see
    _sidebar_time_stats in app.py) — total hours, average pace per day/
    week/month, longest daily streak, and the longest gap with no
    listening at all. Deliberately lighter than alltime_stats() (no top-N
    groupbys over artists/tracks/albums/genres) since this runs on every
    page load via the sidebar, not just the Wrapped page."""
    if df.empty:
        return {}
    dates = df['ts_local'].dt.date
    total_hours = df['minutes_played'].sum() / 60
    span_days = max((df['ts'].max() - df['ts'].min()).days + 1, 1)
    gap_days, gap_start, gap_end = _longest_gap(dates)
    return {
        'total_hours': total_hours,
        'avg_hours_per_day': total_hours / span_days,
        'avg_hours_per_week': total_hours / span_days * 7,
        'avg_hours_per_month': total_hours / span_days * (365.25 / 12),
        'longest_streak': _consecutive_day_streak(dates),
        'longest_gap_days': gap_days,
        'longest_gap_start': gap_start,
        'longest_gap_end': gap_end,
    }


def artist_facts(df, artist, metric='plays'):
    """Headline facts for one artist over `df` (pass the full archive)."""
    sub = df[df['artist_name'] == artist]
    if sub.empty:
        return None
    ranks = artist_rankings(df, metric)
    rank_row = ranks[ranks['artist_name'] == artist]
    by_year = plays_by_year(sub)
    peak = by_year.loc[by_year['plays'].idxmax()] if not by_year.empty else None
    # Tolerate a plays.parquet built before this column existed — it's only
    # added on the next --enrich/--bootstrap rebuild, not retroactively.
    images = (sub['artist_image_url'].dropna() if 'artist_image_url' in sub.columns
             else pd.Series(dtype=object))
    return {
        'artist': artist,
        'image_url': images.iloc[0] if not images.empty else None,
        'plays': int(len(sub)),
        'hours': round(sub['minutes_played'].sum() / 60, 1),
        'rank': int(rank_row['rank'].iloc[0]) if not rank_row.empty else None,
        'total_artists': int(len(ranks)),
        'first_played': sub['ts'].min(),
        'last_played': sub['ts'].max(),
        'peak_year': int(peak['year']) if peak is not None else None,
        'peak_year_plays': int(peak['plays']) if peak is not None else None,
        'skip_rate': round(sub['skipped'].mean(), 3),
        'full_listen_rate': round(sub['full_listen'].mean(), 3),
    }


def group_breakdown(df, artists, metric='plays'):
    """Per-band table for a group: plays, minutes, first/last play, overall rank."""
    ranks = artist_rankings(df, metric).set_index('artist_name')
    rows = []
    for name in artists:
        sub = df[df['artist_name'] == name]
        if sub.empty:
            rows.append({'artist_name': name, 'plays': 0, 'minutes': 0.0,
                         'first_played': pd.NaT, 'last_played': pd.NaT, 'rank': pd.NA})
            continue
        rows.append({
            'artist_name': name,
            'plays': int(len(sub)),
            'minutes': round(sub['minutes_played'].sum(), 1),
            'first_played': sub['ts'].min(),
            'last_played': sub['ts'].max(),
            'rank': int(ranks.loc[name, 'rank']) if name in ranks.index else pd.NA,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(metric, ascending=False).reset_index(drop=True)


def alltime_stats(df):
    """Whole-archive totals + records for the Wrapped All-Time widget."""
    if df.empty:
        return {}
    dates = df['ts_local'].dt.date
    total_hours = df['minutes_played'].sum() / 60
    span_days = max((df['ts'].max() - df['ts'].min()).days, 1)

    by_day = df.groupby(dates).size()
    # Group by (year, month) ints rather than a formatted "YYYY-MM" string —
    # .dt.strftime() runs a slow per-row Python format call over the whole
    # archive just to build a grouping key; the ints are already vectorized
    # columns, formatted back to a string only for the one winning month.
    by_month = df.groupby([df['ts_local'].dt.year, df['ts_local'].dt.month]).size()
    by_year = df.groupby('year').size()
    by_hour = df.groupby('hour').size()
    by_dow = df.groupby('day_of_week').size()

    def _top1(agg_func):
        t = agg_func(df, n=1)
        return t.iloc[0] if len(t) else None

    # Explode the list-valued genres column once and reuse it for both the
    # unique count and the #1 genre, rather than exploding twice (once here,
    # once inside top_genres) over the full archive.
    exploded_genres = df.explode('genres').dropna(subset=['genres'])
    top_genre_row = _agg_counts(exploded_genres, 'genres', 'plays').head(1)

    art, trk, alb = _top1(top_artists), _top1(top_tracks), _top1(top_albums)
    gen = top_genre_row.iloc[0] if len(top_genre_row) else None

    return {
        'total_plays': int(len(df)),
        'total_hours': round(total_hours),
        'unique_artists': int(df['artist_name'].nunique()),
        'unique_tracks': int(df['track_uri'].nunique()),
        'unique_albums': int(df['album_id'].nunique()),
        'unique_genres': int(exploded_genres['genres'].nunique()),
        'listening_days': int(dates.nunique()),
        'first_play': df['ts'].min(),
        'last_play': df['ts'].max(),
        'span_years': round(span_days / 365.25, 1),
        'avg_hours_per_week': round(total_hours / (span_days / 7), 1),
        'longest_streak': _consecutive_day_streak(dates),
        'busiest_day': (str(by_day.idxmax()), int(by_day.max())),
        'busiest_month': ("%04d-%02d" % by_month.idxmax(), int(by_month.max())),
        'biggest_year': (int(by_year.idxmax()), int(by_year.max())),
        'peak_hour': int(by_hour.idxmax()),
        'top_weekday': int(by_dow.idxmax()),
        'skip_rate': round(df['skipped'].mean(), 3),
        'full_listen_rate': round(df['full_listen'].mean(), 3),
        'top_artist': (art['artist_name'], int(art['plays'])) if art is not None else None,
        'top_track': (f"{trk['track_name']} — {trk['artist_name']}", int(trk['plays'])) if trk is not None else None,
        'top_album': (f"{alb['album_name']} — {alb['artist_name']}", int(alb['plays'])) if alb is not None else None,
        'top_genre': (gen['genres'], int(gen['plays'])) if gen is not None else None,
    }


def _consecutive_day_streak_range(dates):
    """Like _consecutive_day_streak, but also returns the (start, end) dates
    of the longest run — needed for the Wrapped Story streak slide, which
    shows the actual date range alongside the day count."""
    days = sorted(set(dates))
    if not days:
        return 0, None, None
    longest = run = 1
    best_start = best_end = run_start = days[0]
    for prev, cur in zip(days, days[1:]):
        if (cur - prev).days == 1:
            run += 1
        else:
            run, run_start = 1, cur
        if run > longest:
            longest, best_start, best_end = run, run_start, cur
    return longest, best_start, best_end


_MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def monthly_breakdown(df, year):
    """Per-calendar-month days-active/hours/plays for one year, always 12
    rows (Jan-Dec, zero-filled) so the Wrapped Story's monthly bar/line
    slides don't have to special-case missing months."""
    sub = df[df['year'] == year]
    month = sub['ts_local'].dt.month
    by_days = sub.assign(_d=sub['ts_local'].dt.date).groupby(month)['_d'].nunique()
    by_hours = sub.groupby(month)['minutes_played'].sum() / 60
    by_plays = sub.groupby(month).size()
    return pd.DataFrame({
        'month': range(1, 13),
        'label': _MONTH_LABELS,
        'days_active': [int(by_days.get(m, 0)) for m in range(1, 13)],
        'hours': [round(float(by_hours.get(m, 0.0)), 1) for m in range(1, 13)],
        'plays': [int(by_plays.get(m, 0)) for m in range(1, 13)],
    })


def wrapped_story_data(df, year, top_n=5):
    """Assembles everything the Wrapped Story carousel needs for one year
    into a single JSON-safe dict: totals, the monthly days/hours series,
    the longest listening streak (with its date range), and a top-artists
    leaderboard. Returns None if there's no data for that year."""
    sub = df[df['year'] == year]
    if sub.empty:
        return None
    dates = sub['ts_local'].dt.date
    streak_days, streak_start, streak_end = _consecutive_day_streak_range(dates)
    top = top_artists(sub, n=top_n)
    return {
        'year': int(year),
        'total_plays': int(len(sub)),
        'total_hours': round(sub['minutes_played'].sum() / 60),
        'listening_days': int(dates.nunique()),
        'unique_artists': int(sub['artist_name'].nunique()),
        'longest_streak': int(streak_days),
        'streak_start': str(streak_start) if streak_start else None,
        'streak_end': str(streak_end) if streak_end else None,
        'monthly': monthly_breakdown(sub, year).to_dict('records'),
        'top_artists': [
            {'rank': int(r['rank']), 'artist_name': r['artist_name'],
             'plays': int(r['plays']), 'hours': round(r['minutes'] / 60, 1)}
            for r in top.assign(rank=range(1, len(top) + 1)).to_dict('records')
        ],
    }
