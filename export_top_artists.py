"""
export_top_artists.py — dev tool: write top-N artists per year in several views.

Loads the GDPR export from data/raw/, builds the play DataFrame (no API
enrichment needed — artist + year come straight from the raw data), and writes:

  * <name>_long.csv   tidy long format: year, rank, artist_name, plays, minutes
  * <name>_wide.csv   rank chart: rows = rank, columns = year, cells = artist
  * <name>.md         stylized Markdown table (years across the top)

Ranking metric defaults to minutes of listening.

Usage:
    python export_top_artists.py                      # top 10 by minutes
    python export_top_artists.py -n 25 --metric plays
    python export_top_artists.py --show-values        # embed the metric in cells
    python export_top_artists.py -o data/processed/top_artists
"""
import argparse
import os

from src import config, fetch_data, process_data as proc

DEFAULT_BASE = os.path.join(config.PROCESSED_DIR, 'top_artists_by_year')


def main():
    parser = argparse.ArgumentParser(description="Export top-N artists per year.")
    parser.add_argument('-n', '--top', type=int, default=10,
                        help="number of artists per year (default 10)")
    parser.add_argument('--metric', choices=['plays', 'minutes'], default='minutes',
                        help="ranking dimension (default minutes)")
    parser.add_argument('--show-values', action='store_true',
                        help="embed the metric value in wide/markdown cells")
    parser.add_argument('-o', '--output-base', default=DEFAULT_BASE,
                        help=f"output path prefix (default {DEFAULT_BASE})")
    parser.add_argument('--no-exclude', action='store_true',
                        help="ignore data/exclusions.json (keep all artists)")
    args = parser.parse_args()

    plays = fetch_data.load_gdpr_export()
    df = proc.build_plays_df(plays, track_cache={}, artist_cache={})

    if not args.no_exclude:
        before = len(df)
        df = proc.apply_exclusions(df, proc.load_exclusions())
        dropped = before - len(df)
        if dropped:
            print(f"   Excluded {dropped:,} plays via data/exclusions.json")

    long = proc.top_artists_per_year(df, n=args.top, metric=args.metric)
    wide = proc.top_artists_wide(df, n=args.top, metric=args.metric,
                                 show_values=args.show_values)

    os.makedirs(os.path.dirname(args.output_base) or '.', exist_ok=True)
    long_path = f"{args.output_base}_long.csv"
    wide_path = f"{args.output_base}_wide.csv"
    md_path = f"{args.output_base}.md"

    long.to_csv(long_path, index=False)
    wide.to_csv(wide_path)  # keep the rank index as the first column
    md_title = f"Top {args.top} artists per year — by {args.metric}"
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(proc.wide_to_markdown(wide, md_title))

    n_years = long['year'].nunique()
    print(f"✅ Top {args.top} artists/year by {args.metric} across {n_years} years:")
    print(f"   long CSV : {long_path}")
    print(f"   wide CSV : {wide_path}")
    print(f"   markdown : {md_path}")


if __name__ == '__main__':
    main()
