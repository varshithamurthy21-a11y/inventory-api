"""Database connection and schema.

Money is stored as INTEGER paise (1 rupee = 100 paise), never as a float.
Floats can't represent 0.10 exactly, so totals drift; integers don't.
"""
import sqlite3

DB_PATH = "inventory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
    stock_qty   INTEGER NOT NULL CHECK (stock_qty >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS order_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER NOT NULL REFERENCES orders(id)   ON DELETE CASCADE,
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    -- Price copied from the product at order time, so later price
    -- changes don't rewrite what the customer was actually charged.
    unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    # isolation_level=None turns off Python's implicit transactions so we
    # control BEGIN/COMMIT ourselves. check_same_thread=False because
    # FastAPI may run a request on a different thread than the one that
    # opened the connection.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite has FKs off by default
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
