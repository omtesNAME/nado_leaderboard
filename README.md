# Nado Leaderboard

Static leaderboard for top traders on [Nado DEX](https://app.nado.xyz) on Ink L2.

The frontend is fully static. Heavy data processing runs in GitHub Actions through `fetch.py`, which reads the Nado Archive API, maintains incremental aggregate state in `data/pipeline_state.json`, and materializes `leaderboard.json` for the browser.

## Architecture

```text
Nado Archive API
  -> fetch.py
  -> data/pipeline_state.json
  -> leaderboard.json
  -> GitHub Pages static UI
```

The browser does not query the Archive API directly. It only loads `leaderboard.json`.

## Deploy

Recommended demo setup:

- Hosting: GitHub Pages
- Data updates: GitHub Actions
- Storage: committed `leaderboard.json` and `data/pipeline_state.json`
- Backend: none
- Database: none

Expected GitHub Pages URL for this repo:

```text
https://omtesname.github.io/nado_leaderboard/
```

To enable Pages:

1. Push this repo to GitHub.
2. Open **Settings -> Pages**.
3. Set source to **Deploy from a branch**.
4. Select branch `main`, folder `/` root.

## Full Bootstrap

The current partial local files are useful for UI testing, but production/demo data requires one full archive bootstrap.

From GitHub Actions:

1. Open **Actions -> Update Leaderboard**.
2. Click **Run workflow**.
3. Set `full_refresh` to `true`.
4. Wait for the action to finish.
5. Confirm that `leaderboard.json` and `data/pipeline_state.json` were committed.

Local equivalent:

```bash
NADO_FULL_REFRESH=1 NADO_REQUEST_DELAY=0 python fetch.py
```

The bootstrap streams archive pages directly into aggregates, so it does not keep the full match history in memory. On GitHub Actions it runs in chunks using `NADO_BOOTSTRAP_PAGE_BUDGET`; if the archive is too large for one run, the workflow commits a checkpoint and the next run resumes from `bootstrap_next_start`.

## Incremental Updates

After the full bootstrap, scheduled hourly runs use the committed state and only scan newest archive pages until already-seen matches are reached. The workflow is not triggered on every push, so a code push will not accidentally run incremental processing against a partial bootstrap.

Safety knobs:

- `NADO_FULL_REFRESH=1` rebuilds state from the full archive.
- `NADO_ASSUME_NEWEST_FIRST=0` disables early incremental stop and scans the full archive.
- `NADO_MAX_INCREMENTAL_PAGES=200` aborts incremental runs if known overlap is not reached.
- `NADO_BOOTSTRAP_PAGE_BUDGET=5000` limits each bootstrap run so GitHub Actions can checkpoint progress before the 6 hour job limit.
- `NADO_RECENT_KEY_LIMIT=50000` controls the dedupe overlap window.

## Demo Checklist

Before sharing with the Nado/Ink team:

1. Run full bootstrap with `full_refresh=true`.
2. Verify the site stats row says `archive: complete`.
3. Check `1d`, `1w`, `1m`, and `all-time`.
4. Check top 20/50/100.
5. Check wallet search.
6. Run one normal workflow with `full_refresh=false` to verify incremental updates.
7. Confirm GitHub Pages serves the latest `leaderboard.json`.

## Stack

- Python + requests
- Vanilla JS + CSS
- GitHub Actions
- GitHub Pages
