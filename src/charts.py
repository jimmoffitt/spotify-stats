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
    fig = go.Figure(go.Bar(
        x=d[value_col],
        y=d[name_col].astype(str),
        orientation='h',
        marker_color=SPOTIFY_GREEN,
        hovertemplate=f'%{{y}}<br>{value_col}: %{{x}}<extra></extra>',
    ))
    fig.update_layout(**_base_layout(
        title=title,
        xaxis=dict(title=value_col, gridcolor=_grid_color()),
        yaxis=dict(title=None, automargin=True),
        height=height or max(300, 28 * len(df) + 80),
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
