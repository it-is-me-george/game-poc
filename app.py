import os
import sqlite3
import string
import random
import threading
import time
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, g, session

load_dotenv()

# ==================== НАСТРОЙКИ ====================
SPEND_OPTIONS = [int(x) for x in os.getenv("SPEND_OPTIONS", "5,10,25,50").split(",")]
SPEND_NAMED = [
    {"name": "Old office", "cost": 2},
    {"name": "Scada", "cost": 3},
    {"name": "New office", "cost": 5},
    {"name": "Response office", "cost": 7},
]
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Mutable runtime settings (admin can change via UI)
game_settings = {
    "points_per_tick": int(os.getenv("POINTS_PER_MINUTE", 100)),
    "tick_interval": int(os.getenv("TICK_INTERVAL", 60)),
}
# ===================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production-8f3k2j")
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


def generate_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            points INTEGER DEFAULT 0,
            code TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            cost INTEGER NOT NULL,
            label TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (team_id) REFERENCES teams(id)
        );
    """)
    # Миграция: добавить столбец code если его нет (для существующих БД)
    cursor = conn.execute("PRAGMA table_info(teams)")
    columns = [row[1] for row in cursor.fetchall()]
    if "code" not in columns:
        conn.execute("ALTER TABLE teams ADD COLUMN code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_code ON teams(code)")
    # Миграция: добавить столбец label в reports если его нет
    cursor2 = conn.execute("PRAGMA table_info(reports)")
    report_columns = [row[1] for row in cursor2.fetchall()]
    if "label" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN label TEXT")
    # Сгенерировать коды для команд, у которых их нет
    teams_without_code = conn.execute(
        "SELECT id FROM teams WHERE code IS NULL"
    ).fetchall()
    for (team_id,) in teams_without_code:
        while True:
            code = generate_code()
            existing = conn.execute(
                "SELECT 1 FROM teams WHERE code = ?", (code,)
            ).fetchone()
            if not existing:
                break
        conn.execute("UPDATE teams SET code = ? WHERE id = ?", (code, team_id))
    conn.commit()
    conn.close()


# ---- Декораторы авторизации ----

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "role" not in session:
            return jsonify(error="Необходима авторизация"), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify(error="Доступ запрещён"), 403
        return f(*args, **kwargs)
    return decorated


# ---- Фоновое начисление очков ----

def tick_points():
    while True:
        time.sleep(game_settings["tick_interval"])
        try:
            conn = sqlite3.connect(DATABASE)
            conn.execute(
                "UPDATE teams SET points = points + ?",
                (game_settings["points_per_tick"],),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Tick error: {e}")


# ---- Маршруты: Авторизация ----

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify(error="Введите код"), 400

    if code == ADMIN_PASSWORD:
        session["role"] = "admin"
        session.pop("team_id", None)
        return jsonify(ok=True, role="admin")

    db = get_db()
    team = db.execute(
        "SELECT id, name FROM teams WHERE code = ?", (code,)
    ).fetchone()
    if team:
        session["role"] = "user"
        session["team_id"] = team["id"]
        return jsonify(ok=True, role="user", team_id=team["id"], team_name=team["name"])

    return jsonify(error="Неверный код"), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/api/me")
def me():
    role = session.get("role")
    if not role:
        return jsonify(role=None)
    result = {"role": role}
    if role == "user":
        result["team_id"] = session.get("team_id")
        db = get_db()
        team = db.execute(
            "SELECT name FROM teams WHERE id = ?", (session["team_id"],)
        ).fetchone()
        if team:
            result["team_name"] = team["name"]
    return jsonify(result)


# ---- Маршруты: API ----

@app.route("/api/config")
def config():
    return jsonify(
        points_per_tick=game_settings["points_per_tick"],
        spend_options=SPEND_OPTIONS,
        spend_named=SPEND_NAMED,
        tick_interval=game_settings["tick_interval"],
    )


@app.route("/api/teams", methods=["GET"])
@login_required
def list_teams():
    db = get_db()
    if session["role"] == "admin":
        teams = db.execute(
            "SELECT id, name, points, code, created_at FROM teams ORDER BY name"
        ).fetchall()
    else:
        team_id = session.get("team_id")
        teams = db.execute(
            "SELECT id, name, points, created_at FROM teams WHERE id = ?",
            (team_id,),
        ).fetchall()
    return jsonify([dict(t) for t in teams])


@app.route("/api/teams", methods=["POST"])
@admin_required
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
    codes = {}
    for name in names:
        while True:
            code = generate_code()
            existing = db.execute(
                "SELECT 1 FROM teams WHERE code = ?", (code,)
            ).fetchone()
            if not existing:
                break
        try:
            db.execute(
                "INSERT INTO teams (name, code) VALUES (?, ?)", (name, code)
            )
            created.append(name)
            codes[name] = code
        except sqlite3.IntegrityError:
            duplicates.append(name)
    db.commit()
    return jsonify(ok=True, created=created, duplicates=duplicates, codes=codes), 201


@app.route("/api/teams/<int:team_id>/spend", methods=["POST"])
@login_required
def spend_points(team_id):
    # Users can only spend for their own team
    if session["role"] == "user" and session.get("team_id") != team_id:
        return jsonify(error="Доступ запрещён"), 403

    data = request.get_json(force=True)
    amount = data.get("amount")
    label = data.get("label", "")

    # Validate: admin uses SPEND_OPTIONS, user uses SPEND_NAMED
    valid_amounts = SPEND_OPTIONS + [s["cost"] for s in SPEND_NAMED]
    if amount not in valid_amounts:
        return jsonify(error="Недопустимая сумма списания"), 400

    db = get_db()
    team = db.execute("SELECT id, points FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        return jsonify(error="Команда не найдена"), 404
    if team["points"] < amount:
        return jsonify(error="Недостаточно очков"), 400

    db.execute("UPDATE teams SET points = points - ? WHERE id = ?", (amount, team_id))
    db.execute(
        "INSERT INTO reports (team_id, cost, status, label) VALUES (?, ?, 'pending', ?)",
        (team_id, amount, label or None),
    )
    db.commit()
    return jsonify(ok=True)


@app.route("/api/reports")
@login_required
def list_reports():
    db = get_db()
    if session["role"] == "admin":
        rows = db.execute("""
            SELECT r.id, t.name AS team_name, r.cost, r.label, r.status, r.created_at
            FROM reports r JOIN teams t ON t.id = r.team_id
            ORDER BY r.created_at DESC
            LIMIT 50
        """).fetchall()
    else:
        team_id = session.get("team_id")
        rows = db.execute("""
            SELECT r.id, t.name AS team_name, r.cost, r.label, r.status, r.created_at
            FROM reports r JOIN teams t ON t.id = r.team_id
            WHERE r.team_id = ?
            ORDER BY r.created_at DESC
            LIMIT 50
        """, (team_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ---- Админ: управление игрой ----

@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def update_settings():
    data = request.get_json(force=True)
    pts = data.get("points_per_tick")
    interval = data.get("tick_interval")
    if pts is not None:
        pts = int(pts)
        if pts < 0:
            return jsonify(error="Очки не могут быть отрицательными"), 400
        game_settings["points_per_tick"] = pts
    if interval is not None:
        interval = int(interval)
        if interval < 1:
            return jsonify(error="Интервал должен быть >= 1 секунды"), 400
        game_settings["tick_interval"] = interval
    return jsonify(ok=True, **game_settings)


@app.route("/api/admin/reset-points", methods=["POST"])
@admin_required
def reset_points():
    db = get_db()
    db.execute("UPDATE teams SET points = 0")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/admin/add-points", methods=["POST"])
@admin_required
def add_points_all():
    data = request.get_json(force=True)
    amount = int(data.get("amount", 0))
    if amount <= 0:
        return jsonify(error="Сумма должна быть > 0"), 400
    db = get_db()
    db.execute("UPDATE teams SET points = points + ?", (amount,))
    db.commit()
    return jsonify(ok=True)


# ---- Запуск ----

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=tick_points, daemon=True)
    t.start()
    print(f"Game started: +{game_settings['points_per_tick']} pts every {game_settings['tick_interval']}s")
    print(f"Spend options: {SPEND_OPTIONS}")
    print(f"Admin password: {ADMIN_PASSWORD}")
    app.run(host="0.0.0.0", debug=False, port=5000)
