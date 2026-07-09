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


def top_tracks(df, n=20, metric='plays'):
    """Top tracks by `metric`, grouped by track_uri rather than track name.
    Spotify sometimes edits a track's display title after release (e.g. a
    remaster relabeled '2010 Remastered' -> 'Remastered 2010', or a curly vs.
    straight quote in the name) — grouping by name would silently split one
    song's plays across near-duplicate rows."""
    out = (df.groupby(['track_uri', 'artist_name'])
             .agg(track_name=('track_name', lambda s: s.mode().iat[0]),
                  plays=('ts', 'size'),
                  minutes=('minutes_played', 'sum'))
             .reset_index())
    out['minutes'] = out['minutes'].round(1)
    return out.sort_values(metric, ascending=False).head(n)[
        ['track_name', 'artist_name', 'plays', 'minutes']]


def top_albums(df, n=20, metric='plays'):
    """Top albums by `metric`, grouped by album_id rather than album name — the
    same rationale as top_tracks: reissues/deluxe editions relabel the album
    name (e.g. 'Stoney' vs 'Stoney - Deluxe') without changing the tracks."""
    out = (df.dropna(subset=['album_id'])
             .groupby(['album_id', 'artist_name'])
             .agg(album_name=('album_name', lambda s: s.mode().iat[0]),
                  plays=('ts', 'size'),
                  minutes=('minutes_played', 'sum'))
             .reset_index())
    out['minutes'] = out['minutes'].round(1)
    return out.sort_values(metric, ascending=False).head(n)[
        ['album_name', 'artist_name', 'plays', 'minutes']]


def top_genres(df, n=20, metric='plays'):
    """Explode the list-valued genres column before aggregating."""
    exploded = df.explode('genres').dropna(subset=['genres'])
    return _agg_counts(exploded, 'genres', metric).head(n)


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


def top_hours(df, n=24, metric='plays'):
    """Total plays/minutes per hour-of-day (0-23), sorted by `metric` desc —
    the ranked-hours list under the Patterns heatmap."""
    return _agg_counts(df, 'hour', metric).head(n)


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
    by_month = df.groupby(df['ts_local'].dt.strftime('%Y-%m')).size()
    by_year = df.groupby('year').size()
    by_hour = df.groupby('hour').size()
    by_dow = df.groupby('day_of_week').size()

    def _top1(agg_func):
        t = agg_func(df, n=1)
        return t.iloc[0] if len(t) else None

    art, trk, alb, gen = (_top1(top_artists), _top1(top_tracks),
                          _top1(top_albums), _top1(top_genres))

    return {
        'total_plays': int(len(df)),
        'total_hours': round(total_hours),
        'unique_artists': int(df['artist_name'].nunique()),
        'unique_tracks': int(df['track_uri'].nunique()),
        'unique_albums': int(df['album_id'].nunique()),
        'unique_genres': int(df.explode('genres')['genres'].dropna().nunique()),
        'listening_days': int(dates.nunique()),
        'first_play': df['ts'].min(),
        'last_play': df['ts'].max(),
        'span_years': round(span_days / 365.25, 1),
        'avg_hours_per_week': round(total_hours / (span_days / 7), 1),
        'longest_streak': _consecutive_day_streak(dates),
        'busiest_day': (str(by_day.idxmax()), int(by_day.max())),
        'busiest_month': (str(by_month.idxmax()), int(by_month.max())),
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
