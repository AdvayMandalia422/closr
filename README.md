# closr.

**Closest wins.** A daily NFL and NBA game based on the Instagram comment trend "name a player closest to #32 all-time in receiving yards."

Each round shows a stat and a target rank. You name the player you think sits closest to that rank, picking them from an autocomplete list. Points depend on how far your pick's real rank is from the target:

| Spots off | Points |
|---|---|
| 0 (dead on) | 100 |
| 2 | 85 |
| 5 | 66 |
| 10 | 43 |
| 20 | 19 |

Points are `100 × e^(−spots/12)`, rounded. Tied players share a rank, and landing anywhere inside a tie that covers the target counts as dead on.

Each league gets six rounds a day, for 600 points max. The rounds mix:

- **This season so far**, once teams have played a few games (3 for the NFL, 12 for the NBA)
- **The last three completed seasons**
- **Career lists**:
  - NFL: career totals for active players, built from nflverse seasons going back to 1999
  - NBA: all-time regular-season leaders from Basketball Reference

The whole game is a single page, [`index.html`](index.html), with no dependencies.

## How the daily rounds are made

A GitHub Action ([`.github/workflows/refresh.yml`](.github/workflows/refresh.yml)) runs every morning at 09:17 UTC. Each run:

1. Downloads current-season stats:
   - NFL player stats and game results from [nflverse](https://github.com/nflverse)
   - NBA totals and standings from [Basketball Reference](https://www.basketball-reference.com)
2. Schedules six rounds per league for yesterday through two days ahead in [`data/schedule.json`](data/schedule.json). Days that already have rounds are never changed, so everyone gets the same set all day in every time zone.
3. Embeds yesterday's, today's and tomorrow's rounds into `index.html`.
4. Saves each finished day to [`archive/`](archive/) (one JSON file per date, plus `archive/index.json`). The in-game Archive lists these so people can play past sets. Archive plays don't change stats or streaks.
5. Commits and deploys to GitHub Pages.

[`data/static.json`](data/static.json) holds the past-season lists, NBA all-time lists and NFL career totals. It's rebuilt automatically every 30 days.

Each round picks a target with at most two players tied at that value, so there is a clear answer:

| Round type | Target range |
|---|---|
| Current season | #6–#30 |
| Past seasons | #8–#40 |
| Career, active NFL players | #8–#45 |
| NBA all-time | #10–#100 |

## Run it locally

Needs Python 3.10 or newer. No packages to install.

```bash
python3 scripts/build.py
```

Then open `index.html` in a browser.

Other options:

```bash
python3 scripts/build.py --today 2026-11-15     # build as if it were another day
python3 scripts/build.py --rebuild-static       # refetch past seasons and career lists now
python3 scripts/build.py --backfill 7           # fill in missing rounds for the past 7 days
```

Downloads are cached in `data/raw/` (not committed).

## Adding a stat

Stats are defined in `NFL_STATS` and `NBA_STATS` in [`scripts/build.py`](scripts/build.py).

- **Season stats:** each entry names the source column and a unit.
- **NFL career rounds:** use any stat that has a single `col`.
- **NBA all-time rounds:** map to Basketball Reference leader pages through `NBA_CAREER`.

## Notes

- Basketball Reference limits automated requests. The daily run makes two requests spaced a few seconds apart; the monthly rebuild makes about a dozen. If they're blocked, current-season NBA rounds are skipped that day and the run log says why.
- Not affiliated with the NFL or NBA.
