"""Business logic. Plain Python + sqlite3, no web framework.

Keeping this separate from the HTTP layer means it can be tested on its own
and the rules (e.g. "never half-place an order") live in one place.
"""
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager


# ---------- errors the API layer turns into HTTP status codes ----------

class NotFound(Exception):
    """-> 404"""


class Conflict(Exception):
    """-> 409 (request is valid but clashes with current state)"""


class InsufficientStock(Exception):
    """-> 400, carries every product that was short."""

    def __init__(self, shortages: list[dict]):
        self.shortages = shortages
        names = ", ".join(
            f"{s['name']} (requested {s['requested']}, available {s['available']})"
            for s in shortages
        )
        super().__init__(f"Insufficient stock: {names}")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All-or-nothing block. BEGIN IMMEDIATE takes the write lock up front,
    so nobody else can change stock between our check and our write."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ------------------------- stock movements -------------------------

def _log_movement(conn, product_id: int, change: int, reason: str,
                  order_id: int | None = None, note: str | None = None) -> None:
    """Record one stock change. Call it right AFTER updating stock_qty,
    inside the same transaction, so the log and the stock can't disagree."""
    balance = conn.execute(
        "SELECT stock_qty FROM products WHERE id = ?", (product_id,)
    ).fetchone()["stock_qty"]
    conn.execute(
        "INSERT INTO stock_movements"
        " (product_id, change, balance_after, reason, order_id, note)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, change, balance, reason, order_id, note),
    )


def restock(conn, product_id: int, quantity: int, note: str | None = None) -> dict:
    if quantity <= 0:
        raise ValueError("Restock quantity must be positive")
    with transaction(conn):
        get_product(conn, product_id)  # 404 if missing (rolls back)
        conn.execute(
            "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
            (quantity, product_id),
        )
        _log_movement(conn, product_id, quantity, "restock", note=note)
    return get_product(conn, product_id)


def list_movements(conn, product_id: int) -> list[dict]:
    get_product(conn, product_id)
    rows = conn.execute(
        "SELECT * FROM stock_movements WHERE product_id = ? ORDER BY id",
        (product_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------- products ----------------------------

def _product_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "price_paise": row["price_paise"],
        "stock_qty": row["stock_qty"],
    }


def create_product(conn, name: str, price_paise: int, stock_qty: int) -> dict:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO products (name, price_paise, stock_qty) VALUES (?, ?, ?)",
            (name, price_paise, stock_qty),
        )
        if stock_qty > 0:
            _log_movement(conn, cur.lastrowid, stock_qty, "initial")
    return get_product(conn, cur.lastrowid)


def list_products(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return [_product_dict(r) for r in rows]


def get_product(conn, product_id: int) -> dict:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise NotFound(f"Product {product_id} not found")
    return _product_dict(row)


def update_product(conn, product_id: int, **fields) -> dict:
    """Partial update: only the fields passed (and not None) change."""
    allowed = {"name", "price_paise", "stock_qty"}
    changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
    with transaction(conn):
        before = get_product(conn, product_id)  # 404 if missing
        if changes:
            # Column names come from the fixed `allowed` set, never from user
            # input, so building the SET clause this way is safe.
            set_clause = ", ".join(f"{col} = ?" for col in changes)
            conn.execute(
                f"UPDATE products SET {set_clause} WHERE id = ?",
                (*changes.values(), product_id),
            )
            # Setting stock directly (e.g. after a stock count) is logged as
            # an adjustment of the difference.
            if "stock_qty" in changes:
                delta = changes["stock_qty"] - before["stock_qty"]
                if delta != 0:
                    _log_movement(conn, product_id, delta, "adjustment",
                                  note="Stock set via update")
    return get_product(conn, product_id)


def delete_product(conn, product_id: int) -> None:
    get_product(conn, product_id)
    try:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    except sqlite3.IntegrityError:
        # ON DELETE RESTRICT: deleting would orphan old order lines.
        raise Conflict(
            f"Product {product_id} appears in existing orders and can't be deleted"
        )


def low_stock(conn, threshold: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM products WHERE stock_qty < ? ORDER BY stock_qty, id",
        (threshold,),
    ).fetchall()
    return [_product_dict(r) for r in rows]


# ----------------------------- orders -----------------------------

def place_order(conn, customer_name: str, items: list[dict]) -> dict:
    """items: [{"product_id": int, "quantity": int}, ...]

    Two phases inside one transaction:
      1. CHECK every line (exists? enough stock?) and collect ALL problems.
      2. Only if nothing failed, WRITE the order, its lines and the stock.
    Any exception rolls the whole thing back, so an order can never
    half-succeed.
    """
    if not items:
        raise ValueError("An order needs at least one item")

    # Merge duplicate lines: [{A,2},{A,3}] must be checked as A x 5,
    # otherwise each line passes alone but together they oversell.
    wanted: "OrderedDict[int, int]" = OrderedDict()
    for item in items:
        if item["quantity"] <= 0:
            raise ValueError("Quantity must be positive")
        wanted[item["product_id"]] = wanted.get(item["product_id"], 0) + item["quantity"]

    with transaction(conn):
        # ---- phase 1: check everything, write nothing ----
        products = {}
        missing, shortages = [], []
        for pid, qty in wanted.items():
            row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if row is None:
                missing.append(pid)
                continue
            products[pid] = row
            if row["stock_qty"] < qty:
                shortages.append({
                    "product_id": pid,
                    "name": row["name"],
                    "requested": qty,
                    "available": row["stock_qty"],
                })
        if missing:
            raise NotFound(f"Products not found: {missing}")
        if shortages:
            raise InsufficientStock(shortages)

        # ---- phase 2: every line is OK, now write ----
        cur = conn.execute(
            "INSERT INTO orders (customer_name) VALUES (?)", (customer_name,)
        )
        order_id = cur.lastrowid
        for pid, qty in wanted.items():
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price_paise)"
                " VALUES (?, ?, ?, ?)",
                (order_id, pid, qty, products[pid]["price_paise"]),
            )
            conn.execute(
                "UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
                (qty, pid),
            )
            _log_movement(conn, pid, -qty, "order", order_id=order_id)

    return get_order(conn, order_id)


def _order_summary(row) -> dict:
    return {
        "id": row["id"],
        "customer_name": row["customer_name"],
        "created_at": row["created_at"],
        "status": row["status"],
        "total_paise": row["total_paise"],
    }


# Total is computed from the lines, not stored, so it can never disagree
# with them.
_ORDER_WITH_TOTAL = """
    SELECT o.*, COALESCE(SUM(oi.quantity * oi.unit_price_paise), 0) AS total_paise
    FROM orders o
    LEFT JOIN order_items oi ON oi.order_id = o.id
"""


def list_orders(conn) -> list[dict]:
    rows = conn.execute(_ORDER_WITH_TOTAL + " GROUP BY o.id ORDER BY o.id").fetchall()
    return [_order_summary(r) for r in rows]


def get_order(conn, order_id: int) -> dict:
    row = conn.execute(
        _ORDER_WITH_TOTAL + " WHERE o.id = ? GROUP BY o.id", (order_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"Order {order_id} not found")
    order = _order_summary(row)
    lines = conn.execute(
        """SELECT oi.id, oi.product_id, p.name AS product_name, oi.quantity,
                  oi.unit_price_paise,
                  oi.quantity * oi.unit_price_paise AS line_total_paise
           FROM order_items oi JOIN products p ON p.id = oi.product_id
           WHERE oi.order_id = ? ORDER BY oi.id""",
        (order_id,),
    ).fetchall()
    order["items"] = [dict(r) for r in lines]
    return order


def cancel_order(conn, order_id: int) -> dict:
    """Mark cancelled and put every line's quantity back on the shelf,
    atomically. Cancelling twice is refused, or stock would be returned
    twice."""
    with transaction(conn):
        row = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"Order {order_id} not found")
        if row["status"] == "cancelled":
            raise Conflict(f"Order {order_id} is already cancelled")

        lines = conn.execute(
            "SELECT product_id, quantity FROM order_items WHERE order_id = ?",
            (order_id,),
        ).fetchall()
        for line in lines:
            conn.execute(
                "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                (line["quantity"], line["product_id"]),
            )
            _log_movement(conn, line["product_id"], line["quantity"], "cancel",
                          order_id=order_id)
        conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,)
        )
    return get_order(conn, order_id)