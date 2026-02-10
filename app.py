import sqlite3
import threading
import time
from flask import Flask, jsonify, request, render_template, g

# ==================== НАСТРОЙКИ ====================
POINTS_PER_MINUTE = 100        # Очков за тик
SPEND_OPTIONS = [5, 10, 25, 50]  # Варианты списания
TICK_INTERVAL = 60            # Интервал начисления (секунды)
# ===================================================

app = Flask(__name__)
DATABASE = "game.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            points INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            cost INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (team_id) REFERENCES teams(id)
        );
    """)
    conn.close()


# ---- Фоновое начисление очков ----

def tick_points():
    while True:
        time.sleep(TICK_INTERVAL)
        try:
            conn = sqlite3.connect(DATABASE)
            conn.execute(
                "UPDATE teams SET points = points + ?", (POINTS_PER_MINUTE,)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Tick error: {e}")


# ---- Маршруты ----

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def config():
    return jsonify(
        points_per_minute=POINTS_PER_MINUTE,
        spend_options=SPEND_OPTIONS,
        tick_interval=TICK_INTERVAL,
    )


@app.route("/api/teams", methods=["GET"])
def list_teams():
    db = get_db()
    teams = db.execute(
        "SELECT id, name, points, created_at FROM teams ORDER BY points DESC"
    ).fetchall()
    return jsonify([dict(t) for t in teams])


@app.route("/api/teams", methods=["POST"])
def create_team():
    data = request.get_json(force=True)
    names = data.get("names") or []
    if isinstance(data.get("name"), str):
        names.append(data["name"])
    names = list(dict.fromkeys(n for raw in names if (n := raw.strip())))
    if not names:
        return jsonify(error="Имя команды не может быть пустым"), 400
    db = get_db()
    created = []
    duplicates = []
    for name in names:
        try:
            db.execute("INSERT INTO teams (name) VALUES (?)", (name,))
            created.append(name)
        except sqlite3.IntegrityError:
            duplicates.append(name)
    db.commit()
    return jsonify(ok=True, created=created, duplicates=duplicates), 201


@app.route("/api/teams/<int:team_id>/spend", methods=["POST"])
def spend_points(team_id):
    data = request.get_json(force=True)
    amount = data.get("amount")
    if amount not in SPEND_OPTIONS:
        return jsonify(error="Недопустимая сумма списания"), 400

    db = get_db()
    team = db.execute("SELECT id, points FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        return jsonify(error="Команда не найдена"), 404
    if team["points"] < amount:
        return jsonify(error="Недостаточно очков"), 400

    db.execute("UPDATE teams SET points = points - ? WHERE id = ?", (amount, team_id))
    db.execute(
        "INSERT INTO reports (team_id, cost, status) VALUES (?, ?, 'pending')",
        (team_id, amount),
    )
    db.commit()
    return jsonify(ok=True)


@app.route("/api/reports")
def list_reports():
    db = get_db()
    rows = db.execute("""
        SELECT r.id, t.name AS team_name, r.cost, r.status, r.created_at
        FROM reports r JOIN teams t ON t.id = r.team_id
        ORDER BY r.created_at DESC
        LIMIT 50
    """).fetchall()
    return jsonify([dict(r) for r in rows])


# ---- Запуск ----

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=tick_points, daemon=True)
    t.start()
    print(f"Game started: +{POINTS_PER_MINUTE} pts every {TICK_INTERVAL}s")
    print(f"Spend options: {SPEND_OPTIONS}")
    app.run(host="0.0.0.0", debug=False, port=5000)
