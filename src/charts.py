"""
src/charts.py — Plotly figure factories for the Streamlit dashboard.

Every public function accepts a DataFrame (or simple arrays) and returns a
go.Figure — no Streamlit calls, so they are independently testable. Call
set_theme(dark=True/False) once per render cycle (from the sidebar) to switch
all subsequent figures between dark and light palettes. _base_layout() is the
shared layout builder used by every chart.
"""
import plotly.graph_objects as go

SPOTIFY_GREEN = '#1DB954'
SPOTIFY_GREEN_LIGHT = '#1ED760'
ACCENT_GRAY = 'rgba(150, 150, 150, 0.35)'

_dark = True


def set_theme(dark: bool) -> None:
    global _dark
    _dark = dark


def _plot_bg():
    return '#1a1c24' if _dark else 'white'


def _paper_bg():
    return '#0e1117' if _dark else 'white'


def _grid_color():
    return 'rgba(255,255,255,0.10)' if _dark else '#eeeeee'


def _font_color():
    return '#e8e8e8' if _dark else '#31333F'


def _base_layout(**kwargs) -> dict:
    base = dict(
        plot_bgcolor=_plot_bg(),
        paper_bgcolor=_paper_bg(),
        font=dict(color=_font_color()),
        margin=dict(t=50, b=40, l=40, r=20),
    )
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Chart factories
# ---------------------------------------------------------------------------

def ranked_bar(df, name_col, value_col='plays', title=None, height=None):
    """
    Horizontal ranked bar — used for top artists / tracks / albums / genres.
    Expects df pre-sorted descending; flips so the largest sits at the top.
    """
    d = df.iloc[::-1]  # plotly draws bottom-up; reverse so rank 1 is on top
    # Label each bar with its value directly (inside if the bar's long enough
    # to fit it, else just outside the tip) — these lists run tall enough that
    # the x-axis scale is often scrolled out of view.
    num_fmt = ',.0f' if value_col == 'plays' else ',.1f'
    fig = go.Figure(go.Bar(
        x=d[value_col],
        y=d[name_col].astype(str),
        orientation='h',
        marker_color=SPOTIFY_GREEN,
        text=d[value_col],
        texttemplate=f'%{{text:{num_fmt}}}',
        textposition='auto',
        textfont=dict(color=_font_color()),
        cliponaxis=False,
        hovertemplate=f'%{{y}}<br>{value_col}: %{{x}}<extra></extra>',
    ))
    fig.update_layout(**_base_layout(
        title=title,
        xaxis=dict(title=value_col, gridcolor=_grid_color()),
        yaxis=dict(title=None, automargin=True, tickfont=dict(size=15)),
        height=height or max(300, 30 * len(df) + 80),
    ))
    return fig


def line_by_year(df, value_col='plays', title=None):
    """Trend of plays (or minutes) per calendar year."""
    fig = go.Figure(go.Scatter(
        x=df['year'], y=df[value_col],
        mode='lines+markers',
        line=dict(color=SPOTIFY_GREEN, width=3),
        marker=dict(size=8, color=SPOTIFY_GREEN_LIGHT),
        hovertemplate=f'%{{x}}<br>{value_col}: %{{y}}<extra></extra>',
    ))
    fig.update_layout(**_base_layout(
        title=title,
        xaxis=dict(title='Year', dtick=1, gridcolor=_grid_color()),
        yaxis=dict(title=value_col, gridcolor=_grid_color()),
        height=380,
    ))
    return fig


def decade_bar(df, value_col='plays', title=None):
    """Vertical bar of plays (or minutes) per release decade."""
    fig = go.Figure(go.Bar(
        x=df['decade'].astype(str) + 's',
        y=df[value_col],
        marker_color=SPOTIFY_GREEN,
        hovertemplate=f'%{{x}}<br>{value_col}: %{{y}}<extra></extra>',
    ))
    fig.update_layout(**_base_layout(
        title=title,
        xaxis=dict(title='Decade'),
        yaxis=dict(title=value_col, gridcolor=_grid_color()),
        height=380,
    ))
    return fig


# Fixed hue per genre family (never reassigned by rank/filter — see
# process_data.GENRE_MACRO_COLOR_ORDER) plus a neutral gray for the leftover
# "Other" bucket. Light/dark pairs are a validated CVD-safe categorical set;
# treemap leaves are directly labeled, so color here is a secondary cue, not
# the sole identity signal.
_GENRE_MACRO_COLORS_LIGHT = {
    'Rock / Indie': '#2a78d6',
    'Pop': '#eb6834',
    'Folk / Americana': '#1baf7a',
    'Punk': '#eda100',
    'Hip-Hop / R&B': '#e87ba4',
    'Electronic': '#008300',
    'Metal': '#4a3aa7',
    'World / Reggae / Jazz': '#e34948',
}
_GENRE_MACRO_COLORS_DARK = {
    'Rock / Indie': '#3987e5',
    'Pop': '#d95926',
    'Folk / Americana': '#199e70',
    'Punk': '#c98500',
    'Hip-Hop / R&B': '#d55181',
    'Electronic': '#008300',
    'Metal': '#9085e9',
    'World / Reggae / Jazz': '#e66767',
}
_GENRE_OTHER_COLOR = {'light': '#9aa0a6', 'dark': '#6b7078'}


def _hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return '#' + ''.join(f'{max(0, min(255, round(c))):02x}' for c in rgb)


def _tint(hex_color, bg_hex, factor=0.35):
    """Blend hex_color toward bg_hex — used to shade a treemap leaf a
    lighter step of its parent macro-genre's hue."""
    a, b = _hex_to_rgb(hex_color), _hex_to_rgb(bg_hex)
    return _rgb_to_hex(a[i] + (b[i] - a[i]) * factor for i in range(3))


def genre_treemap(tree_df, value_col='plays', title=None):
    """Two-level treemap: genre families (process_data.GENRE_MACRO_COLOR_ORDER)
    sized by their full total, each holding its top micro-genres tinted a
    lighter step of the family hue. Built from
    process_data.genre_group_treemap_data(); expects id/label/parent_id/value
    columns, macro rows first (parent_id='')."""
    palette = _GENRE_MACRO_COLORS_DARK if _dark else _GENRE_MACRO_COLORS_LIGHT
    other = _GENRE_OTHER_COLOR['dark' if _dark else 'light']
    # _plot_bg() is 'white' (a CSS name, not hex) in light mode; _tint needs hex.
    bg = _plot_bg() if _plot_bg().startswith('#') else '#ffffff'

    colors = []
    for _, row in tree_df.iterrows():
        is_macro = row['parent_id'] == ''
        base = palette.get(row['id'] if is_macro else row['parent_id'], other)
        colors.append(base if is_macro else _tint(base, bg))

    num_fmt = ',.0f' if value_col == 'plays' else ',.1f'
    fig = go.Figure(go.Treemap(
        ids=tree_df['id'],
        labels=tree_df['label'],
        parents=tree_df['parent_id'],
        values=tree_df['value'],
        branchvalues='total',
        marker=dict(colors=colors, line=dict(color=_paper_bg(), width=2)),
        textfont=dict(color=_font_color()),
        texttemplate=f'%{{label}}<br>%{{value:{num_fmt}}}',
        hovertemplate=f'%{{label}}<br>{value_col}: %{{value:{num_fmt}}}<extra></extra>',
    ))
    fig.update_layout(**_base_layout(title=title, height=500))
    return fig


_DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def hour_dow_heatmap(grid, title=None):
    """
    Time-of-day heatmap from process_data.patterns_heatmap() — a 7x24 grid
    (index=day_of_week 0-6, columns=hour 0-23), colored by play count.
    """
    fig = go.Figure(go.Heatmap(
        z=grid.values,
        x=[f'{h:02d}' for h in grid.columns],
        y=_DOW_LABELS,
        colorscale=[[0, _plot_bg()], [1, SPOTIFY_GREEN]],
        hovertemplate='%{y} %{x}:00<br>plays: %{z}<extra></extra>',
        colorbar=dict(title='plays'),
    ))
    fig.update_layout(**_base_layout(
        title=title,
        xaxis=dict(title='Hour of day', tickmode='linear', dtick=2),
        yaxis=dict(title=None, autorange='reversed'),
        height=380,
    ))
    return fig
