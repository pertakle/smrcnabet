import os
import json
import itertools
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, g, flash, session

# --- Configuration -----------------------------------------------------------
# These are hardcoded for now but should be moved to env/config later.
PORT = 5000
DOMAIN = "smrcnabet"
DATABASE = "db/betting.db"
MARGIN = 0.05        # 5% house margin
ELO_K = 32
ELO_BASE = 1500

app = Flask(__name__)
app.secret_key = "smrcnabet-dev"


# --- Translations ------------------------------------------------------------

TRANSLATIONS_DIR = "translations"


def _flatten(d):
    """Recursively flatten a nested dict into a single level."""
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result.update(_flatten(v))
        else:
            result[k] = v
    return result


def load_translations():
    """Load all translation files from the translations/ directory.

    Each .json file must contain a "_language_name" key for display.
    Keys starting with "_" are treated as metadata and excluded from lookups.
    Translation values can be nested in arbitrary sub-dicts for readability;
    they are flattened into a single-level lookup at load time.

    Returns (languages_dict, available_languages_list).
    """
    languages = {}
    available = []
    if not os.path.isdir(TRANSLATIONS_DIR):
        languages["en"] = {}
        available.append({"code": "en", "name": "English", "flag": "🇬🇧"})
        return languages, available

    for filename in sorted(os.listdir(TRANSLATIONS_DIR)):
        if not filename.endswith(".json"):
            continue
        code = filename[:-5]
        filepath = os.path.join(TRANSLATIONS_DIR, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        name = data.pop("_language_name", code)
        flag = data.pop("_flag", "🌐")
        translations = _flatten({k: v for k, v in data.items() if not k.startswith("_")})
        languages[code] = translations
        available.append({"code": code, "name": name, "flag": flag})

    if not languages:
        languages["en"] = {}
        available.append({"code": "en", "name": "English", "flag": "🇬🇧"})

    return languages, available


LANGUAGES, AVAILABLE_LANGUAGES = load_translations()


def _translate(text, **kwargs):
    lang = session.get("lang", "en") or "en"
    translations = LANGUAGES.get(lang) or LANGUAGES.get("en", {})
    translated = translations.get(text, text)
    if kwargs:
        return translated.format(**kwargs)
    return translated


@app.context_processor
def inject_translations():
    lang = session.get("lang", "en") or "en"
    js_keys = {"Done", "Delete User", "Cancel Bet",
               "Delete this match and all related data?",
               "Delete mode active. Click the ✕ next to a user to permanently delete them and all their bets.",
               "Cancel mode active. Click the ✕ next to a bet to permanently delete it."}
    lang_data = LANGUAGES.get(lang, LANGUAGES.get("en", {}))
    js_translations = {k: v for k, v in lang_data.items() if k in js_keys}
    return dict(_=_translate, lang=lang, js_translations=js_translations,
                available_languages=AVAILABLE_LANGUAGES)


@app.route("/lang/<code>")
def set_lang(code):
    if any(l["code"] == code for l in AVAILABLE_LANGUAGES):
        session["lang"] = code
    referrer = request.referrer or url_for("index")
    return redirect(referrer)


# --- Database ----------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        with open("db/schema.sql") as f:
            db.executescript(f.read())
        with open("db/dummy_db.sql") as f:
            db.executescript(f.read())
        db.commit()


# --- SME (Simplified Multiplayer Elo) ----------------------------------------

def _expected(a, b):
    """Probability that rating *a* beats rating *b*."""
    return 1.0 / (1.0 + 10.0 ** ((b - a) / 400.0))


def compute_player_ratings(before_time=None):
    """SME ratings for every player, recomputed from the full match history.

    If *before_time* is given, only matches before that time are considered.
    """
    db = get_db()
    p = {row["id"]: {"name": row["name"], "rating": ELO_BASE}
         for row in db.execute("SELECT id, name FROM players").fetchall()}
    if not p:
        return p

    time_filter = "AND m.time < ?" if before_time else ""

    rows = db.execute(f"""
        SELECT m.id, m.time, r.team_id, r.position_name,
               GROUP_CONCAT(mp.player_id) AS mids
        FROM matches m
        JOIN results r        ON r.match_id = m.id
        JOIN playing_teams pt ON pt.match_id = m.id AND pt.team_id = r.team_id
        LEFT JOIN members mp  ON mp.team_id = r.team_id
        {time_filter}
        GROUP BY m.id, r.team_id
        ORDER BY m.time ASC
    """, (before_time,) if before_time else ()).fetchall()

    groups = {}
    for r in rows:
        groups.setdefault(r["id"], []).append({
            "team_id": r["team_id"],
            "position": r["position_name"],
            "mids": [int(x) for x in r["mids"].split(",")] if r["mids"] else [],
        })

    for match_id, teams in groups.items():
        try:
            positions = sorted({t["position"] for t in teams})
        except TypeError:
            continue
        rank_of = {pos: i for i, pos in enumerate(positions)}
        N = len(teams)
        if N < 2:
            continue

        for team in teams:
            actual = (N - rank_of[team["position"]] - 1) / (N - 1)
            for pid in team["mids"]:
                if pid not in p:
                    continue
                ex, cnt = 0.0, 0
                for other in teams:
                    for oid in other["mids"]:
                        if oid == pid or oid not in p:
                            continue
                        ex += _expected(p[pid]["rating"], p[oid]["rating"])
                        cnt += 1
                if cnt:
                    p[pid]["rating"] += ELO_K * (actual - ex / cnt)

    return p


# --- Team / course helpers ---------------------------------------------------

def team_ratings(player_ratings):
    """Return {team_id: rating} (average of team members' ratings)."""
    db = get_db()
    tr = {}
    for row in db.execute("""
        SELECT t.id, GROUP_CONCAT(mp.player_id) AS mids
        FROM teams t LEFT JOIN members mp ON mp.team_id = t.id
        GROUP BY t.id
    """).fetchall():
        mids = [int(x) for x in row["mids"].split(",")] if row["mids"] else []
        if mids:
            vals = [player_ratings[pid]["rating"]
                    for pid in mids if pid in player_ratings]
            tr[row["id"]] = sum(vals) / len(vals) if vals else ELO_BASE
        else:
            tr[row["id"]] = ELO_BASE
    return tr


def _win_probs(team_ids, ratings):
    """Exact Plackett–Luce probabilities for every team at every rank position.

    Returns {team_id: [prob_rank0, prob_rank1, …]} where rank 0 = best.
    """
    n = len(team_ids)
    lam = [10.0 ** (ratings[tid] / 400.0) for tid in team_ids]
    total = sum(lam)
    probs = {tid: [0.0] * n for tid in team_ids}

    for perm in itertools.permutations(range(n)):
        prob = 1.0
        rem = total
        for i, idx in enumerate(perm):
            prob *= lam[idx] / rem
            rem -= lam[idx]
        for rank_idx, idx in enumerate(perm):
            probs[team_ids[idx]][rank_idx] += prob

    return probs


def courses_for_match(match_id, tr):
    """Return list of {team_id, team_name, position_name, course}.

    *tr* is the dict from team_ratings().
    """
    db = get_db()
    playing = db.execute(
        "SELECT pt.team_id, t.name FROM playing_teams pt "
        "JOIN teams t ON t.id = pt.team_id WHERE pt.match_id = ?",
        (match_id,),
    ).fetchall()
    positions = [r["position_name"] for r in
                 db.execute("SELECT position_name FROM positions "
                            "WHERE match_id = ? ORDER BY position_name",
                            (match_id,)).fetchall()]
    if not playing or not positions:
        return []

    tids = [r["team_id"] for r in playing]
    names = {r["team_id"]: r["name"] for r in playing}
    rtgs = {tid: tr.get(tid, ELO_BASE) for tid in tids}

    probs = _win_probs(tids, rtgs)
    courses = []
    n_teams = len(tids)
    for tid in tids:
        for rank_idx, pos_name in enumerate(positions):
            if rank_idx >= n_teams:
                continue
            prob = probs[tid][rank_idx]
            course = 1.0 / (prob * (1.0 + MARGIN)) if prob > 0 else 9999.0
            courses.append({
                "team_id": tid,
                "team_name": names[tid],
                "position_name": pos_name,
                "course": round(course, 2),
            })
    return courses


# --- Derived data helpers ---------------------------------------------------

def course_at_time(match_id, team_id, position_name, bet_time,
                   precomputed_ratings=None, precomputed_team_ratings=None):
    """Course for a bet as it would have been at *bet_time*.

    Uses precomputed ratings when available (saves recomputing for the
    current-time case).
    """
    if precomputed_ratings is not None and precomputed_team_ratings is not None:
        # Try current ratings first (cheap)
        cs = courses_for_match(match_id, precomputed_team_ratings)
        for c in cs:
            if c["team_id"] == team_id and c["position_name"] == position_name:
                return c["course"]
    # Fall back to historical recomputation
    p = compute_player_ratings(before_time=bet_time)
    tr = team_ratings(p)
    cs = courses_for_match(match_id, tr)
    for c in cs:
        if c["team_id"] == team_id and c["position_name"] == position_name:
            return c["course"]
    return None


def _compute_user_balance(user_id):
    """Recompute a user's balance from scratch."""
    db = get_db()
    balance = 100
    total_bet = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM bets WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0]
    balance -= total_bet

    winning = db.execute("""
        SELECT b.amount, b.course
        FROM bets b
        JOIN results r ON r.match_id = b.match_id
                      AND r.team_id = b.team_id
                      AND r.position_name = b.position_name
        WHERE b.user_id = ?
    """, (user_id,)).fetchall()

    for bet in winning:
        balance += bet["amount"] * (bet["course"] if bet["course"] is not None else 0)

    return balance


# --- Routes ------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form["username"].strip()
        if not username:
            return render_template("index.html")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?",
                          (username,)).fetchone()
        if not user:
            db.execute("INSERT INTO users (username) VALUES (?)",
                       (username,))
            db.commit()
        return redirect(url_for("dashboard", username=username))
    return render_template("index.html")


@app.route("/dashboard/<username>")
def dashboard(username):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?",
                      (username,)).fetchone()
    if not user:
        return redirect(url_for("index"))

    p_ratings = compute_player_ratings()
    t_ratings = team_ratings(p_ratings)

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # Upcoming = matches without results, before match time
    upcoming_rows = db.execute(
        "SELECT * FROM matches "
        "WHERE id NOT IN (SELECT DISTINCT match_id FROM results) "
        "  AND time > ?",
        (now_str,),
    ).fetchall()
    upcoming = []
    for m in upcoming_rows:
        cs = courses_for_match(m["id"], t_ratings)
        pos_names = sorted({c["position_name"] for c in cs})

        seen = set()
        teams = []
        for c in cs:
            if c["team_id"] not in seen:
                seen.add(c["team_id"])
                rows = db.execute(
                    "SELECT p.name FROM members mp "
                    "JOIN players p ON p.id = mp.player_id "
                    "WHERE mp.team_id = ?",
                    (c["team_id"],),
                ).fetchall()
                teams.append({
                    "team_id": c["team_id"],
                    "team_name": c["team_name"],
                    "players": [r["name"] for r in rows],
                })

        upcoming.append({
            "match": m,
            "courses": cs,
            "teams": teams,
            "position_names": pos_names,
        })

    # Past = all matches past their time or with results
    past_rows = db.execute("""
        SELECT id, name, time FROM matches
        WHERE time <= ? OR id IN (SELECT DISTINCT match_id FROM results)
        ORDER BY time DESC
    """, (now_str,)).fetchall()
    past = []
    for m in past_rows:
        rs = db.execute("""
            SELECT r.*, t.name AS team_name
            FROM results r JOIN teams t ON t.id = r.team_id
            WHERE r.match_id = ?
        """, (m["id"],)).fetchall()
        has_results = bool(rs)
        entry = {"match": m, "results": rs, "has_results": has_results}
        if not has_results:
            cs = courses_for_match(m["id"], t_ratings)
            seen = set()
            teams = []
            for c in cs:
                if c["team_id"] not in seen:
                    seen.add(c["team_id"])
                    rows = db.execute(
                        "SELECT p.name FROM members mp "
                        "JOIN players p ON p.id = mp.player_id "
                        "WHERE mp.team_id = ?",
                        (c["team_id"],),
                    ).fetchall()
                    teams.append({
                        "team_id": c["team_id"],
                        "team_name": c["team_name"],
                        "players": [r["name"] for r in rows],
                    })
            entry["courses"] = cs
            entry["teams"] = teams
            entry["position_names"] = sorted({c["position_name"] for c in cs})
        past.append(entry)

    # Open bets = bets on matches without results
    open_bets = db.execute("""
        SELECT b.id, b.amount, b.time, b.course,
               b.match_id, b.team_id, b.position_name,
               m.name AS match_name, t.name AS team_name
        FROM bets b
        JOIN matches m ON m.id = b.match_id
        JOIN teams t   ON t.id = b.team_id
        WHERE b.user_id = ?
          AND m.id NOT IN (SELECT DISTINCT match_id FROM results)
    """, (user["id"],)).fetchall()

    # Computed balance
    computed_balance = _compute_user_balance(user["id"])

    # Leaderboard
    leaderboard = sorted(
        ({"name": p["name"], "rating": round(p["rating"], 1)}
         for p in p_ratings.values()),
        key=lambda x: x["rating"],
        reverse=True,
    )

    return render_template(
        "dashboard.html",
        user=user,
        computed_balance=computed_balance,
        upcoming=upcoming,
        past=past,
        open_bets=open_bets,
        leaderboard=leaderboard,
    )


@app.route("/bet/<username>", methods=["POST"])
def place_bet(username):
    selection = request.form.get("selection")
    amount = request.form.get("amount", type=float)

    if not selection or not amount or amount <= 0:
        return redirect(url_for("dashboard", username=username))

    try:
        parts = selection.split(":")
        team_id = int(parts[0])
        position_name = parts[1]
        match_id = int(parts[2])
    except (ValueError, IndexError):
        return redirect(url_for("dashboard", username=username))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?",
                      (username,)).fetchone()
    if not user:
        return redirect(url_for("index"))

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    match = db.execute(
        "SELECT * FROM matches WHERE id = ? AND time > ? "
        "AND id NOT IN (SELECT DISTINCT match_id FROM results)",
        (match_id, now_str),
    ).fetchone()
    if not match:
        return redirect(url_for("dashboard", username=username))

    balance = _compute_user_balance(user["id"])
    if amount > balance:
        return redirect(url_for("dashboard", username=username))

    p_ratings = compute_player_ratings()
    t_ratings = team_ratings(p_ratings)
    cs = courses_for_match(match_id, t_ratings)
    course = None
    for c in cs:
        if c["team_id"] == team_id and c["position_name"] == position_name:
            course = c["course"]
            break

    db.execute(
        "INSERT INTO bets (user_id, match_id, team_id, position_name, amount, course) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], match_id, team_id, position_name, amount, course),
    )
    db.commit()
    return redirect(url_for("dashboard", username=username))


# ============================================================
# All Bets
# ============================================================


def _allbets_data():
    """Shared data for the allbets page (balances, bet history, chart)."""
    db = get_db()

    all_users = []
    for u in db.execute("SELECT * FROM users ORDER BY username").fetchall():
        balance = _compute_user_balance(u["id"])
        pending = db.execute(
            "SELECT COALESCE(SUM(b.amount), 0) "
            "FROM bets b WHERE b.user_id = ? "
            "AND b.match_id NOT IN (SELECT DISTINCT match_id FROM results)",
            (u["id"],),
        ).fetchone()[0]
        all_users.append({"username": u["username"], "balance": balance, "balance_pending": balance + pending})

    all_bets_raw = db.execute("""
        SELECT b.id, b.amount, b.time, b.course,
               b.match_id, b.team_id, b.position_name,
               u.username AS bettor,
               m.name AS match_name, m.time AS match_time,
               t.name AS team_name,
               EXISTS (SELECT 1 FROM results WHERE match_id = b.match_id) AS settled,
               EXISTS (SELECT 1 FROM results
                       WHERE match_id = b.match_id AND team_id = b.team_id
                       AND position_name = b.position_name) AS won
        FROM bets b
        JOIN users u ON u.id = b.user_id
        JOIN matches m ON m.id = b.match_id
        JOIN teams t  ON t.id = b.team_id
        ORDER BY b.time DESC
    """).fetchall()

    bets = list(all_bets_raw)

    # --- Chart data ---
    chart_bets = db.execute("""
        SELECT b.id, b.user_id, b.amount, b.time AS bet_time, b.course,
               b.match_id, b.team_id, b.position_name,
               m.time AS match_time
        FROM bets b
        JOIN matches m ON m.id = b.match_id
        ORDER BY b.time
    """).fetchall()

    settled_matches = {
        row["match_id"]
        for row in db.execute("SELECT DISTINCT match_id FROM results").fetchall()
    }
    won_bets = {
        (row["match_id"], row["team_id"], row["position_name"])
        for row in db.execute("SELECT match_id, team_id, position_name FROM results").fetchall()
    }

    bet_courses = {bet["id"]: bet["course"] for bet in chart_bets}

    all_times = [bet["bet_time"] for bet in chart_bets] + \
                [bet["match_time"] for bet in chart_bets]
    if all_times:
        min_date = datetime.strptime(min(all_times)[:10], "%Y-%m-%d").date()
    else:
        min_date = date.today()
    max_date = date.today()

    chart_labels = []
    d = min_date
    while d <= max_date:
        chart_labels.append(d.isoformat())
        d += timedelta(days=1)

    raw_users = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    chart_datasets_balance = []
    chart_datasets_pending = []
    palette = ["#0d6efd", "#dc3545", "#198754", "#ffc107", "#6f42c1", "#fd7e14",
               "#20c997", "#e83e8c", "#6610f2", "#17a2b8"]
    for idx, u in enumerate(raw_users):
        uid = u["id"]
        user_bets = [b for b in chart_bets if b["user_id"] == uid]
        points_balance = []
        points_pending = []
        for ds in chart_labels:
            effective_balance = 100
            effective_pending = 100
            for b in user_bets:
                if b["bet_time"][:10] > ds:
                    continue
                effective_balance -= b["amount"]
                has_result = b["match_id"] in settled_matches
                if has_result and b["match_time"][:10] <= ds:
                    if (b["match_id"], b["team_id"], b["position_name"]) in won_bets:
                        c = bet_courses.get(b["id"], 1)
                        payout = b["amount"] * c
                        effective_balance += payout
                        effective_pending += payout
                    else:
                        effective_pending -= b["amount"]
                # pending: stake not subtracted from pending mode
            points_balance.append(round(effective_balance, 2))
            points_pending.append(round(effective_pending, 2))
        color = palette[idx % len(palette)]
        chart_datasets_balance.append({
            "label": u["username"],
            "data": points_balance,
            "borderColor": color,
            "backgroundColor": color + "22",
            "tension": 0.3,
        })
        chart_datasets_pending.append({
            "label": u["username"],
            "data": points_pending,
            "borderColor": color,
            "backgroundColor": color + "22",
            "tension": 0.3,
        })

    return {
        "all_users": all_users,
        "all_bets": bets,
        "chart_labels": chart_labels,
        "chart_datasets_balance": chart_datasets_balance,
        "chart_datasets_pending": chart_datasets_pending,
    }


@app.route("/allbets")
def allbets_public():
    return render_template("allbets.html", user=None, **_allbets_data())


@app.route("/allbets/<username>")
def allbets(username):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?",
                      (username,)).fetchone()
    if not user:
        return redirect(url_for("index"))
    return render_template("allbets.html", user=user, **_allbets_data())



# ============================================================
# Admin
# ============================================================

def _admin_context(match_sort="time", match_order="desc", match_status="",
                   match_team=None, match_player=None, active_tab="players",
                   bet_user=None, bet_status="",
                   bet_sort="time", bet_order="desc"):
    db = get_db()

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # --- Match filtering ---
    where = []
    params = []
    if match_status == "upcoming":
        where.append("m.id NOT IN (SELECT DISTINCT match_id FROM results) AND m.time > ?")
        params.append(now_str)
    elif match_status == "pending":
        where.append("m.id NOT IN (SELECT DISTINCT match_id FROM results) AND m.time <= ?")
        params.append(now_str)
    elif match_status == "finished":
        where.append("m.id IN (SELECT DISTINCT match_id FROM results)")
    if match_team:
        where.append("m.id IN (SELECT match_id FROM playing_teams WHERE team_id = ?)")
        params.append(match_team)
    if match_player:
        where.append(
            "m.id IN (SELECT pt.match_id FROM playing_teams pt "
            "JOIN members mp ON mp.team_id = pt.team_id WHERE mp.player_id = ?)"
        )
        params.append(match_player)

    order_col = {"time": "m.time", "name": "m.name"}.get(match_sort, "m.time")
    order_dir = "ASC" if match_order == "asc" else "DESC"
    where_sql = " AND ".join(where) if where else "1=1"

    matches = db.execute(
        f"SELECT * FROM matches m WHERE {where_sql} ORDER BY {order_col} {order_dir}",
        params,
    ).fetchall()
    match_details = []
    for m in matches:
        match_details.append({
            "match": m,
            "teams": db.execute(
                "SELECT pt.team_id, t.name, GROUP_CONCAT(p.name, ', ') AS members "
                "FROM playing_teams pt JOIN teams t ON t.id = pt.team_id "
                "LEFT JOIN members mp ON mp.team_id = t.id "
                "LEFT JOIN players p ON p.id = mp.player_id "
                "WHERE pt.match_id = ? GROUP BY pt.team_id",
                (m["id"],),
            ).fetchall(),
            "positions": db.execute(
                "SELECT position_name FROM positions WHERE match_id = ? ORDER BY position_name",
                (m["id"],),
            ).fetchall(),
            "results": db.execute(
                "SELECT r.*, t.name AS team_name FROM results r "
                "JOIN teams t ON t.id = r.team_id WHERE r.match_id = ?",
                (m["id"],),
            ).fetchall(),
            "has_results": bool(
                db.execute("SELECT 1 FROM results WHERE match_id = ?", (m["id"],)).fetchone()
            ),
            "has_passed": m["time"] <= now_str,
        })

    # --- Players & Teams ---
    all_players = db.execute(
        "SELECT p.id, p.name, GROUP_CONCAT(t.name, ', ') AS teams "
        "FROM players p LEFT JOIN members mp ON mp.player_id = p.id "
        "LEFT JOIN teams t ON t.id = mp.team_id GROUP BY p.id ORDER BY p.name"
    ).fetchall()

    team_member_ids = {}
    for row in db.execute("SELECT team_id, player_id FROM members").fetchall():
        team_member_ids.setdefault(row["team_id"], []).append(row["player_id"])

    teams = db.execute(
        "SELECT t.id, t.name, GROUP_CONCAT(p.name, ', ') AS members "
        "FROM teams t LEFT JOIN members mp ON mp.team_id = t.id "
        "LEFT JOIN players p ON p.id = mp.player_id "
        "GROUP BY t.id ORDER BY t.name"
    ).fetchall()

    # --- Users ---
    users_raw = db.execute(
        "SELECT u.id, u.username, "
        "(SELECT COUNT(*) FROM bets WHERE user_id = u.id) AS bet_count "
        "FROM users u ORDER BY u.username"
    ).fetchall()
    users = []
    for u in users_raw:
        users.append({
            "id": u["id"],
            "username": u["username"],
            "balance": _compute_user_balance(u["id"]),
            "bet_count": u["bet_count"],
        })

    # --- All bets (with filters) ---
    bet_where = []
    bet_params = []
    if bet_user:
        bet_where.append("b.user_id = ?")
        bet_params.append(bet_user)
    if bet_status == "open":
        bet_where.append("b.match_id NOT IN (SELECT DISTINCT match_id FROM results)")
    elif bet_status == "won":
        bet_where.append(
            "b.match_id IN (SELECT match_id FROM results) "
            "AND EXISTS (SELECT 1 FROM results r2 "
            "WHERE r2.match_id = b.match_id AND r2.team_id = b.team_id "
            "AND r2.position_name = b.position_name)"
        )
    elif bet_status == "lost":
        bet_where.append(
            "b.match_id IN (SELECT DISTINCT match_id FROM results) "
            "AND NOT EXISTS (SELECT 1 FROM results r2 "
            "WHERE r2.match_id = b.match_id AND r2.team_id = b.team_id "
            "AND r2.position_name = b.position_name)"
        )
    bet_where_sql = " AND ".join(bet_where) if bet_where else "1=1"

    bet_order_col = {"time": "b.time", "amount": "b.amount", "course": "b.course", "user": "u.username", "match": "m.name"}.get(bet_sort, "b.time")
    bet_order_dir = "ASC" if bet_order == "asc" else "DESC"

    all_bets = db.execute(f"""
        SELECT b.id, b.amount, b.time, b.course, b.match_id, b.team_id, b.position_name,
               u.id AS user_id, u.username,
               m.name AS match_name, m.time AS match_time,
               t.name AS team_name,
               EXISTS (SELECT 1 FROM results WHERE match_id = b.match_id) AS settled,
               EXISTS (SELECT 1 FROM results r2
                       WHERE r2.match_id = b.match_id AND r2.team_id = b.team_id
                       AND r2.position_name = b.position_name) AS won
        FROM bets b
        JOIN users u ON u.id = b.user_id
        JOIN matches m ON m.id = b.match_id
        JOIN teams t ON t.id = b.team_id
        WHERE {bet_where_sql}
        ORDER BY {bet_order_col} {bet_order_dir}
    """, bet_params).fetchall()

    open_bets = [b for b in all_bets if not b["settled"]]

    total_matches = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    return {
        "matches": match_details,
        "total_matches": total_matches,
        "teams": teams,
        "team_member_ids": team_member_ids,
        "players": all_players,
        "users": users,
        "open_bets": open_bets,
        "all_bets": all_bets,
        "filter_sort": match_sort,
        "filter_order": match_order,
        "filter_status": match_status,
        "filter_team": match_team,
        "filter_player": match_player,
        "filter_bet_user": bet_user,
        "filter_bet_status": bet_status,
        "filter_bet_sort": bet_sort,
        "filter_bet_order": bet_order,
        "active_tab": active_tab,
    }


@app.route("/admin")
def admin():
    active_tab = request.args.get("tab", "players")
    if active_tab not in ("players", "matches", "users"):
        active_tab = "players"

    sort_by = request.args.get("sort_by", "")
    if sort_by and "|" in sort_by:
        sort, order = sort_by.split("|", 1)
    else:
        sort = request.args.get("sort", "time")
        order = request.args.get("order", "desc")
    status = request.args.get("status", "")
    team = request.args.get("team", type=int)
    player = request.args.get("player", type=int)
    bet_user = request.args.get("bet_user", type=int)
    bet_status = request.args.get("bet_status", "")
    bet_sort_by = request.args.get("bet_sort_by", "")
    if bet_sort_by and "|" in bet_sort_by:
        bet_sort, bet_order = bet_sort_by.split("|", 1)
    else:
        bet_sort = request.args.get("bet_sort", "time")
        bet_order = request.args.get("bet_order", "desc")

    if sort not in ("time", "name"):
        sort = "time"
    if order not in ("asc", "desc"):
        order = "desc"
    if status not in ("", "upcoming", "pending", "finished"):
        status = ""
    if bet_status not in ("", "open", "won", "lost"):
        bet_status = ""
    if bet_sort not in ("time", "amount", "course", "user", "match"):
        bet_sort = "time"
    if bet_order not in ("asc", "desc"):
        bet_order = "desc"

    return render_template(
        "admin.html",
        **_admin_context(sort, order, status, team, player, active_tab, bet_user, bet_status, bet_sort, bet_order),
        sql_result=None,
        sql_error=None,
    )


@app.route("/admin/sql", methods=["POST"])
def admin_sql():
    query = request.form.get("query", "").strip()
    if not query:
        return redirect(url_for("admin"))

    db = get_db()
    result = None
    error = None
    try:
        cur = db.execute(query)
        if query.strip().upper().startswith("SELECT"):
            rows = cur.fetchall()
            result = [dict(r) for r in rows] if rows else []
        elif query.strip().upper().startswith("PRAGMA"):
            rows = cur.fetchall()
            result = [dict(r) for r in rows] if rows else []
        else:
            db.commit()
            result = f"{cur.rowcount} row(s) affected"
    except Exception as e:
        error = str(e)

    return render_template(
        "admin.html",
        **_admin_context(active_tab="matches"),
        sql_result=result,
        sql_error=error,
    )


@app.route("/admin/match/create", methods=["POST"])
def admin_create_match():
    name = request.form.get("name", "").strip()
    match_date = request.form.get("match_date", "").strip()
    match_time = request.form.get("match_time", "").strip()
    team_ids = request.form.getlist("team_ids")

    if not name:
        flash(_translate("Match name is required"), "warning")
        return redirect(url_for("admin", tab="matches"))

    if match_date:
        time = f"{match_date}T{match_time or '06:00'}"
    else:
        tomorrow = date.today() + timedelta(days=1)
        time = f"{tomorrow}T06:00"

    db = get_db()
    cur = db.execute("INSERT INTO matches (name, time) VALUES (?, ?)", (name, time))
    match_id = cur.lastrowid

    num_teams = len(team_ids)
    for tid in team_ids:
        try:
            db.execute("INSERT INTO playing_teams (match_id, team_id) VALUES (?, ?)",
                       (match_id, int(tid)))
        except (ValueError, sqlite3.IntegrityError):
            pass

    for i in range(1, num_teams + 1):
        try:
            db.execute("INSERT INTO positions (match_id, position_name) VALUES (?, ?)",
                       (match_id, str(i)))
        except sqlite3.IntegrityError:
            pass

    db.commit()
    flash(_translate("Match {name} created with {num_teams} team(s)", name=name, num_teams=num_teams))
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/add_team", methods=["POST"])
def admin_add_team(mid):
    team_id = request.form.get("team_id", type=int)
    if team_id:
        db = get_db()
        try:
            db.execute("INSERT INTO playing_teams (match_id, team_id) VALUES (?, ?)", (mid, team_id))
            db.commit()
        except sqlite3.IntegrityError:
            flash(_translate("Team already in match"), "warning")
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/remove_team", methods=["POST"])
def admin_remove_team(mid):
    team_id = request.form.get("team_id", type=int)
    if team_id:
        db = get_db()
        db.execute("DELETE FROM playing_teams WHERE match_id = ? AND team_id = ?", (mid, team_id))
        db.execute("DELETE FROM results WHERE match_id = ? AND team_id = ?", (mid, team_id))
        db.commit()
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/add_position", methods=["POST"])
def admin_add_position(mid):
    position_name = request.form.get("position_name", "").strip()
    if position_name:
        db = get_db()
        try:
            db.execute("INSERT INTO positions (match_id, position_name) VALUES (?, ?)", (mid, position_name))
            db.commit()
        except sqlite3.IntegrityError:
            flash(_translate("Position already exists"), "warning")
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/remove_position", methods=["POST"])
def admin_remove_position(mid):
    position_name = request.form.get("position_name", "").strip()
    if position_name:
        db = get_db()
        db.execute("DELETE FROM positions WHERE match_id = ? AND position_name = ?", (mid, position_name))
        db.execute("DELETE FROM results WHERE match_id = ? AND position_name = ?", (mid, position_name))
        db.commit()
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/edit_time", methods=["POST"])
def admin_edit_match_time(mid):
    match_date = request.form.get("match_date", "").strip()
    match_time = request.form.get("match_time", "").strip()
    if match_date:
        db = get_db()
        new_time = f"{match_date}T{match_time or '06:00'}"
        db.execute("UPDATE matches SET time = ? WHERE id = ?", (new_time, mid))
        db.commit()
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/delete", methods=["POST"])
def admin_delete_match(mid):
    db = get_db()
    db.execute("DELETE FROM bets WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM results WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM positions WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM playing_teams WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM matches WHERE id = ?", (mid,))
    db.commit()
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/set_all_results", methods=["POST"])
def admin_set_all_results(mid):
    db = get_db()

    # Collect submitted positions
    submitted = {}
    for key, val in request.form.items():
        if key.startswith("pos_"):
            try:
                tid = int(key.split("_", 1)[1])
            except (ValueError, IndexError):
                continue
            val = val.strip()
            if val:
                submitted[tid] = val

    # Validate: every team must have a position, OR none may have one (clear)
    teams_in_match = {r["team_id"] for r in
                      db.execute("SELECT team_id FROM playing_teams WHERE match_id = ?",
                                 (mid,)).fetchall()}
    missing = teams_in_match - set(submitted.keys())

    if not submitted:
        # All blank → clear results
        db.execute("DELETE FROM results WHERE match_id = ?", (mid,))
        db.commit()
        return redirect(url_for("admin", tab="matches"))

    if missing:
        missing_str = ' #'.join(str(m) for m in sorted(missing))
        flash(_translate("Either give every team a position, or leave all blank to clear. Missing: #{teams}", teams=missing_str), "warning")
        return redirect(url_for("admin", tab="matches"))

    db.execute("DELETE FROM results WHERE match_id = ?", (mid,))
    for tid, pos_name in submitted.items():
        db.execute(
            "INSERT INTO results (match_id, team_id, position_name) VALUES (?, ?, ?)",
            (mid, tid, pos_name),
        )
    db.commit()
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/match/<int:mid>/clear_results", methods=["POST"])
def admin_clear_results(mid):
    db = get_db()
    db.execute("DELETE FROM results WHERE match_id = ?", (mid,))
    db.commit()
    return redirect(url_for("admin", tab="matches"))


@app.route("/admin/team/create", methods=["POST"])
def admin_create_team():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO teams (name) VALUES (?)", (name,))
        db.commit()
    return redirect(url_for("admin", tab="players"))


@app.route("/admin/player/create", methods=["POST"])
def admin_create_player():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO players (name) VALUES (?)", (name,))
        db.commit()
    return redirect(url_for("admin", tab="players"))


@app.route("/admin/membership/add", methods=["POST"])
def admin_add_membership():
    player_id = request.form.get("player_id", type=int)
    team_id = request.form.get("team_id", type=int)
    if player_id and team_id:
        db = get_db()
        try:
            db.execute("INSERT INTO members (player_id, team_id) VALUES (?, ?)",
                       (player_id, team_id))
            db.commit()
        except sqlite3.IntegrityError:
            flash(_translate("Already a member"), "warning")
    return redirect(url_for("admin", tab="players"))


@app.route("/admin/membership/remove", methods=["POST"])
def admin_remove_membership():
    player_id = request.form.get("player_id", type=int)
    team_id = request.form.get("team_id", type=int)
    if player_id and team_id:
        db = get_db()
        db.execute("DELETE FROM members WHERE player_id = ? AND team_id = ?",
                   (player_id, team_id))
        db.commit()
    return redirect(url_for("admin", tab="players"))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM bets WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(_translate("User #{user_id} and all their bets deleted", user_id=user_id))
    return redirect(url_for("admin", tab="users"))


@app.route("/admin/bet/<int:bet_id>/cancel", methods=["POST"])
def admin_cancel_bet(bet_id):
    db = get_db()
    db.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    db.commit()
    flash(_translate("Bet #{bet_id} cancelled", bet_id=bet_id))
    return redirect(url_for("admin", tab="users"))


# --- Bulk & convenience admin routes ----------------------------------------

@app.route("/admin/players/bulk", methods=["POST"])
def admin_bulk_players():
    lines = [l.strip() for l in request.form.get("names", "").split("\n") if l.strip()]
    if not lines:
        return redirect(url_for("admin", tab="players"))
    db = get_db()
    for name in lines:
        db.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (name,))
    db.commit()
    flash(_translate("Created {n} player(s)", n=len(lines)))
    return redirect(url_for("admin", tab="players"))


@app.route("/admin/team/create-full", methods=["POST"])
def admin_create_team_full():
    name = request.form.get("name", "").strip()
    player_ids = request.form.getlist("player_ids")
    if not name:
        return redirect(url_for("admin", tab="players"))
    db = get_db()
    cur = db.execute("INSERT INTO teams (name) VALUES (?)", (name,))
    team_id = cur.lastrowid
    for pid in player_ids:
        try:
            db.execute("INSERT INTO members (player_id, team_id) VALUES (?, ?)",
                       (int(pid), team_id))
        except (ValueError, sqlite3.IntegrityError):
            pass
    db.commit()
    flash(_translate("Team {name} created with {n} member(s)", name=name, n=len(player_ids)))
    return redirect(url_for("admin", tab="players"))


@app.route("/admin/team/<int:team_id>/members", methods=["POST"])
def admin_team_members(team_id):
    player_ids = request.form.getlist("player_ids")
    db = get_db()
    db.execute("DELETE FROM members WHERE team_id = ?", (team_id,))
    for pid in player_ids:
        try:
            db.execute("INSERT INTO members (player_id, team_id) VALUES (?, ?)",
                       (int(pid), team_id))
        except (ValueError, sqlite3.IntegrityError):
            pass
    db.commit()
    return redirect(url_for("admin", tab="players"))


if __name__ == "__main__":
    if not os.path.exists(DATABASE):
        init_db()
    app.run(host="0.0.0.0", port=PORT, debug=True)
