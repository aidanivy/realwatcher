"""
Movie Game — Flask Application
================================
Routes:
  GET  /                  → lobby (enter player name)
  POST /game/new          → start a new game session
  GET  /game              → main game view (slot machine + draft board)
  POST /game/spin         → spin Era × Studio wheels, return film pool
  POST /game/draft        → draft a film into a marquee slot
  GET  /game/score        → final scorecard
  POST /game/restart      → clear session, back to lobby
  GET  /api/state         → JSON dump of current game state (for JS polling)
"""

import os
import random
import sqlite3
from datetime import datetime
from flask import (
    Flask, render_template, request, session,
    redirect, url_for, jsonify, g
)
from flask_session import Session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

# Server-side sessions — Redis on Railway, filesystem locally
_SESSION_TYPE = "redis" if os.environ.get("REDIS_URL") else "filesystem"
app.config.update(
    SESSION_TYPE=_SESSION_TYPE,
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_FILE_DIR=os.path.join(os.path.dirname(os.path.abspath(__file__)), "flask_session"),
)
if _SESSION_TYPE == "redis":
    import redis as _redis
    app.config["SESSION_REDIS"] = _redis.from_url(os.environ["REDIS_URL"])
Session(app)

# Always resolve DB path relative to this file, not the working directory
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(_HERE, "moviegame.db"))

# -- Startup diagnostics -----------------------------------------------------
def _check_db():
    if not os.path.exists(DB_PATH):
        print('\n  ✗ DATABASE NOT FOUND: ' + DB_PATH)
        print('    Run load_movies_to_db.py and place moviegame.db next to app.py')
        print('    Expected location: ' + _HERE + '\n')
        return
    try:
        con = sqlite3.connect(DB_PATH)
        count = con.execute('SELECT COUNT(*) FROM movies').fetchone()[0]
        genres = con.execute(
            'SELECT genre, COUNT(*) FROM movie_genres GROUP BY genre ORDER BY genre'
        ).fetchall()
        con.close()
        print('\n  Database: ' + DB_PATH)
        print('  Movies loaded: ' + str(count))
        print('  Genre coverage:')
        for genre, n in genres:
            print('    ' + genre.ljust(22) + ' ' + str(n) + ' films')
        print()
    except Exception as e:
        print('\n  Database error: ' + str(e) + '\n')

_check_db()


def _build_valid_combos() -> set:
    """Return set of (era, studio) tuples that have >= 5 films in the DB."""
    if not os.path.exists(DB_PATH):
        return set()
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT era, studio FROM movies GROUP BY era, studio HAVING COUNT(*) >= 5"
        ).fetchall()
        con.close()
        combos = {(r[0], r[1]) for r in rows}
        print(f"  Valid spin combos: {len(combos)}")
        return combos
    except Exception as e:
        print(f"  Could not build valid combos: {e}")
        return set()


VALID_COMBOS = _build_valid_combos()

# ── Slot / game config (must match load_movies_to_db.py) ──────────────────────
MARQUEE_SLOTS = [
    {"slot_number": 1,  "genre": "Action/Thriller", "label": "Action / Thriller I",  "icon": "🎬"},
    {"slot_number": 2,  "genre": "Action/Thriller", "label": "Action / Thriller II", "icon": "💥"},
    {"slot_number": 3,  "genre": "Horror",           "label": "Horror",               "icon": "👻"},
    {"slot_number": 4,  "genre": "Comedy",            "label": "Comedy",               "icon": "😂"},
    {"slot_number": 5,  "genre": "Drama",             "label": "Drama",                "icon": "🎭"},
    {"slot_number": 6,  "genre": "Romance",           "label": "Romance",              "icon": "💕"},
    {"slot_number": 7,  "genre": "Animated",          "label": "Animated",             "icon": "✨"},
    {"slot_number": 8,  "genre": "Oscar Nominated",   "label": "Oscar Nominated",      "icon": "🏆"},
    {"slot_number": 9,  "genre": "Blockbuster",       "label": "Blockbuster",          "icon": "💰"},
    {"slot_number": 10, "genre": None,                "label": "Wildcard",             "icon": "🃏"},
]
TOTAL_ROUNDS = 10  # one round per slot — player fills all 10
ERAS    = ["1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]
STUDIOS = ["Disney", "Warner Brothers", "Universal", "Paramount",
           "Sony/Columbia", "20th Century Fox", "MGM/UA", "Independent"]

# ── Scoring config — adjust thresholds here only ─────────────────────────────
SCORING = {
    "oscar_win_bonus_m": 50,   # $M bonus if the Oscar Nominated slot film is also an Oscar winner
    "tiers": [
        {"min": 7000, "grade": "PERFECT",     "headline": "THE GOLDEN AGE OF HOLLYWOOD"},
        {"min": 5000, "grade": "OUTSTANDING", "headline": "DIAMOND HANDS"},
        {"min": 3000, "grade": "GREAT",       "headline": "BOX OFFICE GOLD"},
        {"min": 2000, "grade": "SOLID",       "headline": "RESPECTABLE RUN"},
        {"min": 1000, "grade": "MIXED",       "headline": "CRITICS ARE DIVIDED"},
        {"min":  800, "grade": "FLOP",        "headline": "STRAIGHT TO NETFLIX"},
    ],
}

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def query(sql, params=()):
    return get_db().execute(sql, params).fetchall()

def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()

# ── Game state helpers ────────────────────────────────────────────────────────
def fresh_state(player_name: str, mode: str = "realwatcher") -> dict:
    """Return a blank game state dict stored in session."""
    return {
        "player":       player_name,
        "mode":         mode,
        "round":        1,              # 1-indexed, max TOTAL_ROUNDS
        "phase":        "spin",         # spin | draft | done
        "spin":         None,           # {"era": ..., "studio": ...}
        "pool":         [],             # list of film dicts for current round
        "marquee":      {               # slot_number (str) → film dict or None
            str(s["slot_number"]): None for s in MARQUEE_SLOTS
        },
        "drafted_ids":  [],             # tmdb IDs already drafted
        "history":      [],             # log of past rounds
        "started_at":   datetime.utcnow().isoformat(),
    }

def state() -> dict:
    return session.get("game", {})

def save_state(s: dict):
    session["game"] = s
    session.modified = True

_PRIVATE_FIELDS = {"gross_m", "budget_m", "profit_m", "oscar_noms", "oscar_wins",
                   "overview", "tmdb_url", "era"}


def film_row_to_dict(row, keep_gross=False) -> dict:
    """Convert a sqlite3.Row to a client-safe dict. gross_m kept for Classic mode."""
    d = dict(row)
    strip = _PRIVATE_FIELDS - ({"gross_m"} if keep_gross else set())
    for f in strip:
        d.pop(f, None)
    d["genre_tags"] = [t for t in (d.get("genre_str") or "").split("|") if t]
    return d


def film_full(film_id: int) -> dict:
    """Fetch all fields for a film including private scoring data. Server-side only."""
    row = query_one("SELECT * FROM movies WHERE id = ?", (film_id,))
    if not row:
        return {}
    d = dict(row)
    d["genre_tags"] = [t for t in (d.get("genre_str") or "").split("|") if t]
    return d

# ── Slot eligibility logic ────────────────────────────────────────────────────
def slot_accepts(slot: dict, film: dict) -> bool:
    """Return True if film can be placed in this slot."""
    if slot["genre"] is None:          # Wildcard
        return True
    return slot["genre"] in film.get("genre_tags", [])

def open_slots(s: dict) -> list[dict]:
    """Return marquee slots that are still empty."""
    return [
        sl for sl in MARQUEE_SLOTS
        if s["marquee"][str(sl["slot_number"])] is None
    ]

def eligible_slots_for_film(s: dict, film: dict) -> list[int]:
    """Slot numbers where this film could legally be placed."""
    return [
        sl["slot_number"] for sl in open_slots(s)
        if slot_accepts(sl, film)
    ]

def spinnable_combos(s: dict) -> list:
    """
    Return valid combos that have at least one film eligible for an open slot.
    If Wildcard is still open, any valid combo qualifies (Wildcard accepts anything).
    Falls back to all VALID_COMBOS if the filtered set is empty.
    """
    open_genre_set = {sl["genre"] for sl in open_slots(s)}

    if None in open_genre_set:          # Wildcard open — any combo works
        return list(VALID_COMBOS)

    genres = list(open_genre_set)
    already = s["drafted_ids"]
    placeholders_g = ",".join("?" * len(genres))

    if already:
        placeholders_d = ",".join("?" * len(already))
        sql = f"""
            SELECT DISTINCT m.era, m.studio
            FROM movies m
            JOIN movie_genres mg ON mg.movie_id = m.id
            WHERE mg.genre IN ({placeholders_g})
              AND m.id NOT IN ({placeholders_d})
        """
        params = genres + already
    else:
        sql = f"""
            SELECT DISTINCT m.era, m.studio
            FROM movies m
            JOIN movie_genres mg ON mg.movie_id = m.id
            WHERE mg.genre IN ({placeholders_g})
        """
        params = genres

    rows = query(sql, params)
    eligible = {(r["era"], r["studio"]) for r in rows} & VALID_COMBOS
    return list(eligible) if eligible else list(VALID_COMBOS)

# ── Scoring ───────────────────────────────────────────────────────────────────
def compute_score(s: dict) -> dict:
    """
    Score = sum of profit_m across all drafted films + Oscar win bonuses.
    Re-fetches full film data from DB so private fields are never in the session.
    """
    slots_detail     = []
    total_profit     = 0.0
    oscar_bonus      = 0.0
    filled           = 0
    top_profit       = None
    top_film_id      = None

    for sl in MARQUEE_SLOTS:
        film_stub = s["marquee"].get(str(sl["slot_number"]))
        if film_stub:
            full       = film_full(film_stub["id"])
            profit     = float(full.get("profit_m") or 0)
            oscar_wins = int(full.get("oscar_wins") or 0)
            total_profit += profit
            if sl["slot_number"] == 8 and oscar_wins > 0:
                oscar_bonus = SCORING["oscar_win_bonus_m"]
            if top_profit is None or profit > top_profit:
                top_profit  = profit
                top_film_id = film_stub["id"]
            filled += 1
        else:
            oscar_wins = 0
        slots_detail.append({
            "slot":       sl,
            "film":       film_stub,
            "oscar_wins": oscar_wins,
        })

    final_score = round(total_profit + oscar_bonus, 1)

    tier = SCORING["tiers"][-1]
    for t in SCORING["tiers"]:
        if final_score >= t["min"]:
            tier = t
            break

    return {
        "total_profit": round(total_profit, 1),
        "oscar_bonus":  round(oscar_bonus, 1),
        "top_film_id":  top_film_id,
        "final_score":      final_score,
        "filled":           filled,
        "slots":            slots_detail,
        "tier":             tier,
    }

# ── Template helpers ─────────────────────────────────────────────────────────
@app.template_filter("dollars")
def dollars_filter(value):
    """Format a dollar value in $M, switching to $B if >= 1000."""
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"${value / 1000:.1f}B"
    return f"${value:,.0f}M"

# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    session.clear()
    return render_template("mode_select.html")


@app.post("/game/mode")
def game_mode():
    mode = request.form.get("mode", "realwatcher")
    if mode not in ("classic", "realwatcher"):
        mode = "realwatcher"
    session["pending_mode"] = mode
    return redirect(url_for("lobby"))


@app.route("/lobby")
def lobby():
    mode = session.get("pending_mode", "realwatcher")
    return render_template("lobby.html", mode=mode)


@app.post("/game/new")
def game_new():
    player = request.form.get("player_name", "").strip() or "Player 1"
    mode   = request.form.get("mode", "realwatcher")
    save_state(fresh_state(player, mode))
    return redirect(url_for("game"))


@app.route("/game")
def game():
    s = state()
    if not s:
        return redirect(url_for("lobby"))
    if s["phase"] == "done":
        return redirect(url_for("score"))
    return render_template(
        "game.html",
        s=s,
        slots=MARQUEE_SLOTS,
        total_rounds=TOTAL_ROUNDS,
    )


@app.post("/game/spin")
def game_spin():
    s = state()
    if not s or s["phase"] != "spin":
        return jsonify({"error": "Not in spin phase"}), 400

    valid = spinnable_combos(s)
    era, studio = random.choice(valid)

    mode       = s.get("mode", "realwatcher")
    keep_gross = mode == "classic"
    order_by   = "m.gross_m DESC" if mode == "classic" else "m.title ASC"
    already    = s["drafted_ids"]

    not_in_clause = ""
    if already:
        placeholders  = ",".join("?" * len(already))
        not_in_clause = f"AND m.id NOT IN ({placeholders})"

    sql = f"""
        SELECT DISTINCT
            m.id, m.title, m.year, m.era, m.studio,
            m.gross_m, m.poster_url, m.tmdb_url, m.overview, m.genre_str
        FROM movies m
        WHERE m.era = ? AND m.studio = ?
          {not_in_clause}
        ORDER BY {order_by}
        LIMIT 20
    """
    params = [era, studio] + already

    rows = query(sql, params)
    pool = [film_row_to_dict(r, keep_gross=keep_gross) for r in rows]

    # Debug: log the spin result to terminal
    print(f"  Spin: {era} x {studio} -> {len(pool)} films found")

    s["spin"]  = {"era": era, "studio": studio}
    s["pool"]  = pool
    s["phase"] = "draft"
    save_state(s)

    return jsonify({
        "era":    era,
        "studio": studio,
        "pool":   pool,
        "open_slots": [
            {"slot_number": sl["slot_number"], "label": sl["label"],
             "genre": sl["genre"], "icon": sl["icon"]}
            for sl in open_slots(s)
        ],
    })


@app.post("/game/draft")
def game_draft():
    s = state()
    if not s or s["phase"] != "draft":
        return jsonify({"error": "Not in draft phase"}), 400

    film_id   = request.json.get("film_id")
    slot_num  = request.json.get("slot_number")

    if film_id is None or slot_num is None:
        return jsonify({"error": "film_id and slot_number required"}), 400

    slot_num = int(slot_num)
    film_id  = int(film_id)

    # Validate slot is open
    slot_obj = next((sl for sl in MARQUEE_SLOTS if sl["slot_number"] == slot_num), None)
    if not slot_obj:
        return jsonify({"error": "Invalid slot"}), 400
    if s["marquee"][str(slot_num)] is not None:
        return jsonify({"error": "Slot already filled"}), 400

    # Validate film is in current pool
    film = next((f for f in s["pool"] if f["id"] == film_id), None)
    if not film:
        return jsonify({"error": "Film not in current pool"}), 400

    # Validate genre fit
    if not slot_accepts(slot_obj, film):
        return jsonify({
            "error": f"'{film['title']}' doesn't qualify for the {slot_obj['label']} slot"
        }), 400

    # Commit the draft
    s["marquee"][str(slot_num)] = film
    s["drafted_ids"].append(film_id)
    s["history"].append({
        "round":  s["round"],
        "era":    s["spin"]["era"],
        "studio": s["spin"]["studio"],
        "film":   film["title"],
        "slot":   slot_obj["label"],
    })

    # Advance round
    s["round"] += 1
    s["pool"]   = []
    s["spin"]   = None

    if s["round"] > TOTAL_ROUNDS:
        s["phase"] = "done"
    else:
        s["phase"] = "spin"

    save_state(s)

    return jsonify({
        "ok":       True,
        "phase":    s["phase"],
        "round":    s["round"],
        "marquee":  s["marquee"],
    })


@app.route("/game/score")
def score():
    s = state()
    if not s:
        return redirect(url_for("lobby"))
    result = compute_score(s)
    return render_template("score.html", s=s, result=result, slots=MARQUEE_SLOTS, scoring=SCORING)


@app.post("/game/restart")
def restart():
    session.clear()
    return redirect(url_for("index"))


@app.get("/api/state")
def api_state():
    s = state()
    if not s:
        return jsonify({"error": "No active game"}), 404
    return jsonify({
        "phase":       s["phase"],
        "round":       s["round"],
        "total_rounds": TOTAL_ROUNDS,
        "player":      s["player"],
        "spin":        s["spin"],
        "marquee":     s["marquee"],
        "open_slots":  [
            {"slot_number": sl["slot_number"], "label": sl["label"], "genre": sl["genre"]}
            for sl in open_slots(s)
        ],
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
