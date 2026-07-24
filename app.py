"""
app.py — Streamlit dashboard for Spotify listening history.

Loads data/processed/plays.parquet (cached) and renders the tabs from
DESIGN.md. Aggregations come from src/process_data.py; figures from
src/charts.py. Run the pipeline first to build the parquet:

    python run_pipeline.py
    streamlit run app.py
"""
# Silence urllib3's NotOpenSSLWarning: the system Python 3.9 links against
# LibreSSL 2.8.3, which urllib3 v2 doesn't certify. Harmless for our use.
# Match by message — importing the warning class would trigger it first.
import warnings

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")

import fnmatch
import glob
import os
import re
import zipfile

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import run_pipeline
from src import charts, config, process_data as proc

st.set_page_config(page_title="sonic-stats", page_icon="🎵", layout="wide",
                   initial_sidebar_state="expanded")


@st.cache_data
def load_plays_cached(path, mtime):
    """Cached parquet load. `mtime` busts the cache when the file changes."""
    return proc.load_plays(path)


@st.cache_data
def filtered_plays_cached(path, mtime, excl_mtime):
    """Exclusion-filtered frame, cached so toggling tabs/filters is instant.
    `mtime`/`excl_mtime` bust the cache when the parquet or exclusions change."""
    df_all = load_plays_cached(path, mtime)
    return proc.apply_exclusions(df_all, proc.load_exclusions())


@st.cache_data
def alltime_stats_cached(path, mtime, excl_mtime, apply_excl):
    """proc.alltime_stats() is the priciest call on the Wrapped page (several
    full-archive groupbys). Cache it the same way as filtered_plays_cached —
    keyed on cheap primitives rather than the DataFrame itself — so it's not
    recomputed on every widget interaction while Wrapped is open, only when
    the underlying data or exclusion filter actually changes."""
    df = (filtered_plays_cached(path, mtime, excl_mtime) if apply_excl
          else load_plays_cached(path, mtime))
    return proc.alltime_stats(df)


def metric_columns(metric):
    """Map the sidebar metric toggle to (column, human label)."""
    return ('minutes', 'Minutes') if metric == 'Minutes' else ('plays', 'Plays')


# "All time" first so it stays the default (index 0) selectbox choice.
_RANGE_PRESETS = ["All time", "Last 7 days", "Last 30 days", "This month"]


def _apply_range(df, sel):
    """Apply a date-range selection: one of _RANGE_PRESETS, or a year string.
    Presets are relative to the most recent play in `df` (not wall-clock
    'now'), so a stale sync doesn't make 'Last 7 days' look emptier than it
    actually is."""
    if df.empty or sel == "All time":
        return df
    latest = df['ts_local'].max()
    if sel == "This month":
        return df[(df['ts_local'].dt.year == latest.year) &
                  (df['ts_local'].dt.month == latest.month)]
    if sel == "Last 7 days":
        return df[df['ts_local'] >= latest - pd.Timedelta(days=7)]
    if sel == "Last 30 days":
        return df[df['ts_local'] >= latest - pd.Timedelta(days=30)]
    return df[df['year'] == int(sel)]


# Editor columns. Bounds accept a year ("2019") or a year-month ("2019-06").
_COL_ARTIST, _COL_ALL = "Artist", "All years"
_COL_BEFORE, _COL_AFTER = "Before (YYYY or YYYY-MM)", "After (YYYY or YYYY-MM)"
_COL_ONLY = "Only (years/months, csv)"
_COL_KEEP = "Keep % (blank=0)"


def _norm_token(s):
    """Normalize a bound token: '2019' -> int 2019, '2019-06' -> '2019-06', else None."""
    s = str(s).strip()
    if re.fullmatch(r"\d{4}", s):
        return int(s)
    if re.fullmatch(r"\d{4}-\d{1,2}", s):
        return s
    return None


def _norm_keep(v):
    """Parse a 'keep' percent (0..100) to a fraction in (0,1); blank/0/100 -> None."""
    if v is None or (isinstance(v, float) and v != v):  # None or NaN
        return None
    try:
        pct = float(str(v).strip().rstrip('%'))
    except ValueError:
        return None
    if pct <= 0 or pct >= 100:
        return None
    return round(pct / 100, 4)


def _exclusions_to_df(exclusions):
    """Flatten the artist-centric exclusions schema into an editable table."""
    rows = []
    for artist, rule in exclusions.get('exclude', {}).items():
        if rule is True or rule == 'all':
            rows.append({_COL_ARTIST: artist, _COL_ALL: True, _COL_BEFORE: "",
                         _COL_AFTER: "", _COL_ONLY: "", _COL_KEEP: None})
        elif isinstance(rule, dict):
            only = ",".join(str(y) for y in (rule.get('years') or []))
            keep = rule.get('keep')
            rows.append({_COL_ARTIST: artist, _COL_ALL: False,
                         _COL_BEFORE: "" if rule.get('before') is None else str(rule['before']),
                         _COL_AFTER: "" if rule.get('after') is None else str(rule['after']),
                         _COL_ONLY: only,
                         _COL_KEEP: int(round(keep * 100)) if keep else None})
    return pd.DataFrame(rows, columns=[_COL_ARTIST, _COL_ALL, _COL_BEFORE,
                                       _COL_AFTER, _COL_ONLY, _COL_KEEP])


def _df_to_exclusions(edited):
    """Rebuild the exclusions schema from the edited table."""
    rules = {}
    for _, r in edited.iterrows():
        name = str(r.get(_COL_ARTIST, "")).strip()
        if not name or name.lower() == "nan":
            continue
        keep = _norm_keep(r.get(_COL_KEEP))
        if bool(r.get(_COL_ALL)):
            rules[name] = {"keep": keep} if keep is not None else True
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
        if keep is not None:
            rule['keep'] = keep
        rules[name] = rule or True  # an artist with no constraints -> all years
    return {"exclude": rules}


def _sidebar_filters(df_all, show):
    """Render the Analytics-only filters (Date range, Rank by). Assumes the
    caller is already inside the sidebar context, so the block can sit
    between the two nav groups. When `show` is False (Tools & settings pages)
    it renders nothing and returns defaults. Returns (metric, range_sel).
    """
    if not show:
        return "Plays", "All time"
    st.divider()
    st.markdown("**Filters**")
    years = sorted(df_all['year'].dropna().unique().tolist(), reverse=True)
    range_sel = st.selectbox("Date range", _RANGE_PRESETS + [str(y) for y in years])
    metric = st.radio("Rank by", ["Plays", "Minutes"], horizontal=True)
    return metric, range_sel


def _sidebar_options():
    """Global display/data options — always visible (not just on Analytics
    pages), since kid-stream exclusion and dark charts both affect every page,
    not only the Analytics tabs. Returns (apply_excl, dark)."""
    apply_excl = st.toggle(
        "Remove kid streams?", value=True,
        help="Filter out the artists/years configured under Artist filters "
             "(e.g. shared-account years).")
    dark = st.toggle("Dark charts", value=False)
    return apply_excl, dark


def _sidebar_data(df_all):
    """Data-freshness section in the sidebar: the most recent play (when,
    what), how current the sync is, plus a one-click Sync. Shown on every
    page so updating is always at hand."""
    st.markdown("**Data**")
    latest = df_all.loc[df_all['ts'].idxmax()]
    st.caption(f"Latest play: {latest['ts'].strftime('%Y-%m-%d %H:%M UTC')}")
    st.caption(f"🎵 {latest['track_name']} — {latest['artist_name']}")

    if config.DEMO_MODE:
        # Read-only demo build: bundled sanitized dataset, no Spotify
        # credentials on the host, so live sync is unavailable by design.
        st.caption("Demo mode — read-only sample dataset; live sync is disabled.")
        return

    # A sync just finished on the previous run — surface it now (post-rerun).
    done = st.session_state.pop('_sync_msg', None)
    if done:
        st.toast(done, icon="✅")

    at = run_pipeline._read_last_sync().get('last_sync_at')
    if at:
        hrs = (pd.Timestamp.now(tz='UTC') - pd.Timestamp(at)).total_seconds() / 3600
        when = ("just now" if hrs < 1 else
                f"{hrs:.0f}h ago" if hrs < 48 else f"{hrs / 24:.0f}d ago")
        st.caption(f"Synced {when}")
    else:
        st.caption("Never synced")

    authorized = os.path.exists(config.TOKEN_FILE)
    if st.button("🔄 Sync now", disabled=not authorized, width='stretch',
                 help="Fetch your latest plays from Spotify (recently-played)."):
        with st.spinner("Syncing…"):
            try:
                res = run_pipeline.sync()
                st.cache_data.clear()
                st.session_state['_sync_msg'] = f"Synced {res['added']} new play(s)."
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")
    if not authorized:
        st.caption("Authorize once in a terminal: `python -m src.setup_tokens`")


def _extract_gdpr_zip(uploaded_file):
    """Pull Streaming_History_Audio_*.json files out of an uploaded Spotify
    export zip and write them into data/raw/, flattening any folder structure
    Spotify wraps them in. Returns the filenames written; raises ValueError
    if the zip contains none (e.g. the smaller "Account data" export, which
    doesn't include play-by-play history)."""
    written = []
    with zipfile.ZipFile(uploaded_file) as zf:
        for info in zf.infolist():
            name = os.path.basename(info.filename)
            if not name or not fnmatch.fnmatch(name, config.GDPR_GLOB):
                continue
            with zf.open(info) as src, open(os.path.join(config.RAW_DIR, name), 'wb') as dst:
                dst.write(src.read())
            written.append(name)
    if not written:
        raise ValueError(
            f"No '{config.GDPR_GLOB}' files found in that zip. Make sure you "
            "requested **Extended streaming history** — the smaller \"Account "
            "data\" export doesn't include play-by-play history.")
    return written


def render_onboarding():
    """First-run screen, shown until data/processed/plays.parquet exists.
    Walks the user through the one part of setup that can't be automated —
    Spotify's own export request — then takes their archive straight from
    the browser (no data/raw/ file-copying, no terminal) and builds the
    dashboard in place."""
    st.title("🎵 sonic-stats")
    st.markdown("### Let's get your listening history")

    with st.popover("❓ How do I get my Spotify data?"):
        st.markdown(
            "1. Go to Spotify **[Account → Privacy settings]"
            "(https://www.spotify.com/account/privacy/)**.\n"
            "2. Under **Download your data**, check **Extended streaming "
            "history** — the default \"Account data\" option is a smaller "
            "summary and isn't enough.\n"
            "3. Click **Request data** and confirm via the email Spotify "
            "sends.\n"
            "4. **Wait.** It can take a few hours to ~30 days (usually a "
            "few days). Spotify emails a download link when it's ready.\n"
            "5. Download the **.zip** and upload it below — no need to "
            "unzip it first."
        )

    uploaded = st.file_uploader(
        "Upload your Spotify export (.zip)", type="zip",
        help="The zip file Spotify emailed you a link to — drop it in as-is.")

    raw_ready = bool(glob.glob(os.path.join(config.RAW_DIR, config.GDPR_GLOB)))
    if uploaded is not None:
        try:
            written = _extract_gdpr_zip(uploaded)
        except (zipfile.BadZipFile, ValueError) as e:
            st.error(str(e))
            return
        st.success(f"Found {len(written)} file(s) in your export.")
        raw_ready = True

    if not raw_ready:
        st.caption("Waiting on your export upload before this dashboard can build.")
        return

    if st.button("Build my dashboard", type="primary", width='stretch'):
        try:
            config.validate_config()
        except ValueError:
            st.error(
                "Missing Spotify API credentials (`SPOTIFY_CLIENT_ID` / "
                "`SPOTIFY_CLIENT_SECRET`), needed to enrich tracks with "
                "genres and release dates. Create a free Spotify app and "
                "save them to `.local.env` — see the README's *Getting "
                "started* step 3, then reload this page.")
            return
        with st.spinner("Building your dashboard — hundreds of enrichment "
                         "API calls, usually a few minutes…"):
            try:
                run_pipeline.bootstrap()
            except Exception as e:
                st.error(f"Bootstrap failed: {e}")
                return
        st.rerun()


def main():
    if not os.path.exists(config.PLAYS_FILE):
        render_onboarding()
        return

    df_all = load_plays_cached(config.PLAYS_FILE, os.path.getmtime(config.PLAYS_FILE))

    # Shared context the page callables read at render time. Populated below,
    # after the sidebar filters resolve — but before pg.run() invokes a page.
    ctx = {}

    # Page wrappers: st.Page needs zero-arg callables, so each reads from ctx.
    def _artists():  render_artists(ctx['view'], ctx['value_col'], ctx['value_label'])
    def _rankings(): render_rankings(ctx['df'])
    def _tracks():   render_tracks(ctx['view'], ctx['value_col'], ctx['value_label'])
    def _albums():   render_albums(ctx['view'], ctx['value_col'], ctx['value_label'])
    def _genres():   render_genres(ctx['view'], ctx['value_col'], ctx['value_label'])
    def _decades():  render_decades(ctx['view'], ctx['value_col'], ctx['value_label'])
    def _wrapped():
        # Computed here, not eagerly in main(), so visiting any other page
        # doesn't pay for alltime_stats_cached() — only Wrapped needs it.
        alltime = alltime_stats_cached(config.PLAYS_FILE,
                                       os.path.getmtime(config.PLAYS_FILE),
                                       ctx['excl_mtime'], ctx['apply_excl'])
        render_wrapped(ctx['df'], alltime)
    def _patterns(): render_patterns(ctx['view'])
    def _bands():    render_bands(ctx['df'])
    def _artist_filters(): render_artist_filters(df_all)
    def _explore():  render_explore(ctx['view'])
    def _export():   render_export(ctx['view'])
    def _settings(): render_settings(ctx['df'])

    analytics = [
        st.Page(_wrapped,  title="Wrapped",  icon="🗓️", url_path="wrapped", default=True),
        st.Page(_artists,  title="Artists",  icon="🎸", url_path="artists"),
        st.Page(_rankings, title="Rankings", icon="🏆", url_path="rankings"),
        st.Page(_tracks,   title="Tracks",   icon="🎵", url_path="tracks"),
        st.Page(_albums,   title="Albums",   icon="💿", url_path="albums"),
        st.Page(_bands,    title="Bands",    icon="🎤", url_path="bands"),
        st.Page(_genres,   title="Genres",   icon="🎼", url_path="genres"),
        st.Page(_patterns, title="Patterns", icon="🕐", url_path="patterns"),
        st.Page(_decades,  title="Decades",  icon="📅", url_path="decades"),
    ]
    tools = [
        st.Page(_artist_filters, title="Artist filters", icon="🚫",
                url_path="artist-filters"),
        st.Page(_explore,  title="Explore",  icon="🔍", url_path="explore"),
        st.Page(_export,   title="Export",   icon="📤", url_path="export"),
        st.Page(_settings, title="Settings", icon="⚙️", url_path="settings"),
    ]

    # Route via st.navigation (so the selected page survives reruns — no more
    # snap-back), but hide its built-in nav so we can build the sidebar by hand
    # and slot the Data section and options between the two nav groups.
    pg = st.navigation({"Analytics": analytics, "Tools & settings": tools},
                       position="hidden")
    is_analytics = pg.url_path in {p.url_path for p in analytics}

    with st.sidebar:
        _sidebar_dark = st.context.theme.type == 'dark'
        _sidebar_bg = '#262730' if _sidebar_dark else '#f0f2f6'
        # Streamlit's native sidebar open/close buttons default to a ~28px hit
        # target — fiddly to tap precisely on a phone. Enlarge both toward the
        # ~44px mobile touch-target guideline; purely cosmetic/hit-area, no
        # behavior change. The header (holding the close button) also isn't
        # sticky by default — on a phone, scrolling down through Analytics/
        # Filters/Data/Tools pushes the close button off the top of the
        # screen entirely, with no way to close the sidebar without scrolling
        # back up first. Pin it to the top of the sidebar's own scroll area.
        st.markdown(
            f"""
            <style>
            [data-testid="stSidebarCollapseButton"] button,
            [data-testid="stExpandSidebarButton"] {{
                width: 44px !important;
                height: 44px !important;
                padding: 8px !important;
            }}
            [data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
            [data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] {{
                font-size: 28px !important;
            }}
            [data-testid="stSidebarHeader"] {{
                position: sticky;
                top: 0;
                z-index: 999;
                background-color: {_sidebar_bg};
            }}
            /* Page-link labels default to a single non-wrapping line (e.g.
               "Artist filters"), which forces the sidebar to stay wide on a
               phone to avoid clipping it. Let labels wrap to a second line
               instead, so the sidebar can be narrowed without losing text. */
            [data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] {{
                height: auto !important;
                min-height: 32px;
                padding-top: 6px !important;
                padding-bottom: 6px !important;
            }}
            [data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] > span:last-child {{
                height: auto !important;
                overflow: visible !important;
                white-space: normal !important;
            }}
            [data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] p {{
                white-space: normal !important;
                word-break: break-word;
                height: auto !important;
                line-height: 1.25;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("**Analytics**")
        for p in analytics:
            st.page_link(p)
        metric, range_sel = _sidebar_filters(df_all, is_analytics)
        st.divider()
        _sidebar_data(df_all)
        st.divider()
        st.markdown("**Tools & settings**")
        for p in tools:
            st.page_link(p)
        apply_excl, dark = _sidebar_options()

        # Selecting a page link on mobile leaves the sidebar covering the
        # whole screen with no obvious next step. Auto-collapse it after
        # navigation, matching typical mobile nav-drawer behavior; desktop is
        # left alone since the sidebar coexists with the content there.
        _autoclose_js = """
            <script>
            (function() {
                if (window.parent.__sonicSidebarAutoCloseAttached) return;
                window.parent.__sonicSidebarAutoCloseAttached = true;
                window.parent.document.addEventListener('click', function(e) {
                    var link = e.target.closest('[data-testid="stPageLink-NavLink"]');
                    if (!link) return;
                    if (window.parent.innerWidth > 768) return;
                    setTimeout(function() {
                        var doc = window.parent.document;
                        var sidebar = doc.querySelector('[data-testid="stSidebar"]');
                        var btn = doc.querySelector('[data-testid="stSidebarCollapseButton"] button');
                        if (btn && sidebar && sidebar.getAttribute('aria-expanded') === 'true') {
                            btn.click();
                        }
                    }, 150);
                }, true);
            })();
            </script>
            """
        # st.iframe (raw-HTML form) is components.v1.html's replacement, but
        # it only exists on Streamlit >=1.51ish — newer than what's pinned in
        # requirements.txt. Prefer it when present (e.g. Streamlit Community
        # Cloud, which warns on the deprecated call) and fall back otherwise,
        # so this doesn't break on older local installs.
        if hasattr(st, 'iframe'):
            # st.iframe's height must be a positive int (0 raises
            # StreamlitInvalidHeightError) — 1px is the smallest valid,
            # effectively-invisible size for this headless JS injection.
            st.iframe(_autoclose_js, height=1)
        else:
            components.html(_autoclose_js, height=0, scrolling=False)
    charts.set_theme(dark)

    excl_mtime = (os.path.getmtime(config.EXCLUSIONS_FILE)
                  if os.path.exists(config.EXCLUSIONS_FILE) else 0)
    df = (filtered_plays_cached(config.PLAYS_FILE,
                                os.path.getmtime(config.PLAYS_FILE), excl_mtime)
          if apply_excl else df_all)
    n_excluded = len(df_all) - len(df)

    value_col, value_label = metric_columns(metric)
    view = _apply_range(df, range_sel)
    ctx.update(df=df, view=view, value_col=value_col, value_label=value_label,
               excl_mtime=excl_mtime, apply_excl=apply_excl)

    st.title("🎵 sonic-stats")
    if is_analytics:
        caption = (f"{len(view):,} plays · {view['minutes_played'].sum()/60:,.0f} "
                   f"hours · {view['artist_name'].nunique():,} artists")
        caption += (f"  ·  🧒 filtered ({n_excluded:,} kid streams removed)"
                    if apply_excl and n_excluded else "  ·  unfiltered (all streams)")
        st.caption(caption)

    pg.run()


# --- Tab renderers ---

def render_artists(df, value_col, value_label):
    st.subheader("Top artists")
    top = proc.top_artists(df, n=25, metric=value_col)
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
    # Options run newest-first so the slider reads left-to-right as 2026 → older;
    # the handles come back in that order, so min/max recover the actual range.
    years_desc = sorted((int(c) for c in wide.columns), reverse=True)
    if len(years_desc) > 1:
        sel = st.select_slider(
            "Year range", options=years_desc,
            value=(years_desc[0], years_desc[-1]))
        lo, hi = min(sel), max(sel)
        wide = wide[[c for c in wide.columns if lo <= int(c) <= hi]]

    # Present newest-first: current year on the left, older years to the right.
    wide = wide[list(reversed(wide.columns))]

    st.dataframe(wide, width='stretch', height=38 * n + 60)
    st.download_button(
        "Download Markdown table",
        proc.wide_to_markdown(wide, f"Top {n} artists per year — by {metric_key}"),
        "top_artists_by_year.md", "text/markdown")


def render_tracks(df, value_col, value_label):
    st.subheader("Top tracks")
    top = proc.top_tracks(df, n=25, metric=value_col)
    label = top['track_name'] + " — " + top['artist_name']
    chart_df = top.assign(label=label)
    st.plotly_chart(charts.ranked_bar(chart_df, 'label', value_col,
                                      f"Top tracks by {value_label.lower()}"),
                    use_container_width=True)
    st.dataframe(top, width='stretch', hide_index=True)


def render_albums(df, value_col, value_label):
    st.subheader("Top albums")
    top = proc.top_albums(df, n=25, metric=value_col)
    label = top['artist_name'].fillna('Unknown') + " — " + top['album_name'].fillna('Unknown')
    chart_df = top.assign(label=label)
    st.plotly_chart(charts.ranked_bar(chart_df, 'label', value_col,
                                      f"Top albums by {value_label.lower()}"),
                    use_container_width=True)


def render_genres(df, value_col, value_label):
    st.subheader("Top genres")
    top = proc.top_genres(df, n=25, metric=value_col)
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


_WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
             'Saturday', 'Sunday']


def _fmt_hour(h):
    """24h int -> friendly clock label: 0 -> '12am', 13 -> '1pm'."""
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


def render_alltime(s):
    """All-time totals + records (all years, but honoring the kid-stream
    exclusion toggle), shown atop the Wrapped tab. `s` is the
    alltime_stats_cached() dict — computed once in main(), not here, so it
    isn't recomputed on every widget interaction while Wrapped is open."""
    st.subheader("🏅 All-time")
    if not s:
        st.info("No data yet.")
        return

    c = st.columns(4)
    c[0].metric("Plays", f"{s['total_plays']:,}")
    c[1].metric("Hours", f"{s['total_hours']:,.0f}")
    c[2].metric("Listening days", f"{s['listening_days']:,}")
    c[3].metric("Span", f"{s['span_years']} yrs")
    c = st.columns(4)
    c[0].metric("Artists", f"{s['unique_artists']:,}")
    c[1].metric("Tracks", f"{s['unique_tracks']:,}")
    c[2].metric("Albums", f"{s['unique_albums']:,}")
    c[3].metric("Genres", f"{s['unique_genres']:,}")

    share = s['top_artist'][1] / s['total_plays'] * 100
    day, day_n = s['busiest_day']
    mon, mon_n = s['busiest_month']
    yr, yr_n = s['biggest_year']
    left = [
        f"🔥 **Busiest day:** {day} ({day_n:,} plays)",
        f"📆 **Busiest month:** {mon} ({mon_n:,})",
        f"🗓️ **Biggest year:** {yr} ({yr_n:,})",
        f"🔁 **Longest streak:** {s['longest_streak']} days in a row",
        f"🕐 **Peak hour:** {_fmt_hour(s['peak_hour'])}  ·  "
        f"**Top day:** {_WEEKDAYS[s['top_weekday']]}",
    ]
    right = [
        f"🥇 **#1 artist:** {s['top_artist'][0]} "
        f"({s['top_artist'][1]:,} plays · {share:.1f}% of all)",
        f"🎵 **#1 track:** {s['top_track'][0]} ({s['top_track'][1]:,})",
        f"💿 **#1 album:** {s['top_album'][0]} ({s['top_album'][1]:,})",
        f"🎼 **#1 genre:** {s['top_genre'][0]} ({s['top_genre'][1]:,})",
        f"⏯️ **{s['avg_hours_per_week']} hrs/week**  ·  full-listen "
        f"{s['full_listen_rate']*100:.0f}%, skip {s['skip_rate']*100:.0f}%",
    ]
    col1, col2 = st.columns(2)
    col1.markdown("\n\n".join(left))
    col2.markdown("\n\n".join(right))


def render_wrapped(df, alltime):
    render_alltime(alltime)
    st.divider()
    st.subheader("Wrapped")
    # "Last 30 days" first so it stays the default (index 0) — Wrapped's
    # traditional default, unlike the sidebar filter which defaults to all-time.
    window = st.selectbox("Window", ["Last 30 days", "Last 7 days", "This month", "All time"] +
                          [str(y) for y in sorted(df['year'].dropna().unique(), reverse=True)])
    w = _apply_range(df, window)

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
    if df.empty:
        st.info("No plays in this range.")
        return
    grid = proc.patterns_heatmap(df)
    st.plotly_chart(charts.hour_dow_heatmap(grid, "Plays by hour and day of week"),
                    use_container_width=True)

    st.markdown("**Times of week**")
    top_tow = proc.top_times_of_week(df, n=5)
    lines = [
        f"{i}. **{_WEEKDAYS[int(row['day_of_week'])]} {_fmt_hour(int(row['hour']))}** — "
        f"{row['plays']:,} plays ({row['plays'] / len(df) * 100:.1f}% of all)"
        for i, row in enumerate(top_tow.to_dict('records'), start=1)
    ]
    st.markdown("\n".join(lines))

    st.markdown("**Top 5 listening hours**")
    top5 = proc.top_hours(df, n=5)
    lines = [
        f"{i}. **{_fmt_hour(int(row['hour']))}** — {row['plays']:,} plays "
        f"({row['plays'] / len(df) * 100:.1f}% of all)"
        for i, row in enumerate(top5.to_dict('records'), start=1)
    ]
    st.markdown("\n".join(lines))


def render_bands(df):
    """Bands tab: single-band deep dive + saved group summaries (full archive)."""
    st.subheader("🎤 Bands")
    mode = st.radio("Mode", ["Single band", "Groups"], horizontal=True,
                    key="bands_mode")
    if mode == "Single band":
        render_single_band(df)
    else:
        render_groups(df)


def render_single_band(df):
    artists = proc.list_artists(df)  # sorted by plays desc
    if not artists:
        st.info("No artists in the data.")
        return

    st.caption("Quick pick — your top artists:")
    cols = st.columns(5)
    for i, name in enumerate(artists[:10]):
        if cols[i % 5].button(name, key=f"qp_{i}", use_container_width=True):
            st.session_state['band_pick'] = name

    # Selectbox is the source of truth; quick-pick buttons seed its index.
    cur = st.session_state.get('band_pick', artists[0])
    idx = artists.index(cur) if cur in artists else 0
    artist = st.selectbox("Search artist", artists, index=idx)
    st.session_state['band_pick'] = artist

    f = proc.artist_facts(df, artist)
    sub = df[df['artist_name'] == artist]
    years = (f['last_played'] - f['first_played']).days / 365.25

    c = st.columns(4)
    c[0].metric("Plays", f"{f['plays']:,}")
    c[1].metric("Hours", f"{f['hours']:,.0f}")
    c[2].metric("Rank", f"#{f['rank']} / {f['total_artists']:,}")
    c[3].metric("Peak year", f"{f['peak_year']} ({f['peak_year_plays']:,})")
    st.caption(
        f"In rotation {f['first_played'].date()} → {f['last_played'].date()} "
        f"(~{years:.1f} yrs)  ·  full-listen {f['full_listen_rate']*100:.0f}%, "
        f"skip {f['skip_rate']*100:.0f}%")

    st.plotly_chart(charts.line_by_year(proc.plays_by_year(sub), 'plays',
                    f"{artist} — plays per year"), use_container_width=True)
    col1, col2 = st.columns(2)
    col1.plotly_chart(charts.ranked_bar(proc.top_tracks(sub, 10), 'track_name',
                      'plays', "Top tracks"), use_container_width=True)
    col2.plotly_chart(charts.ranked_bar(proc.top_albums(sub, 10), 'album_name',
                      'plays', "Top albums"), use_container_width=True)
    st.plotly_chart(charts.hour_dow_heatmap(proc.patterns_heatmap(sub),
                    f"{artist} — listening clock"), use_container_width=True)


def render_groups(df):
    groups = proc.load_groups()
    names = sorted(groups)
    choice = st.selectbox("Group", ["➕ New group…"] + names)
    is_new = choice == "➕ New group…"
    cur_name = "" if is_new else choice
    cur_members = [] if is_new else groups.get(choice, [])
    artists = proc.list_artists(df)

    # Key widgets by the selected group so switching groups resets the editor,
    # while edits within a group persist across reruns.
    name = st.text_input("Group name", value=cur_name, key=f"gname_{choice}")
    members = st.multiselect(
        "Bands in this group", artists,
        default=[m for m in cur_members if m in artists],
        key=f"gmembers_{choice}",
        help="Type to filter your artists (sorted by play count).")

    c1, c2, _ = st.columns([1, 1, 4])
    if c1.button("💾 Save", disabled=not (name.strip() and members)):
        if cur_name and cur_name != name.strip():
            groups.pop(cur_name, None)  # treat a name change as a rename
        groups[name.strip()] = members
        proc.save_groups(groups)
        st.success(f"Saved '{name.strip()}' ({len(members)} bands).")
        st.rerun()
    if not is_new and c2.button("🗑 Delete"):
        groups.pop(choice, None)
        proc.save_groups(groups)
        st.success(f"Deleted '{choice}'.")
        st.rerun()

    if members:
        st.divider()
        render_group_summary(df, name.strip() or "Group", members)
    else:
        st.info("Add bands above to see a group summary.")


def render_group_summary(df, name, members):
    sub = df[df['artist_name'].isin(members)]
    if sub.empty:
        st.info("No plays found for these bands.")
        return

    total, hours = len(sub), sub['minutes_played'].sum() / 60
    share = total / len(df) * 100
    st.markdown(f"### {name}")
    c = st.columns(4)
    c[0].metric("Bands", len(members))
    c[1].metric("Plays", f"{total:,}")
    c[2].metric("Hours", f"{hours:,.0f}")
    c[3].metric("Share of all plays", f"{share:.1f}%")
    st.caption(f"{sub['ts'].min().date()} → {sub['ts'].max().date()}")

    bd = proc.group_breakdown(df, members)
    show = bd.copy()
    show['First'] = pd.to_datetime(show['first_played'], utc=True).dt.date
    show['Last'] = pd.to_datetime(show['last_played'], utc=True).dt.date
    show = show[['artist_name', 'plays', 'minutes', 'rank', 'First', 'Last']].rename(
        columns={'artist_name': 'Band', 'plays': 'Plays', 'minutes': 'Minutes',
                 'rank': 'Overall rank'})
    st.dataframe(show, hide_index=True, width='stretch')

    st.plotly_chart(charts.line_by_year(proc.plays_by_year(sub), 'plays',
                    f"{name} — plays per year"), use_container_width=True)
    st.plotly_chart(charts.ranked_bar(proc.top_tracks(sub, 15), 'track_name',
                    'plays', f"{name} — top tracks"), use_container_width=True)


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


def render_artist_filters(df_all):
    """The exclusions editor — a primary concept, so it's its own Tools & settings page
    (not buried in Settings). Powers the sidebar 'Remove kid streams?' toggle."""
    st.subheader("🚫 Artist filters")
    st.write("Drop plays that weren't really yours — e.g. a shared-account "
             "period when the kids used your profile. These rules drive the "
             "sidebar **Remove kid streams?** toggle on the Analytics pages.")
    removed = len(df_all) - len(proc.apply_exclusions(df_all, proc.load_exclusions()))
    st.caption(f"These filters currently remove **{removed:,}** plays "
               f"of {len(df_all):,}.")
    st.caption("Per row: tick **All years**, or set a **Before** / **After** "
               "bound, or a comma-separated **Only** list. Bounds take a year "
               "(`2019`) or a month (`2019-06`). **Keep %** claims only a share "
               "of that period (e.g. `50` for a 50/50 split with a kid); blank = "
               "drop all. Example — Lorde, *Before* = `2020`, *Keep %* = `50` "
               "keeps half of pre-2020 plays. Add rows with the ＋ at the bottom.")
    editor_df = _exclusions_to_df(proc.load_exclusions())
    edited = st.data_editor(
        editor_df, num_rows="dynamic", width='stretch', key="excl_editor",
        column_config={
            _COL_ARTIST: st.column_config.TextColumn(_COL_ARTIST, required=True),
            _COL_ALL: st.column_config.CheckboxColumn(_COL_ALL, default=False),
            _COL_BEFORE: st.column_config.TextColumn(_COL_BEFORE),
            _COL_AFTER: st.column_config.TextColumn(_COL_AFTER),
            _COL_ONLY: st.column_config.TextColumn(_COL_ONLY),
            _COL_KEEP: st.column_config.NumberColumn(
                _COL_KEEP, min_value=0, max_value=100, step=5, format="%d"),
        })
    if st.button("Save filters"):
        proc.save_exclusions(_df_to_exclusions(edited))
        st.success("Saved. Toggle the sidebar filter or switch pages to apply.")
    with st.expander("Raw JSON"):
        st.json(proc.load_exclusions())


def render_settings(df):
    st.subheader("Settings")
    if config.DEMO_MODE:
        st.info("Demo mode — read-only sample dataset. Live sync is disabled "
                 "on this host; run the app locally with your own Spotify "
                 "export to sync your own history.")
    settings = proc.load_settings()
    last = run_pipeline._read_last_sync()
    st.write("**Data status**")
    st.write(f"- Plays loaded: {len(df):,}")
    st.write(f"- Date range: {df['ts'].min().date()} → {df['ts'].max().date()}")
    if not config.DEMO_MODE:
        st.write(f"- Last sync: {last.get('last_sync_at', 'never')} "
                 f"(+{last.get('last_new', 0)} new)")
        st.write(f"- Sync authorized: {os.path.exists(config.TOKEN_FILE)}")
    st.caption("Use the sidebar **🚫 Artist filters** to choose which artists "
               "to exclude." + ("" if config.DEMO_MODE else
                                 " Use **🔄 Sync now** to fetch recent plays."))

    st.write("**Preferences**")
    st.write(f"- Timezone: {settings.get('timezone') or 'system default'}")
    st.write(f"- Full-listen threshold: {settings.get('full_listen_threshold')}")


if __name__ == "__main__":
    main()
