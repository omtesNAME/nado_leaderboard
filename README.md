# Nado Leaderboard

Static leaderboard for top traders on [Nado DEX](https://app.nado.xyz) (Ink L2 chain).

Data is fetched hourly via GitHub Actions from the Nado Archive API and saved to `leaderboard.json`. GitHub Pages serves the static site with no backend required.

## Deploy to GitHub Pages

1. Push this repo to GitHub
2. Go to **Settings → Pages**
3. Set source to **Deploy from a branch**, branch `main`, folder `/` (root)
4. GitHub Actions will run `fetch.py` every hour and commit updated `leaderboard.json`

## Run fetch.py locally

```bash
pip install requests
python fetch.py
```

Opens `leaderboard.json` in the same directory when done.

## Stack

- Python + requests (data fetcher)
- Vanilla JS + CSS (frontend)
- GitHub Actions (hourly cron)
- GitHub Pages (hosting)
