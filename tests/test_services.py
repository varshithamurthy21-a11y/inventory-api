"""Service-layer tests. Standard library only:  python -m unittest -v"""
import unittest

import db
import services as svc


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.pen = svc.create_product(self.conn, "Pen", 1000, 10)       # ₹10.00
        self.book = svc.create_product(self.conn, "Notebook", 4550, 3)  # ₹45.50

    def stock(self, pid):
        return svc.get_product(self.conn, pid)["stock_qty"]


class ProductTests(Base):
    def test_crud(self):
        self.assertEqual(len(svc.list_products(self.conn)), 2)
        p = svc.update_product(self.conn, self.pen["id"], price_paise=1200)
        self.assertEqual(p["price_paise"], 1200)
        self.assertEqual(p["name"], "Pen")  # untouched fields stay
        svc.delete_product(self.conn, self.pen["id"])
        with self.assertRaises(svc.NotFound):
            svc.get_product(self.conn, self.pen["id"])

    def test_cannot_delete_product_used_in_order(self):
        svc.place_order(self.conn, "Asha", [{"product_id": self.pen["id"], "quantity": 1}])
        with self.assertRaises(svc.Conflict):
            svc.delete_product(self.conn, self.pen["id"])

    def test_low_stock(self):
        names = [p["name"] for p in svc.low_stock(self.conn, 5)]
        self.assertEqual(names, ["Notebook"])


class OrderTests(Base):
    def test_place_order_decrements_and_totals(self):
        o = svc.place_order(self.conn, "Asha", [
            {"product_id": self.pen["id"], "quantity": 4},
            {"product_id": self.book["id"], "quantity": 2},
        ])
        self.assertEqual(self.stock(self.pen["id"]), 6)
        self.assertEqual(self.stock(self.book["id"]), 1)
        self.assertEqual(o["total_paise"], 4 * 1000 + 2 * 4550)
        self.assertEqual(len(o["items"]), 2)
        self.assertEqual(o["status"], "pending")

    def test_short_second_line_writes_nothing(self):
        """The core rule: line 1 is fine, line 2 is short -> no change at all."""
        with self.assertRaises(svc.InsufficientStock) as ctx:
            svc.place_order(self.conn, "Asha", [
                {"product_id": self.pen["id"], "quantity": 2},
                {"product_id": self.book["id"], "quantity": 99},
            ])
        self.assertEqual(ctx.exception.shortages[0]["name"], "Notebook")
        self.assertEqual(ctx.exception.shortages[0]["available"], 3)
        self.assertEqual(self.stock(self.pen["id"]), 10)   # untouched
        self.assertEqual(self.stock(self.book["id"]), 3)
        self.assertEqual(svc.list_orders(self.conn), [])    # no orphan header

    def test_all_short_products_reported(self):
        with self.assertRaises(svc.InsufficientStock) as ctx:
            svc.place_order(self.conn, "Asha", [
                {"product_id": self.pen["id"], "quantity": 11},
                {"product_id": self.book["id"], "quantity": 4},
            ])
        self.assertEqual(len(ctx.exception.shortages), 2)

    def test_duplicate_lines_are_combined(self):
        """2 + 2 notebooks = 4 > 3 in stock, even though each line alone fits."""
        with self.assertRaises(svc.InsufficientStock):
            svc.place_order(self.conn, "Asha", [
                {"product_id": self.book["id"], "quantity": 2},
                {"product_id": self.book["id"], "quantity": 2},
            ])
        self.assertEqual(self.stock(self.book["id"]), 3)

    def test_exact_stock_is_allowed(self):
        svc.place_order(self.conn, "Asha", [{"product_id": self.book["id"], "quantity": 3}])
        self.assertEqual(self.stock(self.book["id"]), 0)

    def test_unknown_product_writes_nothing(self):
        with self.assertRaises(svc.NotFound):
            svc.place_order(self.conn, "Asha", [
                {"product_id": self.pen["id"], "quantity": 1},
                {"product_id": 999, "quantity": 1},
            ])
        self.assertEqual(self.stock(self.pen["id"]), 10)
        self.assertEqual(svc.list_orders(self.conn), [])

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            svc.place_order(self.conn, "Asha", [])
        with self.assertRaises(ValueError):
            svc.place_order(self.conn, "Asha", [{"product_id": self.pen["id"], "quantity": 0}])

    def test_price_change_does_not_rewrite_history(self):
        o = svc.place_order(self.conn, "Asha", [{"product_id": self.pen["id"], "quantity": 2}])
        svc.update_product(self.conn, self.pen["id"], price_paise=5000)
        again = svc.get_order(self.conn, o["id"])
        self.assertEqual(again["items"][0]["unit_price_paise"], 1000)
        self.assertEqual(again["total_paise"], 2000)

    def test_cancel_returns_stock_once(self):
        o = svc.place_order(self.conn, "Asha", [
            {"product_id": self.pen["id"], "quantity": 4},
            {"product_id": self.book["id"], "quantity": 3},
        ])
        c = svc.cancel_order(self.conn, o["id"])
        self.assertEqual(c["status"], "cancelled")
        self.assertEqual(self.stock(self.pen["id"]), 10)
        self.assertEqual(self.stock(self.book["id"]), 3)
        with self.assertRaises(svc.Conflict):
            svc.cancel_order(self.conn, o["id"])
        self.assertEqual(self.stock(self.pen["id"]), 10)  # not returned twice

    def test_cancel_missing_order(self):
        with self.assertRaises(svc.NotFound):
            svc.cancel_order(self.conn, 42)

    def test_list_and_get(self):
        svc.place_order(self.conn, "Asha", [{"product_id": self.pen["id"], "quantity": 1}])
        svc.place_order(self.conn, "Ravi", [{"product_id": self.book["id"], "quantity": 1}])
        orders = svc.list_orders(self.conn)
        self.assertEqual([o["customer_name"] for o in orders], ["Asha", "Ravi"])
        self.assertEqual(orders[1]["total_paise"], 4550)
        with self.assertRaises(svc.NotFound):
            svc.get_order(self.conn, 999)


class MovementTests(Base):
    def history(self, pid):
        return [(m["reason"], m["change"], m["balance_after"])
                for m in svc.list_movements(self.conn, pid)]

    def assert_ledger_matches_stock(self):
        """The core rule: for every product, the sum of its movements
        equals its current stock."""
        for p in svc.list_products(self.conn):
            total = sum(m["change"] for m in svc.list_movements(self.conn, p["id"]))
            self.assertEqual(total, p["stock_qty"], p["name"])

    def test_create_logs_initial_stock(self):
        self.assertEqual(self.history(self.pen["id"]), [("initial", 10, 10)])
        empty = svc.create_product(self.conn, "Eraser", 500, 0)
        self.assertEqual(self.history(empty["id"]), [])  # nothing to log

    def test_restock(self):
        p = svc.restock(self.conn, self.book["id"], 5, note="Supplier delivery")
        self.assertEqual(p["stock_qty"], 8)
        last = svc.list_movements(self.conn, self.book["id"])[-1]
        self.assertEqual((last["reason"], last["change"], last["balance_after"]),
                         ("restock", 5, 8))
        self.assertEqual(last["note"], "Supplier delivery")

    def test_restock_validation(self):
        with self.assertRaises(ValueError):
            svc.restock(self.conn, self.pen["id"], 0)
        with self.assertRaises(svc.NotFound):
            svc.restock(self.conn, 999, 5)

    def test_answers_why_is_this_7(self):
        """Pen: +10 initial, -4 order, -2 order, +4 cancel, -1 adjustment = 7"""
        o1 = svc.place_order(self.conn, "Asha", [{"product_id": self.pen["id"], "quantity": 4}])
        svc.place_order(self.conn, "Ravi", [{"product_id": self.pen["id"], "quantity": 2}])
        svc.cancel_order(self.conn, o1["id"])
        svc.update_product(self.conn, self.pen["id"], stock_qty=7)  # counted 7 on shelf
        self.assertEqual(self.history(self.pen["id"]), [
            ("initial", 10, 10),
            ("order", -4, 6),
            ("order", -2, 4),
            ("cancel", 4, 8),
            ("adjustment", -1, 7),
        ])
        moves = svc.list_movements(self.conn, self.pen["id"])
        self.assertEqual(moves[1]["order_id"], o1["id"])
        self.assertEqual(moves[3]["order_id"], o1["id"])
        self.assert_ledger_matches_stock()

    def test_failed_order_logs_nothing(self):
        with self.assertRaises(svc.InsufficientStock):
            svc.place_order(self.conn, "Asha", [
                {"product_id": self.pen["id"], "quantity": 2},
                {"product_id": self.book["id"], "quantity": 99},
            ])
        self.assertEqual(self.history(self.pen["id"]), [("initial", 10, 10)])

    def test_update_without_stock_change_logs_nothing(self):
        svc.update_product(self.conn, self.pen["id"], price_paise=2000)
        svc.update_product(self.conn, self.pen["id"], stock_qty=10)  # same value
        self.assertEqual(len(svc.list_movements(self.conn, self.pen["id"])), 1)

    def test_delete_product_removes_its_history(self):
        svc.restock(self.conn, self.pen["id"], 3)
        svc.delete_product(self.conn, self.pen["id"])
        n = self.conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE product_id = ?",
            (self.pen["id"],)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_ledger_holds_after_mixed_activity(self):
        o = svc.place_order(self.conn, "A", [
            {"product_id": self.pen["id"], "quantity": 3},
            {"product_id": self.book["id"], "quantity": 3},
        ])
        svc.restock(self.conn, self.book["id"], 10)
        svc.cancel_order(self.conn, o["id"])
        svc.place_order(self.conn, "B", [{"product_id": self.book["id"], "quantity": 7}])
        self.assert_ledger_matches_stock()


if __name__ == "__main__":
    unittest.main()
