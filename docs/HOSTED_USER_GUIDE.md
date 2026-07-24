# sonic-stats — Getting Started With Your Own History

> **Status: DRAFT / design doc.** This describes the *planned* hosted experience
> where anyone can explore their own Spotify history — it is the spec we'll build
> to, not how the app behaves today. The companion `USER_GUIDE.md` covers running
> the app locally as a developer.

Welcome! **sonic-stats** turns your *entire* Spotify listening history — not
just the last year — into an interactive dashboard: your all-time favorites,
who topped your charts each year, your listening patterns, and more.

You don't need an account here, a login, or any technical setup. You'll do two
things:

1. **Ask Spotify for your listening history** — a one-time request on their
   site that takes a few days to arrive. **Do this first:** see
   [Getting your history from Spotify](#getting-your-history-from-spotify) at
   the end of this guide, then come back here when your file lands.
2. **Upload that file here** and start exploring (below).

> **Your privacy, up front.** Your history is processed in your browser session
> and is **never saved on our server**. Close the tab and it's gone. (More in
> [Privacy & data handling](#privacy--data-handling).)

---

## Load it into the dashboard

> Already requested your data and got the `.zip` from Spotify? Great — here's
> the fun part. (If not, start with
> [Getting your history from Spotify](#getting-your-history-from-spotify) first.)

1. Open the app and you'll land on a **welcome screen** with a single big
   **"Upload your Spotify history"** drop zone.
2. **Drag your `.zip` onto it** (or click to browse). You can also drop the
   individual `Streaming_History_Audio_*.json` files if you've already unzipped.
3. The app reads your file and shows a quick confirmation, e.g.:
   > ✅ **Loaded 45,231 plays** spanning **2016 – 2026** across **1,203 artists**.
4. **Optional — richer detail.** A prompt offers to *"Add genres & release
   years."* This fetches extra info from Spotify so the **Genres** and
   **Decades** views light up. It runs in the background with a progress bar;
   you can start exploring immediately and it fills in as it goes. (It's
   optional — skip it and everything except genre/decade stats still works.)
5. You're in. The full dashboard opens on your data.

> **Tip — skip the wait next time.** After loading, you can **Download your
> processed history** (a single small file). Next visit, upload *that* instead of
> the big Spotify zip and you'll be in instantly, genres and all.

---

## Exploring your music

Everything is driven from the **left sidebar**: pick a view, set your filters.

- **🗓️ Wrapped** — your all-time totals and records (busiest day, longest
  streak, #1 artist of all time), plus a Wrapped-style recap for any window.
- **🎸 Artists / 🎵 Tracks / 💿 Albums / 🎼 Genres** — your top lists, by play
  count or listening time, for any year or all-time.
- **🏆 Rankings** — who was your #1 each year, newest year first.
- **🎤 Bands** — deep-dive on any single artist, or build **groups** of bands
  (say, a "Road trip" or "New Zealand" group) and see them summarized together.
- **🕐 Patterns** — when you listen, by hour and day of week.
- **📅 Decades** — your listening by the era the music was released.

Two controls shape every view:

- **Year** — focus on a single year, or all-time.
- **Rank by** — count plays, or total minutes listened.

> A fuller tour of each view, with screenshots, lives in the main
> [User's Guide](USER_GUIDE.md) — the views are identical once your data is loaded.

### Making it yours (this session)

- **Filter out other people's listening.** Shared a Spotify account with family?
  In **Settings** you can drop specific artists (all years, before/after a year,
  or a percentage split) so the stats reflect *you*. 
- **Save band groups** you assemble in the **Bands** tab.

Because nothing is stored on the server, these live in your current session. You
can **export your settings** (filters + groups) to a small file and re-load it
next visit, so you don't have to set them up again.

---

## Keeping your history up to date

Your uploaded file is a snapshot from the day Spotify generated it. To include
newer listening, **request a fresh export** (Part 1) and upload it — the
dashboard rebuilds from the new file.

> *Planned:* an optional **"Connect Spotify"** button to pull your most recent
> plays on top of an upload, so you can refresh without re-requesting the whole
> export. (Not in the first version.)

---

## Privacy & data handling

- **Nothing is stored.** Your history is processed **in memory, in your browser
  session.** We don't write it to a database or disk. Close or refresh the tab
  and your data is cleared.
- **No account, no tracking of you.** There's no login and no per-user profile.
- **What leaves your session:** if you choose the optional "Add genres & release
  years" step, the app asks Spotify's public API for facts about the *tracks*
  (genre, release date) — never your identity or full history.
- **The downloadable "processed history"** stays on *your* computer; it's only
  re-uploaded if you choose to.

---

## Frequently asked

**How far back does it go?** As far as your Spotify account — often a decade or
more. (The "Extended" export is the key; the basic one is roughly one year.)

**How big is the file / how long to load?** Exports are typically a few to tens
of MB. Loading takes seconds; the optional enrichment can take a few minutes the
first time.

**Can others see my data?** No. Each visitor's data lives only in their own
session.

**I got "Account data," not "Extended streaming history."** That package won't
have enough detail — re-request and pick **Extended streaming history**.

---

## Getting your history from Spotify

This is the one-time request you make on Spotify's own site. It's free, but it
isn't instant — Spotify can take a few days (occasionally up to ~30) to prepare
it, so **do this first** and come back to [upload it](#load-it-into-the-dashboard)
when it arrives.

1. Go to **[spotify.com/account/privacy](https://www.spotify.com/account/privacy)**
   and sign in.
2. Scroll to **"Download your data."**
3. You'll see a few options. Tick **Extended streaming history** — this is the
   one with your *complete* play-by-play history.
   > ⚠️ **Not "Account data."** That smaller package only covers about the last
   > year and lacks the detail this dashboard needs. Make sure you pick
   > **Extended streaming history**.
4. Click **Request data** and confirm (Spotify may email you a link to confirm —
   click it, or the request won't start).
5. **Wait for the email.** Spotify emails you when the file is ready, with a
   download link. This usually takes a few days.
6. Download the file — a `.zip` named something like `my_spotify_data.zip`.
   **Keep it zipped;** you'll upload it exactly as-is.

That's the slow part. Once it arrives, [loading it](#load-it-into-the-dashboard)
takes a couple of minutes.

---

## Appendix — Design notes & open questions (for us, not end users)

*This section is for the build, and would be removed from the published guide.
These are the decisions the guide above assumes — flagging them for sign-off.*

1. **Upload format.** Accept the raw Spotify `.zip` *and* loose
   `Streaming_History_Audio_*.json` files. Unzip in-memory, select the
   `Audio` files, ignore `Video`/other. *(Assumed throughout.)*
2. **Enrichment strategy.** Core stats (plays, minutes, artists, tracks, albums,
   rankings, patterns, Wrapped) render immediately from the upload alone.
   Genres/Decades/“full-listen” need the Spotify API, so enrichment is
   **optional + background**, backed by a **shared, server-side track-metadata
   cache keyed by track URI** — so popular tracks are already enriched and it
   gets faster as more people use the app. *Open: is a shared cache acceptable
   (it's track facts, not user data), or keep enrichment fully per-session?*
3. **Per-session isolation.** All uploaded data + derived frames live in
   `st.session_state`; nothing persisted. Need to confirm memory headroom on the
   host (a 50k-play upload is a few MB in pandas — fine for ≤10 users).
4. **Settings/exclusions/groups become per-session**, with **export/import** via
   a small JSON the user downloads and re-uploads. (Today these are server-side
   files for the single owner.)
5. **"Download processed history"** = the enriched parquet (or a compact JSON),
   so returning users skip the Spotify zip and the enrichment wait.
6. **No live sync in hosted mode.** Recently-played sync needs per-user OAuth;
   v1 is upload-only. The optional "Connect Spotify" top-up is a later add.
7. **Credentials.** Enrichment uses the app's own Client-Credentials token
   (one shared app identity), not the visitor's — so no user login is required.
   *Open: rate-limit handling when several users enrich at once.*
