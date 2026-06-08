"""
Movie Game — Flask Application
================================
Routes:
  GET  /                  → lobby (enter player name)
  POST /game/new          → start a new game session
  GET  /game              → main game view (slot machine + draft board)
  POST /game/spin         → spin Era × Studio wheels, return film pool
  POST /game/draft        → draft a film into a marquee slot
  POST /game/pass         → pass on current round (no film drafted)
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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

DB_PATH = os.environ.get("DB_PATH", "moviegame.db")

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
TOTAL_ROUNDS = 8   # player fills 8 of the 10 slots per game
ERAS    = ["70s", "80s", "90s", "00s", "10s", "20s"]
STUDIOS = ["Disney", "Warner Brothers", "Universal", "Paramount",
           "Sony/Columbia", "20th Century Fox", "MGM/UA", "Independent"]

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
def fresh_state(player_name: str) -> dict:
    """Return a blank game state dict stored in session."""
    return {
        "player":       player_name,
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

def film_row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict for JSON / session storage."""
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

# ── Scoring ───────────────────────────────────────────────────────────────────
def compute_score(s: dict) -> dict:
    """
    Tally worldwide gross across all filled marquee slots.
    Returns {"total_gross": float, "total_profit": float, "filled": int,
             "slots": [{slot, film, gross, profit}, ...]}
    """
    slots_detail = []
    total_gross  = 0.0
    total_profit = 0.0
    filled = 0

    for sl in MARQUEE_SLOTS:
        film = s["marquee"].get(str(sl["slot_number"]))
        gross  = float(film["gross_m"]  or 0) if film else 0.0
        profit = float(film["profit_m"] or 0) if film else 0.0
        total_gross  += gross
        total_profit += profit
        if film:
            filled += 1
        slots_detail.append({
            "slot":   sl,
            "film":   film,
            "gross":  gross,
            "profit": profit,
        })

    return {
        "total_gross":  round(total_gross,  1),
        "total_profit": round(total_profit, 1),
        "filled":       filled,
        "slots":        slots_detail,
    }

# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def lobby():
    session.clear()
    return render_template("lobby.html")


@app.post("/game/new")
def game_new():
    player = request.form.get("player_name", "").strip() or "Player 1"
    save_state(fresh_state(player))
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

    era    = random.choice(ERAS)
    studio = random.choice(STUDIOS)

    # Pull eligible films for this combo — exclude already drafted
    already = s["drafted_ids"]
    placeholders = ",".join("?" * len(already)) if already else "NULL"
    sql = f"""
        SELECT DISTINCT
            m.id, m.title, m.year, m.era, m.studio,
            m.gross_m, m.profit_m, m.oscar_noms, m.oscar_wins,
            m.poster_url, m.tmdb_url, m.overview, m.genre_str
        FROM movies m
        WHERE m.era = ? AND m.studio = ?
          AND m.id NOT IN ({placeholders if already else 'SELECT NULL'})
        ORDER BY m.gross_m DESC
        LIMIT 12
    """
    params = [era, studio] + already
    rows = query(sql, params)
    pool = [film_row_to_dict(r) for r in rows]

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


@app.post("/game/pass")
def game_pass():
    """Skip this round without drafting a film."""
    s = state()
    if not s or s["phase"] != "draft":
        return jsonify({"error": "Not in draft phase"}), 400

    s["history"].append({
        "round":  s["round"],
        "era":    s["spin"]["era"],
        "studio": s["spin"]["studio"],
        "film":   None,
        "slot":   None,
    })
    s["round"] += 1
    s["pool"]   = []
    s["spin"]   = None

    if s["round"] > TOTAL_ROUNDS:
        s["phase"] = "done"
    else:
        s["phase"] = "spin"

    save_state(s)
    return jsonify({"ok": True, "phase": s["phase"], "round": s["round"]})


@app.route("/game/score")
def score():
    s = state()
    if not s:
        return redirect(url_for("lobby"))
    result = compute_score(s)
    return render_template("score.html", s=s, result=result, slots=MARQUEE_SLOTS)


@app.post("/game/restart")
def restart():
    session.clear()
    return redirect(url_for("lobby"))


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
