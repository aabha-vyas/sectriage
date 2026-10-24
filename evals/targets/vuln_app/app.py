"""Small notes service used as a local eval target."""

import sqlite3
import subprocess
from pathlib import Path

from flask import Flask, abort, request, send_file
from markupsafe import escape

app = Flask(__name__)
app.config["SECRET_KEY"] = "insecure-demo-secret-do-not-use"

BASE_DIR = Path(__file__).parent
DOCS_DIR = BASE_DIR / "docs"

db = sqlite3.connect(":memory:", check_same_thread=False)
db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
db.execute("INSERT INTO users (name, email) VALUES ('alice', 'alice@example.test')")
db.execute("INSERT INTO users (name, email) VALUES ('bob', 'bob@example.test')")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/users")
def find_user():
    name = request.args.get("name", "")
    query = f"SELECT id, name, email FROM users WHERE name = '{name}'"
    rows = db.execute(query).fetchall()
    return {"users": rows}


@app.get("/users/by-id")
def find_user_by_id():
    user_id = request.args.get("id", "")
    rows = db.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchall()
    return {"users": rows}


@app.get("/greet")
def greet():
    name = request.args.get("name", "world")
    return "<html><body><h1>Hello, " + name + "!</h1></body></html>"


@app.get("/greet-safe")
def greet_safe():
    name = request.args.get("name", "world")
    return f"<html><body><h1>Hello, {escape(name)}!</h1></body></html>"


@app.get("/docs/download")
def download():
    filename = request.args.get("file", "")
    path = DOCS_DIR / filename
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.get("/ping")
def ping():
    host = request.args.get("host", "127.0.0.1")
    result = subprocess.run(
        "ping -c 1 " + host,
        shell=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return {"output": result.stdout}


@app.get("/version")
def version():
    result = subprocess.run(["uname", "-s"], capture_output=True, text=True, timeout=5)
    return {"os": result.stdout.strip()}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
