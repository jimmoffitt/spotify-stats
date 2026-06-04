"""
app.py — Streamlit dashboard for Spotify listening history.

Loads data/processed/plays.parquet (cached) and renders the tabs from
DESIGN.md. Aggregations come from src/process_data.py; figures from
src/charts.py. Run the pipeline first to build the parquet:

    python run_pipeline.py
    streamlit run app.py
"""
import os
import re

import pandas as pd
import streamlit as st

from src import charts, config, process_data as proc

st.set_page_config(page_title="spotify-stats", page_icon="🎵", layout="wide")


@st.cache_data
def load_plays_cached(path, mtime):
    """Cached parquet load. `mtime` busts the cache when the file changes."""
    return proc.load_plays(path)


def metric_columns(metric):
    """Map the sidebar metric toggle to (column, human label)."""
    return ('minutes', 'Minutes') if metric == 'Minutes' else ('plays', 'Plays')


# Editor columns. Bounds accept a year ("2019") or a year-month ("2019-06").
_COL_ARTIST, _COL_ALL = "Artist", "All years"
_COL_BEFORE, _COL_AFTER = "Before (YYYY or YYYY-MM)", "After (YYYY or YYYY-MM)"
_COL_ONLY = "Only (years/months, csv)"


def _norm_token(s):
    """Normalize a bound token: '2019' -> int 2019, '2019-06' -> '2019-06', else None."""
    s = str(s).strip()
    if re.fullmatch(r"\d{4}", s):
        return int(s)
    if re.fullmatch(r"\d{4}-\d{1,2}", s):
        return s
    return None


def _exclusions_to_df(exclusions):
    """Flatten the artist-centric exclusions schema into an editable table."""
    rows = []
    for artist, rule in exclusions.get('exclude', {}).items():
        if rule is True or rule == 'all':
            rows.append({_COL_ARTIST: artist, _COL_ALL: True,
                         _COL_BEFORE: "", _COL_AFTER: "", _COL_ONLY: ""})
        elif isinstance(rule, dict):
            only = ",".join(str(y) for y in (rule.get('years') or []))
            rows.append({_COL_ARTIST: artist, _COL_ALL: False,
                         _COL_BEFORE: "" if rule.get('before') is None else str(rule['before']),
                         _COL_AFTER: "" if rule.get('after') is None else str(rule['after']),
                         _COL_ONLY: only})
    return pd.DataFrame(rows, columns=[_COL_ARTIST, _COL_ALL, _COL_BEFORE,
                                       _COL_AFTER, _COL_ONLY])


def _df_to_exclusions(edited):
    """Rebuild the exclusions schema from the edited table."""
    rules = {}
    for _, r in edited.iterrows():
        name = str(r.get(_COL_ARTIST, "")).strip()
        if not name or name.lower() == "nan":
            continue
        if bool(r.get(_COL_ALL)):
            rules[name] = True
            continue
        rule = {}
        before = _norm_token(r.get(_COL_BEFORE, ""))
        if before is not None:
            rule['before'] = before
        after = _norm_token(r.get(_COL_AFTER, ""))
        if after is not None:
            rule['after'] = after
        only = [t for t in (_norm_token(x) for x in
                            str(r.get(_COL_ONLY, "")).split(",")) if t is not None]
        if only:
            rule['years'] = only
        rules[name] = rule or True  # an artist with no constraints -> all years
    return {"exclude": rules}


def main():
    st.title("🎵 spotify-stats")

    if not os.path.exists(config.PLAYS_FILE):
        st.info("No processed data yet. Drop your GDPR export into `data/raw/` "
                "and run `python run_pipeline.py`.")
        return

    df_all = load_plays_cached(config.PLAYS_FILE, os.path.getmtime(config.PLAYS_FILE))
    exclusions = proc.load_exclusions()

    # --- Sidebar: theme, metric, exclusions, year filter ---
    with st.sidebar:
        st.header("Filters")
        dark = st.toggle("Dark charts", value=True)
        charts.set_theme(dark)
        metric = st.radio("Rank by", ["Plays", "Minutes"], horizontal=True)
        apply_excl = st.toggle(
            "Remove kid streams?", value=True,
            help="Filter out the artists/years configured under Settings → "
                 "Artist exclusions (e.g. shared-account years).")
        years = sorted(df_all['year'].dropna().unique().tolist())
        year_sel = st.selectbox("Year", ["All years"] + [str(y) for y in years])

    df = proc.apply_exclusions(df_all, exclusions) if apply_excl else df_all
    n_excluded = len(df_all) - len(df)

    value_col, value_label = metric_columns(metric)
    view = df if year_sel == "All years" else df[df['year'] == int(year_sel)]

    caption = (f"{len(view):,} plays · {view['minutes_played'].sum()/60:,.0f} hours "
               f"· {view['artist_name'].nunique():,} artists")
    caption += (f"  ·  🧒 filtered ({n_excluded:,} kid streams removed)"
                if apply_excl and n_excluded else "  ·  unfiltered (all streams)")
    st.caption(caption)

    tabs = st.tabs(["🎸 Artists", "🏆 Rankings", "🎵 Tracks", "💿 Albums",
                    "🎼 Genres", "📅 Decades", "🗓️ Wrapped", "🕐 Patterns",
                    "🔍 Explore", "📤 Export", "⚙️ Settings"])

    with tabs[0]:
        render_artists(view, value_col, value_label)
    with tabs[1]:
        render_rankings(df)  # cross-year rank chart — uses the full dataset
    with tabs[2]:
        render_tracks(view, value_col, value_label)
    with tabs[3]:
        render_albums(view, value_col, value_label)
    with tabs[4]:
        render_genres(view, value_col, value_label)
    with tabs[5]:
        render_decades(view, value_col, value_label)
    with tabs[6]:
        render_wrapped(df)  # Wrapped picks its own window
    with tabs[7]:
        render_patterns(view)
    with tabs[8]:
        render_explore(view)
    with tabs[9]:
        render_export(view)
    with tabs[10]:
        render_settings(df)


# --- Tab renderers ---

def render_artists(df, value_col, value_label):
    st.subheader("Top artists")
    top = proc.top_artists(df, n=25)
    st.plotly_chart(charts.ranked_bar(top, 'artist_name', value_col,
                                      f"Top artists by {value_label.lower()}"),
                    use_container_width=True)
    st.caption("Country choropleth requires Phase 2 MusicBrainz enrichment.")


def render_rankings(df):
    """Years-across-the-top rank chart (the Markdown table, made interactive)."""
    st.subheader("Top artists per year")

    c1, c2, c3 = st.columns([1.2, 1.2, 1])
    metric = c1.radio("Rank by", ["Minutes", "Plays"], horizontal=True, key="rank_metric")
    n = c2.slider("Artists per year", 5, 25, 10, key="rank_n")
    show_values = c3.checkbox("Show values in cells", key="rank_vals")
    metric_key = 'minutes' if metric == "Minutes" else 'plays'

    wide = proc.top_artists_wide(df, n=n, metric=metric_key, show_values=show_values)

    # Year navigation: limit which year columns are shown (the table is wide).
    years = [int(c) for c in wide.columns]
    if len(years) > 1:
        lo, hi = st.select_slider(
            "Year range", options=years, value=(min(years), max(years)))
        wide = wide[[c for c in wide.columns if lo <= int(c) <= hi]]

    st.dataframe(wide, width='stretch', height=38 * n + 60)
    st.download_button(
        "Download Markdown table",
        proc.wide_to_markdown(wide, f"Top {n} artists per year — by {metric_key}"),
        "top_artists_by_year.md", "text/markdown")


def render_tracks(df, value_col, value_label):
    st.subheader("Top tracks")
    top = proc.top_tracks(df, n=25)
    label = top['track_name'] + " — " + top['artist_name']
    chart_df = top.assign(label=label)
    st.plotly_chart(charts.ranked_bar(chart_df, 'label', value_col,
                                      f"Top tracks by {value_label.lower()}"),
                    use_container_width=True)
    st.dataframe(top, width='stretch', hide_index=True)


def render_albums(df, value_col, value_label):
    st.subheader("Top albums")
    top = proc.top_albums(df, n=25)
    label = top['artist_name'].fillna('Unknown') + " — " + top['album_name'].fillna('Unknown')
    chart_df = top.assign(label=label)
    st.plotly_chart(charts.ranked_bar(chart_df, 'label', value_col,
                                      f"Top albums by {value_label.lower()}"),
                    use_container_width=True)


def render_genres(df, value_col, value_label):
    st.subheader("Top genres")
    top = proc.top_genres(df, n=25)
    if top.empty:
        st.info("No genre data yet — run artist enrichment.")
        return
    st.plotly_chart(charts.ranked_bar(top, 'genres', value_col,
                                      f"Top genres by {value_label.lower()}"),
                    use_container_width=True)


def render_decades(df, value_col, value_label):
    st.subheader("Listening by release decade")
    dec = proc.decade_breakdown(df)
    if dec.empty:
        st.info("No release-date data yet — run track enrichment.")
        return
    st.plotly_chart(charts.decade_bar(dec, value_col,
                                      f"{value_label} by decade"),
                    use_container_width=True)


def render_wrapped(df):
    st.subheader("Wrapped")
    window = st.selectbox("Window", ["Last 30 days", "All-time"] +
                          [str(y) for y in sorted(df['year'].dropna().unique(), reverse=True)])
    if window == "Last 30 days":
        cutoff = df['ts'].max() - pd.Timedelta(days=30)
        w = df[df['ts'] >= cutoff]
    elif window == "All-time":
        w = df
    else:
        w = df[df['year'] == int(window)]

    if w.empty:
        st.info("No plays in this window.")
        return

    top_artist = proc.top_artists(w, 1)
    top_track = proc.top_tracks(w, 1)
    top_genre = proc.top_genres(w, 1)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total plays", f"{len(w):,}")
    c2.metric("Hours listened", f"{w['minutes_played'].sum()/60:,.0f}")
    c3.metric("Listening days", f"{w['ts_local'].dt.date.nunique():,}")
    c1.metric("Top artist", top_artist.iloc[0]['artist_name'] if len(top_artist) else "—")
    c2.metric("Top track", top_track.iloc[0]['track_name'] if len(top_track) else "—")
    c3.metric("Top genre", top_genre.iloc[0]['genres'] if len(top_genre) else "—")


def render_patterns(df):
    st.subheader("When do I listen?")
    grid = proc.patterns_heatmap(df)
    st.plotly_chart(charts.hour_dow_heatmap(grid, "Plays by hour and day of week"),
                    use_container_width=True)


def render_explore(df):
    st.subheader("Explore")
    query = st.text_input("Search track / artist / album")
    results = df
    if query:
        q = query.lower()
        mask = (
            results['track_name'].str.lower().str.contains(q, na=False)
            | results['artist_name'].str.lower().str.contains(q, na=False)
            | results['album_name'].str.lower().str.contains(q, na=False)
        )
        results = results[mask]
    cols = ['ts_local', 'track_name', 'artist_name', 'album_name',
            'minutes_played', 'full_listen']
    st.caption(f"{len(results):,} plays")
    st.dataframe(results[cols].sort_values('ts_local', ascending=False).head(1000),
                 width='stretch', hide_index=True)
    st.download_button("Download CSV", results[cols].to_csv(index=False),
                       "plays_filtered.csv", "text/csv")


def render_export(df):
    st.subheader("Export")
    st.download_button("Full play log (CSV)", df.to_csv(index=False),
                       "plays_full.csv", "text/csv")
    annual = proc.plays_by_year(df)
    st.download_button("Annual summary (CSV)", annual.to_csv(index=False),
                       "annual_summary.csv", "text/csv")
    st.dataframe(annual, width='stretch', hide_index=True)


def render_settings(df):
    st.subheader("Settings")
    settings = proc.load_settings()
    st.write("**Data status**")
    st.write(f"- Plays loaded: {len(df):,}")
    st.write(f"- Date range: {df['ts'].min().date()} → {df['ts'].max().date()}")
    if os.path.exists(config.LAST_SYNC_FILE):
        st.write(f"- Last sync file: `{config.LAST_SYNC_FILE}`")
    st.write("**Preferences**")
    st.write(f"- Timezone: {settings.get('timezone') or 'system default'}")
    st.write(f"- Full-listen threshold: {settings.get('full_listen_threshold')}")

    st.divider()
    st.write("**Artist exclusions**")
    st.caption("Drop an artist's plays for shared-account periods. Per row: tick "
               "**All years**, or set a **Before** / **After** bound, or a "
               "comma-separated **Only** list. Bounds take a year (`2019`) or a "
               "month (`2019-06`). Example — Taylor Swift, *Before* = `2019` drops "
               "2011–2018 and keeps 2019 onward. Add rows with the ＋ at the bottom.")
    editor_df = _exclusions_to_df(proc.load_exclusions())
    edited = st.data_editor(
        editor_df, num_rows="dynamic", width='stretch', key="excl_editor",
        column_config={
            _COL_ARTIST: st.column_config.TextColumn(_COL_ARTIST, required=True),
            _COL_ALL: st.column_config.CheckboxColumn(_COL_ALL, default=False),
            _COL_BEFORE: st.column_config.TextColumn(_COL_BEFORE),
            _COL_AFTER: st.column_config.TextColumn(_COL_AFTER),
            _COL_ONLY: st.column_config.TextColumn(_COL_ONLY),
        })
    if st.button("Save exclusions"):
        proc.save_exclusions(_df_to_exclusions(edited))
        st.success("Saved. Toggle the sidebar filter or switch tabs to apply.")
    with st.expander("Raw JSON"):
        st.json(proc.load_exclusions())

    st.caption("Sync Now (live API) is TODO — wires to fetch_data.fetch_recently_played.")


if __name__ == "__main__":
    main()
