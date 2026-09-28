import hmac
import os
import sqlite3
from datetime import date
from pathlib import Path

from flask import Flask, g, has_request_context, jsonify, request, send_from_directory


ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "participation.sqlite3"))
app = Flask(__name__)


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def request_db():
    if not has_request_context():
        return connect()
    if "database" not in g:
        g.database = connect()
    return g.database


@app.teardown_appcontext
def close_database(_error=None):
    db = g.pop("database", None)
    if db is not None:
        db.close()


def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                group_id INTEGER NOT NULL REFERENCES groups(id)
            );
            """
        )
        attendance = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'attendance'"
        ).fetchone()
        if attendance is None:
            db.execute(
                """CREATE TABLE attendance (
                    id INTEGER PRIMARY KEY,
                    lab_date TEXT NOT NULL,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    home_group_id INTEGER NOT NULL REFERENCES groups(id),
                    status TEXT NOT NULL CHECK (status IN ('planned', 'present', 'absent', 'excused')),
                    attended_group_id INTEGER REFERENCES groups(id),
                    UNIQUE (lab_date, student_id)
                )"""
            )
        elif (
            "planned" not in attendance["sql"].lower()
            or "home_group_id" not in attendance["sql"].lower()
        ):
            with db:
                db.execute("ALTER TABLE attendance RENAME TO attendance_legacy")
                db.execute(
                    """CREATE TABLE attendance (
                        id INTEGER PRIMARY KEY,
                        lab_date TEXT NOT NULL,
                        student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                        home_group_id INTEGER NOT NULL REFERENCES groups(id),
                        status TEXT NOT NULL CHECK (status IN ('planned', 'present', 'absent', 'excused')),
                        attended_group_id INTEGER REFERENCES groups(id),
                        UNIQUE (lab_date, student_id)
                    )"""
                )
                db.execute(
                    """INSERT INTO attendance
                       (id, lab_date, student_id, home_group_id, status, attended_group_id)
                       SELECT a.id, a.lab_date, a.student_id, s.group_id, a.status,
                              a.attended_group_id
                       FROM attendance_legacy a
                       JOIN students s ON s.id = a.student_id"""
                )
                db.execute("DROP TABLE attendance_legacy")


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.before_request
def require_password():
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    authorization = request.authorization
    if authorization and authorization.type.lower() == "basic":
        supplied = authorization.password or ""
        if hmac.compare_digest(supplied, password):
            return None
    response = jsonify(error="Authentication required.")
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="Lab participation"'
    return response


@app.errorhandler(RequestError)
def handle_request_error(error):
    return jsonify(error=error.message), error.status


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/api/groups")
def list_groups():
    with request_db() as db:
        rows = db.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/groups")
def create_group():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise RequestError("Request body must be a JSON object.")
    name = clean_text(payload.get("name"))
    if not name:
        raise RequestError("Enter a group name.")
    with request_db() as db:
        try:
            cursor = db.execute("INSERT INTO groups (name) VALUES (?)", (name,))
        except sqlite3.IntegrityError:
            raise RequestError("That group already exists.")
    return jsonify(id=cursor.lastrowid, name=name), 201


@app.get("/api/students")
def list_students():
    with request_db() as db:
        rows = db.execute(
            """SELECT s.id, s.name, s.group_id, g.name AS group_name
               FROM students s JOIN groups g ON g.id = s.group_id
               ORDER BY g.name, s.name"""
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/students")
def create_student():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise RequestError("Request body must be a JSON object.")
    name = clean_text(payload.get("name"))
    group_id = payload.get("group_id")
    if not name or not is_int(group_id):
        raise RequestError("Enter a student name and select a group.")
    with request_db() as db:
        try:
            cursor = db.execute(
                "INSERT INTO students (name, group_id) VALUES (?, ?)",
                (name, group_id),
            )
        except sqlite3.IntegrityError:
            raise RequestError("Select an existing group.")
    return jsonify(id=cursor.lastrowid), 201


@app.patch("/api/students/<int:student_id>")
def move_student(student_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not is_int(payload.get("group_id")):
        raise RequestError("Select a group.")
    with request_db() as db:
        try:
            cursor = db.execute(
                "UPDATE students SET group_id = ? WHERE id = ?",
                (payload["group_id"], student_id),
            )
        except sqlite3.IntegrityError:
            raise RequestError("Select an existing group.")
        if cursor.rowcount == 0:
            raise RequestError("Student not found.", 404)
    return jsonify(updated=student_id, group_id=payload["group_id"])


@app.delete("/api/students/<int:student_id>")
def delete_student(student_id):
    with request_db() as db:
        cursor = db.execute("DELETE FROM students WHERE id = ?", (student_id,))
        if cursor.rowcount == 0:
            raise RequestError("Student not found.", 404)
    return jsonify(deleted=student_id)


@app.get("/api/attendance")
def list_attendance():
    start = request.args.get("from", "")
    end = request.args.get("to", "")
    if not valid_date(start) or not valid_date(end) or start > end:
        raise RequestError("Provide a valid date range.")
    with request_db() as db:
        rows = db.execute(
            """SELECT a.lab_date, a.student_id, a.home_group_id, a.status,
                      a.attended_group_id, s.name AS student_name,
                      home.name AS home_group,
                      attended.name AS attended_group
               FROM attendance a
               JOIN students s ON s.id = a.student_id
               JOIN groups home ON home.id = a.home_group_id
               LEFT JOIN groups attended ON attended.id = a.attended_group_id
               WHERE a.lab_date BETWEEN ? AND ?
               ORDER BY a.lab_date, home.name, s.name""",
            (start, end),
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/attendance")
def save_attendance():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise RequestError("Request body must be a JSON object.")
    lab_date = payload.get("date")
    records = payload.get("records")
    if not valid_date(lab_date) or not isinstance(records, list):
        raise RequestError("Provide a valid date and attendance records.")
    try:
        with request_db() as db:
            with db:
                db.execute("DELETE FROM attendance WHERE lab_date = ?", (lab_date,))
                for record in records:
                    if not isinstance(record, dict):
                        raise RequestError("Each attendance record must be an object.")
                    status = record.get("status")
                    if status not in ("planned", "present", "absent", "excused"):
                        raise RequestError("Invalid attendance status.")
                    student_id = record.get("student_id")
                    home_group_id = record.get("home_group_id")
                    attended_group_id = record.get("attended_group_id")
                    if not is_int(student_id) or not is_int(home_group_id):
                        raise RequestError("Invalid student or group.")
                    if status in ("planned", "present"):
                        if not is_int(attended_group_id):
                            raise RequestError("Select the group the student plans to attend.")
                    elif attended_group_id is not None:
                        raise RequestError(
                            "Only planned or present students can be assigned a group."
                        )
                    db.execute(
                        """INSERT INTO attendance
                           (lab_date, student_id, home_group_id, status, attended_group_id)
                           VALUES (?, ?, ?, ?, ?)""",
                        (lab_date, student_id, home_group_id, status, attended_group_id),
                    )
    except sqlite3.IntegrityError:
        raise RequestError("A selected student or group does not exist.")
    return jsonify(saved=len(records))


def clean_text(value):
    return value.strip() if isinstance(value, str) else ""


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


initialize()

if "PORT" in os.environ and not os.environ.get("APP_PASSWORD"):
    raise RuntimeError("Set APP_PASSWORD before starting on a public-facing port.")


if __name__ == "__main__":
    app.run(host="0.0.0.0" if "PORT" in os.environ else "127.0.0.1",
            port=int(os.environ.get("PORT", "8000")))
