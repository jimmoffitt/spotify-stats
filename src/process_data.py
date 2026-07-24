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
    """Load data/settings.json, filling any missing keys from DEFAULT_SETTINGS."""
    settings = dict(config.DEFAULT_SETTINGS)
    if os.path.exists(config.SETTINGS_FILE):
        with open(config.SETTINGS_FILE, 'r', encoding='utf-8') as f:
            settings.update(json.load(f))
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

    for artist, rule in rules.items():
        is_artist = artist_lower == str(artist).lower()
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

    # 4. Artist enrichment: genres (union across the track's artists).
    df['genres'] = df['track_uri'].map(
        lambda u: _genres_for_track(track_cache.get(u), artist_cache))

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
    return out.sort_values(metric, ascending=False).head(n)[
        [label_col, 'artist_name', 'plays', 'minutes']]


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


def top_genres(df, n=20, metric='plays'):
    """Explode the list-valued genres column before aggregating."""
    exploded = df.explode('genres').dropna(subset=['genres'])
    return _agg_counts(exploded, 'genres', metric).head(n)


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


def genre_group_treemap_data(df, metric='plays', top_micro_per_macro=6):
    """Two-level treemap data: macro genre family -> its top micro-genres.
    Returns a tidy frame with one row per node (macro rows have
    parent_id=''), ready for charts.genre_treemap(). Each macro's own
    value is its *full* total (all micro-genres, not just the ones shown
    as children), so the macro block sizes reflect true totals even though
    only the top few micro-genres are broken out inside it."""
    exploded = df.explode('genres').dropna(subset=['genres']).copy()
    exploded['macro_genre'] = exploded['genres'].map(_macro_genre)

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


def artist_concert_warmups(df, spike_days=14, cooldown_days=14):
    """Bands with a "charge up, then crash" listening shape: a concentrated
    burst over a `spike_days`-day window, followed by a sharp drop in the
    `cooldown_days` days right after — the pattern of hyping up for a show,
    then coming back down from it (as opposed to track/artist "binges",
    which just rank the single most concentrated window regardless of what
    follows it).

    For each artist, finds the spike_days-day sliding window (same
    cumsum/searchsorted approach as _sliding_window_peaks) with the most
    listening, then compares its daily rate to the daily rate over the
    following cooldown_days. Artists whose most recent play is within
    cooldown_days of their spike (no runway to measure a "return to normal")
    are dropped — can't tell a crash from "still going".

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
        if spike_hours <= 0 or latest_overall - spike_end < cooldown:
            continue
        cool_mask = (ts > spike_end) & (ts <= spike_end + cooldown)
        cooldown_hours = minutes[cool_mask].sum() / 60.0
        spike_daily = spike_hours / spike_days
        cooldown_daily = cooldown_hours / cooldown_days
        drop_pct = max(0.0, 1 - cooldown_daily / spike_daily)
        rows.append((artist, spike_hours, spike_start, spike_end,
                     cooldown_hours, drop_pct, spike_hours * drop_pct))
    out = pd.DataFrame(rows, columns=['artist_name', 'spike_hours', 'spike_start',
                                       'spike_end', 'cooldown_hours', 'drop_pct',
                                       'warmup_score'])
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


def artist_facts(df, artist, metric='plays'):
    """Headline facts for one artist over `df` (pass the full archive)."""
    sub = df[df['artist_name'] == artist]
    if sub.empty:
        return None
    ranks = artist_rankings(df, metric)
    rank_row = ranks[ranks['artist_name'] == artist]
    by_year = plays_by_year(sub)
    peak = by_year.loc[by_year['plays'].idxmax()] if not by_year.empty else None
    return {
        'artist': artist,
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
