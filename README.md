# Box Office Draft for the #RealWatcher

A Flask web game where you spin Era × Studio reels and draft films onto a 7-slot marquee. Score is calculated in **WAE (Watcher Adjusted Earnings)** — a weighted mix of box office gross, profitability, critic ratings, and Oscar hardware.

Two modes: **RealWatcher** (grosses hidden during draft — pure instinct) and **Classic** (grosses visible, sorted by revenue).

---

## Quick Start

```bash
cd moviegame
pip install -r requirements.txt
python app.py
# → http://127.0.0.1:5000
```

The database (`moviegame.db`) is committed to the repo and ready to use. Startup prints movie count, valid spin combos, and genre coverage.

---

## Project Structure

```
moviegame/
├── app.py                  # All routes, game logic, and scoring config
├── moviegame.db            # SQLite database (1,359 films)
├── requirements.txt
├── Procfile                # gunicorn entry point for Railway/Render
├── railway.toml
├── render.yaml
├── static/
│   ├── css/main.css        # Vintage Hollywood marquee aesthetic
│   └── js/
│       ├── main.js         # Toast notifications, bulb animation
│       └── game.js         # Reel spin animation, film picker, draft calls
└── templates/
    ├── base.html           # Header, footer, scoring info modal
    ├── mode_select.html    # RealWatcher vs Classic mode picker
    ├── lobby.html          # Ticket-style name entry + how-to-play
    ├── game.html           # Slot machine reels + draft board + film pool
    ├── score.html          # Final scorecard with tier headline
    ├── leaderboard.html    # Separate RealWatcher + Classic leaderboards
    └── shared_score.html   # Public shareable scorecard view
```

---

## Game Rules

**7 rounds** — one spin and one draft pick per round. All 7 slots must be filled.

| # | Slot              | Eligible films                            |
|---|-------------------|-------------------------------------------|
| 1 | Action            | Tagged Action                             |
| 2 | Horror / Thriller | Tagged Horror/Thriller                    |
| 3 | Drama             | Tagged Drama                              |
| 4 | Romance / Comedy  | Tagged Romance/Comedy                     |
| 5 | Blockbuster       | Worldwide gross ≥ $100M                   |
| 6 | Oscar Nominated   | ≥ 1 Academy Award nomination              |
| 7 | Wildcard          | Any film                                  |

- Spin lands on an **Era** (70s–20s) × **Studio** (8 majors) combination
- Only combos with ≥ 5 eligible films are valid spin targets
- The same Era × Studio combo cannot be spun back-to-back
- One free **respin** per game (available during the draft phase)
- Each film can only be drafted once per game

---

## Scoring — WAE (Watcher Adjusted Earnings)

All scoring config lives in the `SCORING` dict at the top of `app.py`.

### Era Inflation Adjustment
Gross and budget for older eras are scaled before scoring to account for inflation:

| Era | Multiplier |
|-----|-----------|
| 70s | 5×        |
| 80s | 3×        |
| 90s | 2×        |
| 00s–20s | 1× (no adjustment) |

Displayed figures always show the original box office numbers.

### Base Formula (per slot)

```
profitability  = gross − (budget × 2)
commercial     = 0.80 × gross + 0.40 × profitability
prestige       = 0.45 × critic_score + 0.40 × oscar_noms + 0.25 × oscar_wins
pre_bonus      = 0.75 × commercial + 0.35 × prestige
slot_score     = max((pre_bonus + bonus) × multiplier, 0)
```

### Per-Slot Bonuses

| Slot              | Mult | Bonus |
|-------------------|------|-------|
| Action            | 1×   | +10/+20/+30M for profitability/budget > 3×/5×/10× · +5M if critic 60+ · +10M if critic 75+ |
| Horror / Thriller | 1×   | +10/+20/+30M for profitability/budget > 3×/5×/10× · +5M if critic 65+ |
| Romance / Comedy  | 1×   | +10/+20/+30M for profitability/budget > 3×/5×/10× · +5M if critic 65+ |
| Drama             | 1×   | +5M critic 70–79 · +10M critic 80–89 · +15M critic 90+ · +2M/nom · +3M/win |
| Wildcard          | 1.5× | +2M if critic 65+ · +0.5M/nom · +1M/win · +1M if Animated |
| Oscar Nominated   | 2×   | +5M/nom · +10M/win · +10M BP nom · +15M BP win (uncapped) |
| Blockbuster       | 2.5× | +10M per $100M above threshold · +10M if > $1B gross (max +150M) |

### Score Tiers

| Min Score | Grade       | Headline           |
|-----------|-------------|--------------------|
| 12,000+   | PERFECT     | "CINEMA"           |
| 10,000+   | OUTSTANDING | IMPECCABLE TASTE   |
| 7,000+    | GREAT       | BOX OFFICE GOLD    |
| 5,000+    | SOLID       | RESPECTABLE RUN    |
| 3,000+    | DECENT      | I MEAN, SURE       |
| 0+        | FLOP        | BOMB               |

---

## Environment Variables

| Variable     | Default                           | Description                                        |
|--------------|-----------------------------------|----------------------------------------------------|
| `SECRET_KEY` | `dev-secret-change-in-production` | Flask session secret — required for production     |
| `DB_PATH`    | `./moviegame.db`                  | Path to the SQLite database                        |
| `REDIS_URL`  | *(unset)*                         | If set, uses Redis for sessions (Railway provides) |

---

## Deployment (Railway / Render)

Both platforms auto-detect the `Procfile`. Set `SECRET_KEY` in the platform dashboard. `REDIS_URL` is provided automatically on Railway if you add a Redis plugin.

```bash
# Generate a secret key
python -c "import secrets; print(secrets.token_hex())"
```

The SQLite database is committed to the repo and deployed with the app — suitable for solo-session play. Migrate to PostgreSQL if multiplayer is ever added.

---

## Data Pipeline (rebuilding the database)

The pipeline scripts live in the parent directory and are not required for normal use:

```
build_movie_db_api.py   # Pull from TMDB + OMDb → movies_raw.json + posters/
apply_golden_oscars.py  # Enrich Oscar data from oscars_golden.json
load_movies_to_db.py    # Load movies_raw.json → moviegame.db
```

API keys required: [TMDB](https://www.themoviedb.org/settings/api) (free) and [OMDb](https://www.omdbapi.com/apikey.aspx) (free tier). Set them in the `CONFIG` block at the top of `build_movie_db_api.py`.
