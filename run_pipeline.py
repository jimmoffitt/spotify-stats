"""
run_pipeline.py — CLI: load GDPR export → enrich → process → save.

Orchestrates the offline build of data/processed/plays.parquet from the GDPR
export in data/raw/ plus the cached API enrichment. Run after dropping your
Spotify export into data/raw/:

    python run_pipeline.py

See DESIGN.md "Data Flow" for the full sequence.
"""
from src import config, enrich_data, fetch_data, process_data


def main():
    config.validate_config()

    # 1. Load the GDPR streaming-history backbone.
    plays = fetch_data.load_gdpr_export()

    # 2. Enrich: track metadata (duration/release/album), then artist genres.
    #    Catalog endpoints (/tracks, /artists) need no user scope, so use the
    #    app-only Client Credentials token — no interactive OAuth required.
    access_token = fetch_data.get_client_credentials_token()
    track_cache = enrich_data.enrich_tracks(plays, access_token)
    artist_cache = enrich_data.enrich_artists(track_cache, access_token)

    # 3. Merge + derive columns -> plays.parquet.
    df = process_data.build_plays_df(plays, track_cache, artist_cache)
    process_data.save_plays(df)


if __name__ == '__main__':
    main()
