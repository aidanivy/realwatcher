# Box Office Draft — Movie Game

A Flask web game where you spin Era × Studio reels and draft the highest-grossing films you can onto your marquee.

## Setup

### 1. Prerequisites

```bash
pip install flask
```

### 2. Database

Make sure you have run the data pipeline first:

```bash
# Step 1 — Pull film data from TMDB + OMDb (needs API keys in build_movie_db_api.py)
python build_movie_db_api.py

# Step 2 — Load movies_raw.json into SQLite
python load_movies_to_db.py
```

This produces `moviegame.db` in the same directory. Copy it into this folder, or set the `DB_PATH` environment variable.

### 3. Run the app

```bash
cd moviegame
flask run
```

Or directly:

```bash
python app.py
```

Then open http://localhost:5000 in your browser.

### 4. Environment variables (optional)

| Variable     | Default                          | Description                        |
|--------------|----------------------------------|------------------------------------|
| `SECRET_KEY` | `dev-secret-change-in-production`| Flask session secret — change this for production |
| `DB_PATH`    | `moviegame.db`                   | Path to the SQLite database file   |

For production, set a real secret key:

```bash
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex())')"
export DB_PATH="/path/to/moviegame.db"
flask run
```

---

## Project Structure

```
moviegame/
├── app.py                  # Flask app — all routes and game logic
├── requirements.txt
├── README.md
├── static/
│   ├── css/
│   │   └── main.css        # Vintage Hollywood marquee aesthetic
│   └── js/
│       ├── main.js         # Shared utilities (toast, bulb animation)
│       └── game.js         # Slot machine, film selection, draft calls
└── templates/
    ├── base.html           # Base layout with header/footer
    ├── lobby.html          # Name entry + how-to-play
    ├── game.html           # Main game view (spin + draft board)
    └── score.html          # Final scorecard
```

---

## Game Rules

| # | Marquee Slot        | Eligible films                            |
|---|---------------------|-------------------------------------------|
| 1 | Action / Thriller I | Tagged Action/Thriller                    |
| 2 | Action / Thriller II| Tagged Action/Thriller                    |
| 3 | Horror              | Tagged Horror                             |
| 4 | Comedy              | Tagged Comedy                             |
| 5 | Drama               | Tagged Drama                              |
| 6 | Romance             | Tagged Romance                            |
| 7 | Animated            | Tagged Animated                           |
| 8 | Oscar Nominated     | ≥ 1 Academy Award nomination              |
| 9 | Blockbuster         | Worldwide profit ≥ $250M                  |
|10 | Wildcard            | Any film                                  |

- **8 rounds** per game — one spin and one draft pick per round
- **Score** = total worldwide gross of all films placed on your marquee
- **Pass** a round if none of the pool films fit your open slots
- Drafting fills the slot; each film can only be drafted once

---

## Deployment (Render / Railway)

Add a `Procfile`:

```
web: gunicorn app:app
```

And install gunicorn:

```
pip install gunicorn
```

Both Render and Railway will auto-detect the `Procfile` and deploy. Set `SECRET_KEY` and `DB_PATH` in the platform's environment variable settings.

SQLite works fine for solo-session play. If you later add multiplayer, migrate to PostgreSQL and swap `sqlite3` for `psycopg2` + SQLAlchemy.
