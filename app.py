import base64
import hmac
import json
import os
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "participation.sqlite3"))


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY,
                lab_date TEXT NOT NULL,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (status IN ('present', 'absent', 'excused')),
                attended_group_id INTEGER REFERENCES groups(id),
                UNIQUE (lab_date, student_id)
            );
            """
        )


class RequestError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


class Handler(BaseHTTPRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        password = os.environ.get("APP_PASSWORD")
        if password:
            authorization = self.headers.get("Authorization", "")
            try:
                scheme, encoded = authorization.split(" ", 1)
                username_password = base64.b64decode(encoded, validate=True).decode()
                supplied_password = username_password.split(":", 1)[1]
            except (ValueError, UnicodeDecodeError, IndexError):
                supplied_password = ""
                scheme = ""
            if scheme.lower() != "basic" or not hmac.compare_digest(supplied_password, password):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Lab participation"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
        return True

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except (ValueError, json.JSONDecodeError):
            raise RequestError("Request body must be valid JSON.")

    def do_GET(self):
        route = urlparse(self.path)
        with connect() as db:
            if route.path == "/api/groups":
                rows = db.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
                return self.send_json([dict(row) for row in rows])
            if route.path == "/api/students":
                rows = db.execute(
                    """SELECT s.id, s.name, s.group_id, g.name AS group_name
                       FROM students s JOIN groups g ON g.id = s.group_id
                       ORDER BY g.name, s.name"""
                ).fetchall()
                return self.send_json([dict(row) for row in rows])
            if route.path == "/api/attendance":
                params = parse_qs(route.query)
                start = params.get("from", [""])[0]
                end = params.get("to", [""])[0]
                if not valid_date(start) or not valid_date(end) or start > end:
                    raise RequestError("Provide a valid date range.")
                rows = db.execute(
                    """SELECT a.lab_date, a.student_id, a.status,
                              a.attended_group_id, s.name AS student_name,
                              home.name AS home_group,
                              attended.name AS attended_group
                       FROM attendance a
                       JOIN students s ON s.id = a.student_id
                       JOIN groups home ON home.id = s.group_id
                       LEFT JOIN groups attended ON attended.id = a.attended_group_id
                       WHERE a.lab_date BETWEEN ? AND ?
                       ORDER BY a.lab_date, home.name, s.name""",
                    (start, end),
                ).fetchall()
                return self.send_json([dict(row) for row in rows])
        if route.path == "/":
            return self.serve_file(ROOT / "index.html", "text/html; charset=utf-8")
        if route.path == "/favicon.ico":
            self.send_response(204)
            return self.end_headers()
        self.send_json({"error": "Not found."}, 404)

    def do_POST(self):
        payload = self.body_json()
        with connect() as db:
            if self.path == "/api/groups":
                name = clean_text(payload.get("name"))
                if not name:
                    raise RequestError("Enter a group name.")
                try:
                    cursor = db.execute("INSERT INTO groups (name) VALUES (?)", (name,))
                except sqlite3.IntegrityError:
                    raise RequestError("That group already exists.")
                return self.send_json({"id": cursor.lastrowid, "name": name}, 201)
            if self.path == "/api/students":
                name = clean_text(payload.get("name"))
                group_id = payload.get("group_id")
                if not name or not isinstance(group_id, int):
                    raise RequestError("Enter a student name and select a group.")
                try:
                    cursor = db.execute(
                        "INSERT INTO students (name, group_id) VALUES (?, ?)",
                        (name, group_id),
                    )
                except sqlite3.IntegrityError:
                    raise RequestError("Select an existing group.")
                return self.send_json({"id": cursor.lastrowid}, 201)
            if self.path == "/api/attendance":
                lab_date = payload.get("date")
                records = payload.get("records")
                if not valid_date(lab_date) or not isinstance(records, list):
                    raise RequestError("Provide a valid date and attendance records.")
                try:
                    with db:
                        db.execute("DELETE FROM attendance WHERE lab_date = ?", (lab_date,))
                        for record in records:
                            status = record.get("status")
                            if status not in ("present", "absent", "excused"):
                                raise RequestError("Invalid attendance status.")
                            student_id = record.get("student_id")
                            attended_group_id = record.get("attended_group_id")
                            if not isinstance(student_id, int) or (
                                attended_group_id is not None
                                and not isinstance(attended_group_id, int)
                            ):
                                raise RequestError("Invalid student or group.")
                            db.execute(
                                """INSERT INTO attendance
                                   (lab_date, student_id, status, attended_group_id)
                                   VALUES (?, ?, ?, ?)""",
                                (lab_date, student_id, status, attended_group_id),
                            )
                except sqlite3.IntegrityError:
                    raise RequestError("A selected student or group does not exist.")
                return self.send_json({"saved": len(records)})
        self.send_json({"error": "Not found."}, 404)

    def do_DELETE(self):
        parts = self.path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "students"]:
            try:
                student_id = int(parts[3])
            except ValueError:
                raise RequestError("Invalid student ID.")
            with connect() as db:
                cursor = db.execute("DELETE FROM students WHERE id = ?", (student_id,))
                if cursor.rowcount == 0:
                    raise RequestError("Student not found.", 404)
            return self.send_json({"deleted": student_id})
        self.send_json({"error": "Not found."}, 404)

    def serve_file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except RequestError as error:
            self.send_json({"error": error.message}, error.status)

    def log_message(self, _format, *_args):
        pass


def clean_text(value):
    return value.strip() if isinstance(value, str) else ""


def valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


if __name__ == "__main__":
    initialize()
    if "PORT" in os.environ and not os.environ.get("APP_PASSWORD"):
        raise SystemExit("Set APP_PASSWORD before starting on a public-facing port.")
    port = int(os.environ.get("PORT", "8000"))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Lab participation tracker listening on port {port}")
    server.serve_forever()
