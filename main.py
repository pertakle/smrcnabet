from flask import Flask, render_template, request, redirect, url_for, g
import sqlite3
import os

app = Flask(__name__)

PORT = 5000
DOMAIN = "smrcnabet"
DATABASE = "betting.db"

# -------------------
# Database connection
# -------------------

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

# -------------------
# Initialize DB
# -------------------

def init_db():
    with app.app_context():
        db = get_db()
        with open("schema.sql") as f:
            db.executescript(f.read())

        with open("dummy_db.sql") as f:
            db.executescript(f.read())
        # db.execute("INSERT INTO opportunities (title, odds) VALUES (?, ?)",
                   # ("Team A vs Team B", 1.5))
        # db.execute("INSERT INTO opportunities (title, odds) VALUES (?, ?)",
                   # ("Coin Flip", 2.0))
        db.commit()

    # print_all()
    # exit()


def print_all():
    with app.app_context():
        db = get_db()
        players = db.execute("SELECT * FROM players")
        for player in players:
            print(player["id"], player["name"], player["elo"])

        teams = db.execute("SELECT id, name FROM teams")
        print("\nTEAMS")
        for team in teams:
            print(team["id"], team["name"])
            team_members = db.execute(
                """SELECT players.id, players.name
                FROM players
                INNER JOIN members
                ON members.player_id = players.id
                WHERE members.team_id = ?
                """,
                (team["id"],)
            )
            for member in team_members:
                print(member["players.id"], member["players.name"])


# -------------------
# Routes
# -------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form["username"].strip()
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

    user = db.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    opportunities = db.execute(
        "SELECT * FROM opportunities"
    ).fetchall()

    open_bets = db.execute("""
        SELECT bets.amount, bets.status, opportunities.title, opportunities.odds
        FROM bets
        JOIN opportunities ON bets.opportunity_id = opportunities.id
        WHERE bets.user_id = ? AND bets.status = 'open'
    """, (user["id"],)).fetchall()

    return render_template(
        "dashboard.html",
        user=user,
        opportunities=opportunities,
        open_bets=open_bets
    )


@app.route("/bet/<username>/<int:opp_id>", methods=["POST"])
def place_bet(username, opp_id):
    amount = float(request.form["amount"])
    db = get_db()

    user = db.execute("SELECT * FROM users WHERE username = ?",
                      (username,)).fetchone()

    if amount <= 0:
        return redirect(url_for("dashboard", username=username))

    if amount > user["balance"]:
        return redirect(url_for("dashboard", username=username))

    # Deduct balance
    new_balance = user["balance"] - amount

    db.execute("UPDATE users SET balance = ? WHERE id = ?",
               (new_balance, user["id"]))

    db.execute(
        "INSERT INTO bets (user_id, opportunity_id, amount) VALUES (?, ?, ?)",
        (user["id"], opp_id, amount)
    )

    db.commit()

    return redirect(url_for("dashboard", username=username))


if __name__ == "__main__":
    if not os.path.exists(DATABASE):
        init_db()
    # init_db()
    app.run(host="localhost", port=PORT, debug=True)
    # app.run(host="0.0.0.0", port=PORT, debug=True)
