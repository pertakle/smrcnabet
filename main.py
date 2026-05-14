import os
import itertools
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, g, flash

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
        SELECT b.amount, b.match_id, b.team_id, b.position_name, b.time
        FROM bets b
        JOIN results r ON r.match_id = b.match_id
                      AND r.team_id = b.team_id
                      AND r.position_name = b.position_name
        WHERE b.user_id = ?
    """, (user_id,)).fetchall()

    for bet in winning:
        course = course_at_time(
            bet["match_id"], bet["team_id"], bet["position_name"], bet["time"],
        )
        balance += bet["amount"] * (course if course is not None else 0)

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

    # Upcoming = matches without results yet
    upcoming_rows = db.execute(
        "SELECT * FROM matches "
        "WHERE id NOT IN (SELECT DISTINCT match_id FROM results)"
    ).fetchall()
    upcoming = []
    for m in upcoming_rows:
        cs = courses_for_match(m["id"], t_ratings)
        pos_names = sorted({c["position_name"] for c in cs})
        upcoming.append({
            "match": m,
            "courses": cs,
            "position_names": pos_names,
        })

    # Past = matches that DO have results
    past_rows = db.execute("""
        SELECT DISTINCT m.id, m.name, m.time
        FROM matches m JOIN results r ON r.match_id = m.id
        ORDER BY m.time DESC
    """).fetchall()
    past = []
    for m in past_rows:
        rs = db.execute("""
            SELECT r.*, t.name AS team_name
            FROM results r JOIN teams t ON t.id = r.team_id
            WHERE r.match_id = ?
        """, (m["id"],)).fetchall()
        past.append({"match": m, "results": rs})

    # Open bets = bets on matches without results
    open_bets = db.execute("""
        SELECT b.id, b.amount, b.time,
               b.match_id, b.team_id, b.position_name,
               m.name AS match_name, t.name AS team_name
        FROM bets b
        JOIN matches m ON m.id = b.match_id
        JOIN teams t   ON t.id = b.team_id
        WHERE b.user_id = ?
          AND m.id NOT IN (SELECT DISTINCT match_id FROM results)
    """, (user["id"],)).fetchall()

    # Compute course for each open bet from historical ratings
    open_bets_with_course = []
    for bet in open_bets:
        course = course_at_time(
            bet["match_id"], bet["team_id"], bet["position_name"], bet["time"],
            p_ratings, t_ratings,
        )
        open_bets_with_course.append({**bet, "course": course})

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
        open_bets=open_bets_with_course,
        leaderboard=leaderboard,
    )


@app.route("/bet/<username>", methods=["POST"])
def place_bet(username):
    match_id = request.form.get("match_id", type=int)
    selection = request.form.get("selection")
    amount = request.form.get("amount", type=float)

    if not all([match_id, selection, amount]) or amount <= 0:
        return redirect(url_for("dashboard", username=username))

    try:
        team_id_str, position_name = selection.split(":", 1)
        team_id = int(team_id_str)
    except (ValueError, TypeError):
        return redirect(url_for("dashboard", username=username))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?",
                      (username,)).fetchone()
    if not user:
        return redirect(url_for("index"))

    balance = _compute_user_balance(user["id"])
    if amount > balance:
        return redirect(url_for("dashboard", username=username))

    db.execute(
        "INSERT INTO bets (user_id, match_id, team_id, position_name, amount) "
        "VALUES (?, ?, ?, ?, ?)",
        (user["id"], match_id, team_id, position_name, amount),
    )
    db.commit()
    return redirect(url_for("dashboard", username=username))


# ============================================================
# Admin
# ============================================================

def _admin_context(match_sort="time", match_order="desc", match_status="",
                   match_team=None, match_player=None, active_tab="players"):
    db = get_db()

    # --- Match filtering ---
    where = []
    params = []
    if match_status == "upcoming":
        where.append("m.id NOT IN (SELECT DISTINCT match_id FROM results)")
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

    # --- Open bets (bets on matches without results) ---
    open_raw = db.execute(
        "SELECT b.id, b.amount, b.time, b.match_id, b.team_id, b.position_name, "
        "u.username, m.name AS match_name, t.name AS team_name "
        "FROM bets b JOIN users u ON u.id = b.user_id "
        "JOIN matches m ON m.id = b.match_id "
        "JOIN teams t ON t.id = b.team_id "
        "WHERE m.id NOT IN (SELECT DISTINCT match_id FROM results)"
    ).fetchall()
    open_bets = []
    for b in open_raw:
        course = course_at_time(b["match_id"], b["team_id"], b["position_name"], b["time"])
        open_bets.append({**b, "course": course})

    total_matches = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    return {
        "matches": match_details,
        "total_matches": total_matches,
        "teams": teams,
        "team_member_ids": team_member_ids,
        "players": all_players,
        "users": users,
        "open_bets": open_bets,
        "filter_sort": match_sort,
        "filter_order": match_order,
        "filter_status": match_status,
        "filter_team": match_team,
        "filter_player": match_player,
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

    if sort not in ("time", "name"):
        sort = "time"
    if order not in ("asc", "desc"):
        order = "desc"
    if status not in ("", "upcoming", "finished"):
        status = ""

    return render_template(
        "admin.html",
        **_admin_context(sort, order, status, team, player, active_tab),
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
        flash("Match name is required", "warning")
        return redirect(url_for("admin", tab="matches"))

    from datetime import datetime
    if match_date:
        time = f"{match_date}T{match_time or '20:00'}"
    else:
        time = datetime.now().strftime("%Y-%m-%dT%H:%M")

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
    flash(f"Match '{name}' created with {num_teams} team(s)")
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
            flash("Team already in match", "warning")
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
            flash("Position already exists", "warning")
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


@app.route("/admin/match/<int:mid>/delete", methods=["POST"])
def admin_delete_match(mid):
    db = get_db()
    db.execute("DELETE FROM results WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM positions WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM playing_teams WHERE match_id = ?", (mid,))
    db.execute("DELETE FROM bets WHERE match_id = ?", (mid,))
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
        flash(f"Either give every team a position, or leave all blank to clear. Missing: #{' #'.join(str(m) for m in sorted(missing))}", "warning")
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
            flash("Already a member", "warning")
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
    flash(f"Created {len(lines)} player(s)")
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
    flash(f"Team '{name}' created with {len(player_ids)} member(s)")
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
