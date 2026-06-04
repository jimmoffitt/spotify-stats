# spotify-stats

A personal Spotify listening-history dashboard built with [Streamlit](https://streamlit.io/).
It turns your Spotify **Extended Streaming History** export into the views that
Spotify Wrapped leaves out: how your taste shifts year over year, which artists
and decades dominate, and when you actually listen.

## Features

- **GDPR loader** — reads the `Streaming_History_Audio_*.json` files from your
  Spotify data export and merges them into one clean play log.
- **Rankings** — top artists per year (a years-across-the-top rank chart),
  rankable by play count or minutes listened.
- **Artists / Tracks / Albums / Genres / Decades** — all-time and per-year tops.
- **Patterns** — an hour-of-day × day-of-week listening heatmap.
- **Wrapped / Explore / Export** — period summaries, full-text search, CSV export.
- **Artist exclusions** — filter out shared-account streams (e.g. a kid's
  listening) per artist, with year or month (`2019-06`) resolution, toggleable
  live in the UI.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional — for genre/release-date enrichment and live sync, create a Spotify app
at <https://developer.spotify.com/dashboard>, then copy `example.env` to
`.local.env` and fill in your credentials:

```bash
cp example.env .local.env
python -m src.setup_tokens     # one-time OAuth authorization
```

## Usage

1. Request your data from Spotify (Account → Privacy → *Download your data*,
   "Extended streaming history"). Unzip and place the
   `Streaming_History_Audio_*.json` files in `data/raw/`.
2. Build the processed play log:

   ```bash
   python run_pipeline.py
   ```

3. Launch the dashboard:

   ```bash
   python -m streamlit run app.py
   ```

Dev tool — export the top artists per year to CSV / Markdown without the UI:

```bash
python export_top_artists.py            # top 10 by minutes
python export_top_artists.py --help     # options
```

## Project structure

```
app.py               # Streamlit dashboard (all tabs)
run_pipeline.py      # CLI: load export → enrich → process → plays.parquet
export_top_artists.py# dev tool: top-N artists per year → CSV / Markdown
src/
  config.py          # paths, constants, OAuth config
  fetch_data.py      # Spotify OAuth/refresh, recently-played, GDPR loader
  enrich_data.py     # track + artist metadata enrichment (Spotify API)
  process_data.py    # DataFrame build, aggregations, exclusions
  charts.py          # Plotly figure factories
data/                # local data (gitignored)
```

## Contributors

This project is developed through **pair programming with
[Claude Code](https://www.anthropic.com/claude-code)**, Anthropic's agentic
command-line coding tool. The human partner sets direction, reviews, and tests
against real data; Claude Code drafts and iterates on the implementation. Design,
architecture, and feature decisions are made collaboratively in that loop.

## Acknowledgements

Modeled after the [strava-stats](https://github.com/jimmoffitt/strava-stats)
dashboard, which established the `fetch_data` / `process_data` / `charts` /
Streamlit-tabs pattern reused here.
