import os
import sqlite3
import string
import random
import threading
import time
import uuid
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

# Default settings (used only for initial DB seed)
DEFAULT_POINTS_PER_TICK = int(os.getenv("POINTS_PER_MINUTE", 100))
DEFAULT_TICK_INTERVAL = int(os.getenv("TICK_INTERVAL", 60))
# ===================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production-8f3k2j")
DATABASE = "game.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
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
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
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
            points_before INTEGER,
            points_after INTEGER,
            label TEXT,
            uuid TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (team_id) REFERENCES teams(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    # Seed default settings if not present
    existing = conn.execute("SELECT key FROM settings").fetchall()
    existing_keys = {row[0] for row in existing}
    if "points_per_tick" not in existing_keys:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                     ("points_per_tick", str(DEFAULT_POINTS_PER_TICK)))
    if "tick_interval" not in existing_keys:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                     ("tick_interval", str(DEFAULT_TICK_INTERVAL)))
    # Миграция: добавить столбец code если его нет (для существующих БД)
    cursor = conn.execute("PRAGMA table_info(teams)")
    columns = [row[1] for row in cursor.fetchall()]
    if "code" not in columns:
        conn.execute("ALTER TABLE teams ADD COLUMN code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_code ON teams(code)")
    # Миграция: добавить столбцы в reports если их нет
    report_columns = [row[1] for row in conn.execute("PRAGMA table_info(reports)").fetchall()]
    if "label" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN label TEXT")
    if "uuid" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN uuid TEXT")
    if "points_before" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN points_before INTEGER")
    if "points_after" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN points_after INTEGER")
    if "type" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN type TEXT")
        conn.execute("UPDATE reports SET type = CASE WHEN cost < 0 THEN 'начисление' ELSE 'списание' END")
    if "checked" not in report_columns:
        conn.execute("ALTER TABLE reports ADD COLUMN checked INTEGER DEFAULT 0")
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


def get_settings(conn=None):
    """Read game settings from DB. Accepts either a raw connection or uses Flask g.db."""
    if conn is None:
        conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    s = {row[0]: row[1] for row in rows}
    return {
        "points_per_tick": int(s.get("points_per_tick", DEFAULT_POINTS_PER_TICK)),
        "tick_interval": int(s.get("tick_interval", DEFAULT_TICK_INTERVAL)),
    }


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
        try:
            conn = sqlite3.connect(DATABASE, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            settings = get_settings(conn)
            interval = settings["tick_interval"]
            pts = settings["points_per_tick"]
            conn.close()
        except Exception as e:
            print(f"Tick settings error: {e}")
            interval = DEFAULT_TICK_INTERVAL
            pts = DEFAULT_POINTS_PER_TICK

        time.sleep(interval)

        try:
            conn = sqlite3.connect(DATABASE, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE teams SET points = points + ?", (pts,))
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
    settings = get_settings()
    return jsonify(
        points_per_tick=settings["points_per_tick"],
        spend_options=SPEND_OPTIONS,
        spend_named=SPEND_NAMED,
        tick_interval=settings["tick_interval"],
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

    # Validate: admin can spend any positive amount, user uses SPEND_NAMED
    is_admin = session["role"] == "admin"
    if is_admin:
        if not isinstance(amount, int) or amount <= 0:
            return jsonify(error="Сумма должна быть > 0"), 400
    else:
        valid_amounts = [s["cost"] for s in SPEND_NAMED]
        if amount not in valid_amounts:
            return jsonify(error="Недопустимая сумма списания"), 400

    db = get_db()
    team = db.execute("SELECT id, points FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        return jsonify(error="Команда не найдена"), 404
    if team["points"] < amount:
        return jsonify(error="Недостаточно очков"), 400

    txn_uuid = None if is_admin else str(uuid.uuid4())
    report_label = "admin" if is_admin else (label or None)
    points_before = team["points"]
    points_after = points_before - amount
    db.execute("UPDATE teams SET points = points - ? WHERE id = ?", (amount, team_id))
    db.execute(
        "INSERT INTO reports (team_id, cost, points_before, points_after, label, uuid, type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (team_id, amount, points_before, points_after, report_label, txn_uuid, "списание"),
    )
    db.commit()
    result = {"ok": True}
    if txn_uuid:
        result["uuid"] = txn_uuid
    return jsonify(result)


@app.route("/api/reports")
@login_required
def list_reports():
    page = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(100, int(request.args.get("per_page", 20))))
    search = (request.args.get("q") or "").strip()
    offset = (page - 1) * per_page
    db = get_db()

    base_where = []
    params_where = []

    if session["role"] != "admin":
        base_where.append("r.team_id = ?")
        params_where.append(session.get("team_id"))

    if search:
        base_where.append("(t.name LIKE ? OR r.uuid LIKE ?)")
        like = f"%{search}%"
        params_where.extend([like, like])

    where_sql = ("WHERE " + " AND ".join(base_where)) if base_where else ""

    total = db.execute(
        f"SELECT COUNT(*) FROM reports r JOIN teams t ON t.id = r.team_id {where_sql}",
        params_where,
    ).fetchone()[0]

    rows = db.execute(f"""
        SELECT r.id, t.name AS team_name, r.cost, r.points_before, r.points_after, r.label, r.uuid, r.type, r.checked, r.created_at
        FROM reports r JOIN teams t ON t.id = r.team_id
        {where_sql}
        ORDER BY r.created_at DESC
        LIMIT ? OFFSET ?
    """, params_where + [per_page, offset]).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return jsonify(items=[dict(r) for r in rows], page=page, per_page=per_page,
                   total=total, total_pages=total_pages)


@app.route("/api/teams/<int:team_id>", methods=["DELETE"])
@admin_required
def delete_team(team_id):
    db = get_db()
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        return jsonify(error="Команда не найдена"), 404
    db.execute("DELETE FROM reports WHERE team_id = ?", (team_id,))
    db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/reports/<int:report_id>/check", methods=["POST"])
@admin_required
def toggle_check(report_id):
    db = get_db()
    report = db.execute("SELECT id, checked FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not report:
        return jsonify(error="Запись не найдена"), 404
    new_val = 0 if report["checked"] else 1
    db.execute("UPDATE reports SET checked = ? WHERE id = ?", (new_val, report_id))
    db.commit()
    return jsonify(ok=True, checked=new_val)


# ---- Админ: управление игрой ----

@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def update_settings():
    data = request.get_json(force=True)
    pts = data.get("points_per_tick")
    interval = data.get("tick_interval")
    db = get_db()
    if pts is not None:
        pts = int(pts)
        if pts < 0:
            return jsonify(error="Очки не могут быть отрицательными"), 400
        db.execute("UPDATE settings SET value = ? WHERE key = ?", (str(pts), "points_per_tick"))
    if interval is not None:
        interval = int(interval)
        if interval < 1:
            return jsonify(error="Интервал должен быть >= 1 секунды"), 400
        db.execute("UPDATE settings SET value = ? WHERE key = ?", (str(interval), "tick_interval"))
    db.commit()
    settings = get_settings()
    return jsonify(ok=True, **settings)


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
    teams = db.execute("SELECT id, points FROM teams").fetchall()
    db.execute("UPDATE teams SET points = points + ?", (amount,))
    for t in teams:
        db.execute(
            "INSERT INTO reports (team_id, cost, points_before, points_after, label, type) VALUES (?, ?, ?, ?, ?, ?)",
            (t["id"], -amount, t["points"], t["points"] + amount, "admin", "начисление"),
        )
    db.commit()
    return jsonify(ok=True)


@app.route("/api/teams/<int:team_id>/add-points", methods=["POST"])
@admin_required
def add_points_team(team_id):
    data = request.get_json(force=True)
    amount = int(data.get("amount", 0))
    if amount <= 0:
        return jsonify(error="Сумма должна быть > 0"), 400
    db = get_db()
    team = db.execute("SELECT id, points FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        return jsonify(error="Команда не найдена"), 404
    points_before = team["points"]
    db.execute("UPDATE teams SET points = points + ? WHERE id = ?", (amount, team_id))
    db.execute(
        "INSERT INTO reports (team_id, cost, points_before, points_after, label, type) VALUES (?, ?, ?, ?, ?, ?)",
        (team_id, -amount, points_before, points_before + amount, "admin", "начисление"),
    )
    db.commit()
    return jsonify(ok=True)


# ---- Запуск ----

init_db()


def start_tick_thread():
    t = threading.Thread(target=tick_points, daemon=True)
    t.start()
    print("Tick thread started (reads settings from DB)")


if __name__ == "__main__":
    start_tick_thread()
    print(f"Spend options: {SPEND_OPTIONS}")
    print(f"Admin password: {ADMIN_PASSWORD}")
    app.run(host="0.0.0.0", debug=False, port=5000)
