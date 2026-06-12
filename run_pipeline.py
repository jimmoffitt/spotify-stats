"""
run_pipeline.py — build and maintain data/processed/plays.parquet.

Two-phase data model (see README "Data retrieval & storage"):

  Phase 1 — bootstrap (one-time): load the GDPR export from data/raw/, enrich
            tracks + artists via the Spotify API, and write plays.parquet.
  Phase 2 — sync (ongoing): pull the latest plays from recently-played, dedupe,
            append to data/synced_plays.json, and rebuild plays.parquet.

Commands:
    python run_pipeline.py --bootstrap   # one-time GDPR load + full enrichment
    python run_pipeline.py --sync        # incremental: fetch recent, dedupe, append
    python run_pipeline.py --enrich      # re-run enrichment + rebuild (add --force to refetch)
    python run_pipeline.py --status      # last sync time, total plays, gap risk

Enrichment uses the app-only Client Credentials token (no browser). --sync also
needs the user-authorized token from `python -m src.setup_tokens`.
"""
# Silence urllib3's NotOpenSSLWarning (system Python 3.9 links LibreSSL, which
# urllib3 v2 doesn't certify). Harmless, and it keeps the auto-sync log clean.
# Match by message — importing the warning class would trigger it first.
import warnings

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")

import argparse
import json
import os
from datetime import datetime, timezone

from src import config, enrich_data, fetch_data, process_data


# --- synced-plays store + last-sync state ---

def _load_synced():
    if os.path.exists(config.SYNCED_PLAYS_FILE):
        with open(config.SYNCED_PLAYS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def _save_synced(plays):
    os.makedirs(os.path.dirname(config.SYNCED_PLAYS_FILE), exist_ok=True)
    with open(config.SYNCED_PLAYS_FILE, 'w', encoding='utf-8') as f:
        json.dump(plays, f, indent=2, ensure_ascii=False)


def _read_last_sync():
    if os.path.exists(config.LAST_SYNC_FILE):
        with open(config.LAST_SYNC_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _write_last_sync(**updates):
    """Merge updates into last_sync.json. Only sync() sets 'last_sync_at'."""
    state = _read_last_sync()
    state.update(updates)
    with open(config.LAST_SYNC_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    return state


# --- shared build steps ---

def _dedupe_key(rec):
    return (rec['ts'], rec.get('spotify_track_uri'))


def _load_all_plays():
    """GDPR export (data/raw) merged with incrementally-synced plays, deduped."""
    plays = fetch_data.load_gdpr_export()
    synced = _load_synced()
    if synced:
        seen = {_dedupe_key(p) for p in plays}
        added = 0
        for p in synced:
            if _dedupe_key(p) not in seen:
                seen.add(_dedupe_key(p))
                plays.append(p)
                added += 1
        plays.sort(key=lambda r: r['ts'])
        print(f"   Merged {added} synced plays (of {len(synced)} stored).")
    return plays


def _enrich_and_build(plays, force=False):
    """Enrich tracks + artists (app-only token), build and save plays.parquet."""
    token = fetch_data.get_client_credentials_token()
    track_cache = enrich_data.enrich_tracks(plays, token, force=force)
    artist_cache = enrich_data.enrich_artists(track_cache, token, force=force)
    df = process_data.build_plays_df(plays, track_cache, artist_cache)
    process_data.save_plays(df)
    return df


# --- commands ---

def bootstrap():
    config.validate_config()
    plays = _load_all_plays()
    df = _enrich_and_build(plays)
    _write_last_sync(total_plays=len(df))
    print(f"\n✅ Bootstrap complete: {len(df):,} plays in {config.PLAYS_FILE}")


def enrich(force=False):
    config.validate_config()
    plays = _load_all_plays()
    df = _enrich_and_build(plays, force=force)
    _write_last_sync(total_plays=len(df))
    print(f"\n✅ Enrichment complete: {len(df):,} plays {'(forced refetch)' if force else ''}")


def sync():
    """Fetch recently-played, dedupe, append to the synced store, rebuild parquet."""
    config.validate_config()
    if not os.path.exists(config.TOKEN_FILE):
        raise FileNotFoundError(
            "Not authorized for sync. Run `python -m src.setup_tokens` once first.")

    token = fetch_data.get_access_token()
    after_ms = _read_last_sync().get('last_played_at_ms')
    items = fetch_data.fetch_recently_played(token, after_ts=after_ms)
    records = fetch_data.recently_played_to_records(items)
    print(f"recently-played: {len(items)} items -> {len(records)} music plays")

    # Dedupe new records against everything we already have (GDPR + synced),
    # on (ts, track_uri) — the same track can legitimately repeat, so both keys.
    existing = fetch_data.load_gdpr_export()
    synced = _load_synced()
    seen = {_dedupe_key(p) for p in existing} | {_dedupe_key(p) for p in synced}

    added = 0
    for rec in records:
        if _dedupe_key(rec) not in seen:
            seen.add(_dedupe_key(rec))
            synced.append(rec)
            added += 1

    if added:
        _save_synced(synced)

    plays = _load_all_plays()
    df = _enrich_and_build(plays)

    new_last_ms = fetch_data.latest_played_at_ms(items)
    state = {'total_plays': len(df), 'last_new': added,
             'last_sync_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    if new_last_ms is not None:
        state['last_played_at_ms'] = new_last_ms
    _write_last_sync(**state)

    print(f"\n✅ Sync complete: {added} new play(s) appended; {len(df):,} total.")
    return {'added': added, 'total': len(df)}


def status():
    state = _read_last_sync()
    total = state.get('total_plays')
    if total is None and os.path.exists(config.PLAYS_FILE):
        total = len(process_data.load_plays())

    print("=== spotify-stats status ===")
    print(f"  plays.parquet exists : {os.path.exists(config.PLAYS_FILE)}")
    print(f"  total plays          : {total:,}" if total is not None else
          "  total plays          : (run --bootstrap)")
    print(f"  sync authorized      : {os.path.exists(config.TOKEN_FILE)} "
          f"(token file {'present' if os.path.exists(config.TOKEN_FILE) else 'missing'})")

    last_at = state.get('last_sync_at')
    if last_at:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_at.replace('Z', '+00:00'))
        hours = delta.total_seconds() / 3600
        print(f"  last sync            : {last_at} ({hours:.1f}h ago)")
        # recently-played caps at 50 plays; a heavy listener can exceed that in a
        # day, so flag long gaps as a risk that plays were missed.
        if hours > 12:
            print("  ⚠️  gap risk         : >12h since last sync; heavy listening "
                  "may exceed the 50-play recently-played window. Sync more often.")
    else:
        print("  last sync            : never")
    print(f"  last new plays       : {state.get('last_new', 0)}")


def main():
    parser = argparse.ArgumentParser(description="Build/maintain plays.parquet.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--bootstrap', action='store_true',
                       help="one-time GDPR load + full enrichment (default)")
    group.add_argument('--sync', action='store_true',
                       help="incremental: fetch recent plays, dedupe, append, rebuild")
    group.add_argument('--enrich', action='store_true',
                       help="re-run enrichment + rebuild parquet")
    group.add_argument('--status', action='store_true',
                       help="print last sync time, total plays, gap risk")
    parser.add_argument('--force', action='store_true',
                        help="with --enrich: refetch all metadata (ignore caches)")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.sync:
        sync()
    elif args.enrich:
        enrich(force=args.force)
    else:  # --bootstrap or no flag (back-compat default)
        bootstrap()


if __name__ == '__main__':
    main()
