"""
gen_screenshots.py — dev tool: render dashboard visuals to docs/screenshots/.

Produces PNGs used in the README without driving a browser, by rendering the
same Plotly figures the app uses (exclusions applied, light theme):

    docs/screenshots/artists.png   — Artists tab: top artists by minutes
    docs/screenshots/rankings.png  — Rankings tab: top artists per year

Usage: python gen_screenshots.py
"""
import os

import plotly.graph_objects as go

from src import charts, process_data as proc

OUT_DIR = os.path.join('docs', 'screenshots')


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = proc.apply_exclusions(proc.load_plays(), proc.load_exclusions())
    charts.set_theme(False)  # light

    # --- Artists tab: top artists by minutes ---
    top = proc.top_artists(df, n=15, metric='minutes')
    fig = charts.ranked_bar(top, 'artist_name', 'minutes', "Top artists by minutes")
    fig.update_layout(width=900, height=620)
    fig.write_image(os.path.join(OUT_DIR, 'artists.png'), scale=2)

    # --- Rankings tab: top artists per year (recent years for a readable width) ---
    wide = proc.top_artists_wide(df, n=10, metric='minutes')
    wide = wide[list(wide.columns)[-8:]]  # most recent 8 years
    header = ['Rank'] + list(wide.columns)
    cells = [list(wide.index)] + [wide[c].fillna('').tolist() for c in wide.columns]
    tbl = go.Figure(go.Table(
        columnwidth=[40] + [110] * len(wide.columns),
        header=dict(values=header, fill_color=charts.SPOTIFY_GREEN,
                    font=dict(color='white', size=13), align='left', height=30),
        cells=dict(values=cells, align='left', height=26,
                   fill_color='white', font=dict(size=12)),
    ))
    tbl.update_layout(width=1150, height=520,
                      margin=dict(t=40, b=10, l=10, r=10),
                      title="Top 10 artists per year — by minutes")
    tbl.write_image(os.path.join(OUT_DIR, 'rankings.png'), scale=2)

    print(f"✅ Wrote {OUT_DIR}/artists.png and {OUT_DIR}/rankings.png")


if __name__ == '__main__':
    main()
