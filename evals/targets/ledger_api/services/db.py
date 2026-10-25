"""SQLite access for the ledger service."""

import sqlite3

from . import queries

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row

_SCHEMA = """
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS users;
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    customer TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL, email TEXT NOT NULL);
INSERT INTO invoices (customer, amount_cents, status, created) VALUES
    ('acme', 12000, 'paid', '2026-01-04'),
    ('acme', 4500, 'open', '2026-02-11'),
    ('globex', 99900, 'open', '2026-03-02'),
    ('initech', 2500, 'paid', '2026-03-19');
INSERT INTO users (username, email) VALUES
    ('acme', 'billing@acme.example.test'),
    ('globex', 'ap@globex.example.test');
"""


def init() -> None:
    _conn.executescript(_SCHEMA)


def query_all(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in _conn.execute(sql, params).fetchall()]


def add_user(username: str, email: str) -> int:
    cur = _conn.execute("INSERT INTO users (username, email) VALUES (?, ?)", (username, email))
    _conn.commit()
    return cur.lastrowid


def get_user(user_id: int) -> dict | None:
    rows = query_all("SELECT id, username, email FROM users WHERE id = ?", (user_id,))
    return rows[0] if rows else None


def list_invoices(sort: str, status: str | None = None) -> list[dict]:
    sql = "SELECT id, customer, amount_cents, status FROM invoices"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += queries.build_order_clause(sort)
    return query_all(sql, params)


def list_for_customer(customer: str, order_clause: str) -> list[dict]:
    sql = "SELECT id, customer, amount_cents, status FROM invoices WHERE customer = ?"
    return query_all(sql + order_clause, (customer,))


def search_invoices(term: str) -> list[dict]:
    sql = "SELECT id, customer, amount_cents, status FROM invoices WHERE customer LIKE ?"
    return query_all(sql, (f"%{term}%",))
