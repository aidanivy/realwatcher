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

import base64
import json
import os
import random
import secrets
import sqlite3
import zlib
from datetime import datetime, timezone
from flask import (
    Flask, render_template, request, session,
    redirect, url_for, jsonify, g
)
from markupsafe import Markup
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

# Seed persistent volume on first deploy (Railway: DB_PATH points to /data/moviegame.db)
_SEED_DB = os.path.join(_HERE, "moviegame.db")
if DB_PATH != _SEED_DB and not os.path.exists(DB_PATH) and os.path.exists(_SEED_DB):
    import shutil
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    shutil.copy2(_SEED_DB, DB_PATH)
    print(f"  Seeded DB: {_SEED_DB} -> {DB_PATH}")

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


def _ensure_scores_table():
    if not os.path.exists(DB_PATH):
        return
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                player      TEXT    NOT NULL,
                mode        TEXT    NOT NULL,
                final_score INTEGER NOT NULL,
                grade       TEXT    NOT NULL,
                played_at   TEXT    NOT NULL,
                share_token TEXT    UNIQUE,
                result_json TEXT
            )
        """)
        for col, defn in [("share_token", "TEXT"), ("result_json", "TEXT")]:
            try:
                con.execute(f"ALTER TABLE scores ADD COLUMN {col} {defn}")
            except Exception:
                pass
        con.commit()
        con.close()
    except Exception as e:
        print(f"  Could not create scores table: {e}")

_ensure_scores_table()


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
    {"slot_number": 1, "genre": "Action",          "label": "Action",           "icon": "🎬"},
    {"slot_number": 2, "genre": "Horror/Thriller", "label": "Horror / Thriller","icon": "👻"},
    {"slot_number": 3, "genre": "Drama",           "label": "Drama",            "icon": "🎭"},
    {"slot_number": 4, "genre": "Romance/Comedy",  "label": "Romance / Comedy", "icon": "💕"},
    {"slot_number": 5, "genre": "Blockbuster",     "label": "Blockbuster",      "icon": "💰"},
    {"slot_number": 6, "genre": "Oscar Nominated", "label": "Oscar Nominated",  "icon": "🏆"},
    {"slot_number": 7, "genre": None,              "label": "Wildcard",         "icon": "🃏"},
]
TOTAL_ROUNDS = 7  # one round per slot — player fills all 7
ERAS    = ["1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]
STUDIOS = ["Disney", "Warner Brothers", "Universal", "Paramount",
           "Sony/Columbia", "20th Century Fox", "MGM/UA", "Independent"]

# ── Scoring config — adjust thresholds and slot rules here only ──────────────
# WAE = Watcher Adjusted Earnings (unit for all scores)
# Formula per slot:
#   profitability = gross_m - (budget_m * profit_budget_mult)
#   commercial    = commercial_weights[0]*gross_m + commercial_weights[1]*profitability
#   prestige      = prestige_weights[0]*critic + prestige_weights[1]*noms + prestige_weights[2]*wins
#   pre_bonus     = final_weights[0]*commercial + final_weights[1]*prestige
#   slot_score    = max((pre_bonus + bonus) * multiplier, 0)
SCORING = {
    "profit_budget_mult": 2,            # profitability = gross - (budget × this)
    "commercial_weights": (0.80, 0.40), # (gross, profitability)
    "prestige_weights":   (0.45, 0.40, 0.25),  # (critic_score, oscar_noms, oscar_wins)
    "final_weights":      (0.75, 0.35), # (commercial, prestige)
    "inflation_mult": {"70s": 5, "80s": 3, "90s": 2},  # flat CPI adj for scoring only
    "tiers": [
        {"min": 12000, "grade": "PERFECT",     "headline": "CINEMA"},
        {"min": 10000, "grade": "OUTSTANDING", "headline": "IMPECCABLE TASTE"},
        {"min": 7000,  "grade": "GREAT",       "headline": "BOX OFFICE GOLD"},
        {"min": 5000,  "grade": "SOLID",       "headline": "RESPECTABLE RUN"},
        {"min": 3000,  "grade": "DECENT",      "headline": "I MEAN, SURE"},
        {"min":    0,  "grade": "FLOP",        "headline": "BOMB"},
    ],
    "slot_rules": [
        {
            "genre": "Action",          "label": "Action",
            "mult": 1.0,
            "ratio_type":  "profit",
            "ratio_tiers": [(3, 10), (5, 20), (10, 30)],
            "critic_thresh": 60, "critic_amt": 5,
            "critic_thresh2": 75, "critic_amt2": 10,
        },
        {
            "genre": "Horror/Thriller", "label": "Horror / Thriller",
            "mult": 1.0,
            "ratio_type":  "profit",
            "ratio_tiers": [(3, 10), (5, 20), (10, 30)],
            "critic_thresh": 65, "critic_amt": 5,
        },
        {
            "genre": "Romance/Comedy",  "label": "Romance / Comedy",
            "mult": 1.0,
            "ratio_type":  "profit",
            "ratio_tiers": [(3, 10), (5, 20), (10, 30)],
            "critic_thresh": 65, "critic_amt": 5,
        },
        {
            "genre": "Drama",           "label": "Drama",
            "mult": 1.0,
            "critic_tiers": [(70, 5), (80, 10), (90, 15)],
            "nom_bonus": 2, "win_bonus": 3,
        },
        {
            "genre": None,              "label": "Wildcard",
            "mult": 1.5,
            "critic_thresh": 65, "critic_amt": 2,
            "nom_bonus": 0.5, "win_bonus": 1.0,
            "animated_bonus": 1,
        },
        {
            "genre": "Oscar Nominated", "label": "Oscar Nominated",
            "mult": 2.0,
            "per_nom": 5, "per_win": 10, "bp_nom_amt": 10, "bp_win_amt": 15,
            "rw_note": "#RealWatcher: any film accepted — 2× and bonuses only apply if actually nominated (1× otherwise)",
        },
        {
            "genre": "Blockbuster",     "label": "Blockbuster",
            "mult": 2.5,
            "per_100m": 10, "over_1b": 10, "block_cap": 150,
        },
    ],
}


def _slot_rule_desc(r: dict) -> str:
    """Build a human-readable bonus description from a slot rule dict."""
    parts = []
    if "ratio_tiers" in r:
        rtype      = "profitability" if r.get("ratio_type") == "profit" else "gross"
        tier_str   = " / ".join(f"+{b}M" for _, b in r["ratio_tiers"])
        thresh_str = " / ".join(f"{t}×" for t, _ in r["ratio_tiers"])
        parts.append(f"{tier_str} for {rtype}/budget > {thresh_str}")
    if "critic_thresh" in r:
        parts.append(f"+{r['critic_amt']}M if critic {r['critic_thresh']}+")
    if "critic_thresh2" in r:
        parts.append(f"+{r['critic_amt2']}M if critic {r['critic_thresh2']}+")
    if "critic_tiers" in r:
        tiers = r["critic_tiers"]
        for i, (thresh, amt) in enumerate(tiers):
            if i + 1 < len(tiers):
                parts.append(f"+{amt}M critic {thresh}–{tiers[i+1][0]-1}")
            else:
                parts.append(f"+{amt}M critic {thresh}+")
    if "nom_bonus" in r:
        parts.append(f"+{r['nom_bonus']}M/nom · +{r['win_bonus']}M/win")
    if "animated_bonus" in r:
        parts.append(f"+{r['animated_bonus']}M if Animated")
    if "per_nom" in r:
        cap_str = f" (max +{r['oscar_cap']}M)" if "oscar_cap" in r else ""
        parts.append(
            f"+{r['per_nom']}M per nom · +{r['per_win']}M per win"
            f" · +{r['bp_nom_amt']}M BP nom · +{r['bp_win_amt']}M BP win{cap_str}"
        )
    if "per_100m" in r:
        parts.append(
            f"+{r['per_100m']}M per $100M above threshold"
            f" · +{r['over_1b']}M if > $1B gross"
            f" (max +{r['block_cap']}M)"
        )
    return " · ".join(parts) if parts else "—"


for _r in SCORING["slot_rules"]:
    _r["desc"] = _slot_rule_desc(_r)

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
        "player":        player_name,
        "mode":          mode,
        "round":         1,              # 1-indexed, max TOTAL_ROUNDS
        "phase":         "spin",         # spin | draft | done
        "spin":          None,           # {"era": ..., "studio": ...}
        "pool":          [],             # list of film dicts for current round
        "marquee":       {               # slot_number (str) → film dict or None
            str(s["slot_number"]): None for s in MARQUEE_SLOTS
        },
        "drafted_ids":   [],             # tmdb IDs already drafted
        "history":       [],             # log of past rounds
        "respin_used":   False,          # one free respin per game
        "running_total": 0.0,            # cumulative WAE score
        "slot_scores":   {},             # slot_number (str) → WAE score
        "started_at":    datetime.now(timezone.utc).isoformat(),
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
    era = d.get("era", "")  # capture before era is stripped
    strip = _PRIVATE_FIELDS - ({"gross_m"} if keep_gross else set())
    for f in strip:
        d.pop(f, None)
    d["genre_tags"] = [t for t in (d.get("genre_str") or "").split("|") if t]
    d["inflation_mult"] = SCORING["inflation_mult"].get(era, 1)
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
def slot_accepts(slot: dict, film: dict, mode: str = "") -> bool:
    """Return True if film can be placed in this slot."""
    if slot["genre"] is None:
        return True
    if slot["genre"] == "Oscar Nominated" and mode == "realwatcher":
        return True  # RealWatcher: Oscar slot accepts any film
    return slot["genre"] in film.get("genre_tags", [])

def open_slots(s: dict) -> list[dict]:
    """Return marquee slots that are still empty."""
    return [
        sl for sl in MARQUEE_SLOTS
        if s["marquee"][str(sl["slot_number"])] is None
    ]

def eligible_slots_for_film(s: dict, film: dict) -> list[int]:
    """Slot numbers where this film could legally be placed."""
    mode = s.get("mode", "")
    return [
        sl["slot_number"] for sl in open_slots(s)
        if slot_accepts(sl, film, mode)
    ]

def spinnable_combos(s: dict) -> list:
    """
    Return valid combos that have at least one film eligible for an open slot.
    If Wildcard is still open, any valid combo qualifies (Wildcard accepts anything).
    In RealWatcher mode, Oscar slot also accepts any film, so same applies.
    Falls back to all VALID_COMBOS if the filtered set is empty.
    """
    open_genre_set = {sl["genre"] for sl in open_slots(s)}
    mode = s.get("mode", "")

    if None in open_genre_set:          # Wildcard open — any combo works
        return list(VALID_COMBOS)
    if mode == "realwatcher" and "Oscar Nominated" in open_genre_set:
        return list(VALID_COMBOS)  # Oscar slot open in RealWatcher — any combo works

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
def compute_profitability(gross_m: float, budget_m: float) -> float:
    if not gross_m or not budget_m:
        return 0.0
    return gross_m - (budget_m * SCORING["profit_budget_mult"])


def compute_slot_score(film_data: dict, slot_obj: dict) -> float:
    """
    Compute the WAE score for one drafted film in one slot.
    Score is floored at 0 (a flop cannot subtract from your total).
    All bonus values and multipliers are driven by SCORING["slot_rules"].
    """
    era          = film_data.get("era", "")
    inf_mult     = SCORING["inflation_mult"].get(era, 1.0)
    gross_m      = float(film_data.get("gross_m")      or 0) * inf_mult
    budget_m     = float(film_data.get("budget_m")     or 0) * inf_mult
    oscar_noms   = int(film_data.get("oscar_noms")     or 0)
    oscar_wins   = int(film_data.get("oscar_wins")     or 0)
    vote_average = float(film_data.get("vote_average") or 0)
    bp_nom       = bool(film_data.get("best_picture_nominated"))
    bp_won       = bool(film_data.get("best_picture_won"))

    profitability = compute_profitability(gross_m, budget_m)
    cw = SCORING["commercial_weights"]
    pw = SCORING["prestige_weights"]
    fw = SCORING["final_weights"]
    commercial   = cw[0] * gross_m + cw[1] * profitability
    critic_score = vote_average * 10  # 0–10 → 0–100
    prestige     = pw[0] * critic_score + pw[1] * oscar_noms + pw[2] * oscar_wins
    pre_bonus    = fw[0] * commercial  + fw[1] * prestige

    genre = slot_obj.get("genre")
    rule  = next((r for r in SCORING["slot_rules"] if r["genre"] == genre), {})

    bonus      = 0.0
    multiplier = rule.get("mult", 1.0)

    if "ratio_tiers" in rule and budget_m > 0:
        ratio = (compute_profitability(gross_m, budget_m) / budget_m
                 if rule.get("ratio_type") == "profit"
                 else gross_m / budget_m)
        for thresh, amt in reversed(rule["ratio_tiers"]):
            if ratio > thresh:
                bonus = float(amt)
                break

    if "critic_thresh" in rule and critic_score >= rule["critic_thresh"]:
        bonus += rule["critic_amt"]

    if "critic_thresh2" in rule and critic_score >= rule["critic_thresh2"]:
        bonus += rule["critic_amt2"]

    if "critic_tiers" in rule:
        for thresh, amt in reversed(rule["critic_tiers"]):
            if critic_score >= thresh:
                bonus += float(amt)
                break

    if "nom_bonus" in rule:
        bonus += oscar_noms * rule["nom_bonus"] + oscar_wins * rule["win_bonus"]

    if "animated_bonus" in rule:
        if "Animated" in film_data.get("genre_tags", []):
            bonus += rule["animated_bonus"]

    if "per_nom" in rule:
        if oscar_noms == 0 and oscar_wins == 0 and not bp_nom and not bp_won:
            multiplier = 1.0  # not actually nominated — no bonus, no multiplier
        else:
            raw = oscar_noms * rule["per_nom"] + oscar_wins * rule["per_win"]
            if bp_nom: raw += rule["bp_nom_amt"]
            if bp_won: raw += rule["bp_win_amt"]
            bonus = float(min(raw, rule["oscar_cap"]) if "oscar_cap" in rule else raw)

    if "per_100m" in rule and gross_m >= 100:
        b = int((gross_m - 100) / 100) * rule["per_100m"]
        if gross_m > 1000:
            b += rule["over_1b"]
        bonus = min(float(b), rule["block_cap"])

    return max(round((pre_bonus + bonus) * multiplier, 1), 0.0)


def compute_score(s: dict) -> dict:
    """
    Build the final scorecard from per-slot WAE scores stored in session.
    Re-fetches oscar_wins from DB for the display (never stored in session).
    """
    slots_detail    = []
    total_wae       = 0.0
    total_adj_gross = 0.0
    total_noms      = 0
    total_wins      = 0
    filled          = 0
    top_wae         = None
    top_film_id     = None
    slot_scores     = s.get("slot_scores", {})

    for sl in MARQUEE_SLOTS:
        film_stub  = s["marquee"].get(str(sl["slot_number"]))
        slot_score = float(slot_scores.get(str(sl["slot_number"]), 0.0))

        if film_stub:
            full         = film_full(film_stub["id"])
            oscar_wins   = int(full.get("oscar_wins") or 0)
            oscar_noms   = int(full.get("oscar_noms") or 0)
            gross_m      = full.get("gross_m")
            bp_won       = bool(full.get("best_picture_won"))
            inf_mult     = SCORING["inflation_mult"].get(full.get("era", ""), 1)
            total_wae       += slot_score
            total_adj_gross += float(gross_m or 0) * inf_mult
            total_noms      += oscar_noms
            total_wins      += oscar_wins
            if top_wae is None or slot_score > top_wae:
                top_wae     = slot_score
                top_film_id = film_stub["id"]
            filled += 1
        else:
            oscar_wins = 0
            gross_m    = None
            bp_won     = False
            inf_mult   = 1

        slots_detail.append({
            "slot":       sl,
            "film":       film_stub,
            "oscar_wins": oscar_wins,
            "gross_m":    gross_m,
            "bp_won":     bp_won,
            "slot_score": round(slot_score, 1),
            "inflation_mult": inf_mult,
        })

    final_score = round(total_wae, 1)

    tier = SCORING["tiers"][-1]
    for t in SCORING["tiers"]:
        if final_score >= t["min"]:
            tier = t
            break

    return {
        "total_profit":    round(total_wae, 1),
        "oscar_bonus":     0.0,
        "top_film_id":     top_film_id,
        "final_score":     final_score,
        "filled":          filled,
        "slots":           slots_detail,
        "tier":            tier,
        "total_adj_gross": round(total_adj_gross, 1),
        "total_noms":      total_noms,
        "total_wins":      total_wins,
    }

# ── Template helpers ─────────────────────────────────────────────────────────
@app.template_filter("dollars")
def dollars_filter(value):
    if value is None:
        return "—"
    if abs(value) >= 999.5:  # rounds to $1,000M → show as $1.0B instead
        return f"${value / 1000:.1f}B"
    return f"${value:,.0f}M"

@app.template_filter("wae")
def wae_filter(value):
    """Format a WAE score: number is the centrepiece, WAE is subtle."""
    if value is None:
        return Markup("—")
    try:
        v = float(value)
    except (TypeError, ValueError):
        return Markup("—")
    if abs(v) >= 1000:
        num = f"${v / 1000:.1f}B"
    else:
        num = f"${v:,.0f}M"
    return Markup(f'{num}<span class="wae-unit">WAE</span>')

@app.context_processor
def inject_scoring():
    return {"scoring": SCORING}

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
    player = request.form.get("player_name", "").strip()
    mode   = request.form.get("mode", session.pop("pending_mode", "realwatcher"))
    save_state(fresh_state(player, mode))
    return redirect(url_for("game"))


@app.route("/game")
def game():
    s = state()
    if not s:
        return redirect(url_for("index"))
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
    last  = s.get("spin")
    if last:
        filtered = [c for c in valid if c != (last["era"], last["studio"])]
        valid    = filtered if filtered else valid
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
        LIMIT 30
    """
    params = [era, studio] + already

    rows = query(sql, params)
    pool = [film_row_to_dict(r, keep_gross=keep_gross) for r in rows]
    pool = [f for f in pool if eligible_slots_for_film(s, f)]

    print(f"  Spin: {era} x {studio} -> {len(pool)} eligible films")

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


@app.post("/game/respin")
def game_respin():
    s = state()
    if not s or s["phase"] != "draft":
        return jsonify({"error": "Not in draft phase"}), 400
    if s.get("respin_used"):
        return jsonify({"error": "Respin already used"}), 400

    mode       = s.get("mode", "realwatcher")
    keep_gross = mode == "classic"
    order_by   = "m.gross_m DESC" if mode == "classic" else "m.title ASC"
    already    = s["drafted_ids"]

    valid      = spinnable_combos(s)
    current    = (s["spin"]["era"], s["spin"]["studio"])
    options    = [c for c in valid if c != current] or valid
    era, studio = random.choice(options)

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
        LIMIT 30
    """
    pool = [film_row_to_dict(r, keep_gross=keep_gross) for r in query(sql, [era, studio] + already)]
    pool = [f for f in pool if eligible_slots_for_film(s, f)]

    s["spin"]        = {"era": era, "studio": studio}
    s["pool"]        = pool
    s["respin_used"] = True
    save_state(s)

    print(f"  Respin: {era} x {studio} -> {len(pool)} eligible films")

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
    if not slot_accepts(slot_obj, film, s.get("mode", "")):
        return jsonify({
            "error": f"'{film['title']}' doesn't qualify for the {slot_obj['label']} slot"
        }), 400

    # Compute WAE score for this draft (server-side only, uses private DB fields)
    full_film  = film_full(film_id)
    slot_score = compute_slot_score(full_film, slot_obj)

    # Commit the draft
    s["marquee"][str(slot_num)]       = film
    s["slot_scores"][str(slot_num)]   = slot_score
    s["running_total"]                = round(
        s.get("running_total", 0.0) + slot_score, 1
    )
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
        "ok":            True,
        "phase":         s["phase"],
        "round":         s["round"],
        "marquee":       s["marquee"],
        "slot_score":    slot_score,
        "running_total": s["running_total"],
    })


def _build_snapshot(result, player, mode):
    return {
        "player":          player,
        "mode":            mode,
        "grade":           result["tier"]["grade"],
        "headline":        result["tier"]["headline"],
        "final_score":     result["final_score"],
        "total_profit":    result["total_profit"],
        "oscar_bonus":     0,
        "filled":          result["filled"],
        "top_film_id":     result["top_film_id"],
        "total_adj_gross": result["total_adj_gross"],
        "total_noms":      result["total_noms"],
        "total_wins":      result["total_wins"],
        "slots": [
            {
                "slot_number":    e["slot"]["slot_number"],
                "label":          e["slot"]["label"],
                "icon":           e["slot"]["icon"],
                "slot_score":     e.get("slot_score", 0),
                "oscar_wins":     e.get("oscar_wins", 0),
                "bp_won":         e.get("bp_won", False),
                "gross_m":        e.get("gross_m"),
                "inflation_mult": e.get("inflation_mult", 1),
                "film": {
                    "id":         e["film"]["id"],
                    "title":      e["film"]["title"],
                    "year":       e["film"]["year"],
                    "poster_url": e["film"].get("poster_url"),
                } if e["film"] else None,
            }
            for e in result["slots"]
        ],
    }


@app.route("/game/score")
def score():
    s = state()
    if not s:
        return redirect(url_for("index"))
    result = compute_score(s)
    return render_template("score.html", s=s, result=result, slots=MARQUEE_SLOTS, scoring=SCORING)


def _encode_snapshot(snapshot: dict) -> str:
    raw = json.dumps(snapshot, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip('=')

def _decode_snapshot(data: str) -> dict:
    padded = data + '=' * (-len(data) % 4)
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(padded)))


@app.post("/game/share")
def game_share():
    s = state()
    if not s or s.get("phase") != "done":
        return {"error": "No active game"}, 400
    result   = compute_score(s)
    snapshot = _build_snapshot(result, s.get("player", ""), s.get("mode", "realwatcher"))
    encoded  = _encode_snapshot(snapshot)
    return {"url": request.host_url.rstrip("/") + f"/s/{encoded}"}


@app.post("/game/save")
def game_save():
    s = state()
    if not s or s.get("phase") != "done":
        return redirect(url_for("index"))
    if s.get("score_saved"):
        return redirect(url_for("score"))

    player = request.form.get("player_name", "").strip()
    if not player:
        return redirect(url_for("score"))

    result   = compute_score(s)
    snapshot = _build_snapshot(result, player, s.get("mode", "realwatcher"))
    token    = secrets.token_urlsafe(8)
    try:
        db = get_db()
        db.execute(
            """INSERT INTO scores
               (player, mode, final_score, grade, played_at, share_token, result_json)
               VALUES (?,?,?,?,?,?,?)""",
            (player, s.get("mode", "realwatcher"),
             result["final_score"], result["tier"]["grade"],
             datetime.now(timezone.utc).isoformat(),
             token, json.dumps(snapshot))
        )
        db.commit()
    except Exception as e:
        print(f"  Score save error: {e}")
    s["score_saved"] = True
    s["player"]      = player
    save_state(s)
    return redirect(url_for("score"))


@app.get("/s/<data>")
def shared_score(data):
    try:
        snapshot = _decode_snapshot(data)
    except Exception:
        return redirect(url_for("index"))
    return render_template("shared_score.html", snap=snapshot)


@app.get("/leaderboard")
def leaderboard():
    rw = query("""
        SELECT player, final_score, grade, played_at
        FROM scores WHERE mode = 'realwatcher' AND player != ''
        ORDER BY final_score DESC LIMIT 25
    """)
    classic = query("""
        SELECT player, final_score, grade, played_at
        FROM scores WHERE mode = 'classic' AND player != ''
        ORDER BY final_score DESC LIMIT 25
    """)
    return render_template("leaderboard.html",
                           rw_entries=[dict(r) for r in rw],
                           classic_entries=[dict(r) for r in classic])


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
