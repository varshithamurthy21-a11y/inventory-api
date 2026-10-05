# Inventory & Order Management API

A small warehouse backend: products with stock counts, multi-product orders that
decrement stock all-or-nothing, cancellation that returns stock, and a low-stock view.
Built with FastAPI and SQLite.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000/docs** for interactive API docs where you can try every endpoint.

## Test it

```bash
python -m unittest -v tests.test_services   # business rules, no installs needed
pytest -v                                   # full suite incl. HTTP tests
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| POST | `/products` | Create product |
| GET | `/products` | List products |
| GET | `/products/low-stock?threshold=5` | Products with `stock_qty < threshold` |
| GET | `/products/{id}` | One product |
| PATCH | `/products/{id}` | Update any of name / price / stock |
| DELETE | `/products/{id}` | Delete (409 if it appears in an order) |
| POST | `/products/{id}/restock` | Add stock, with an optional note |
| GET | `/products/{id}/movements` | Every stock change for this product, with reason |
| POST | `/orders` | Place order with several items |
| GET | `/orders` | List orders with totals |
| GET | `/orders/{id}` | Order + line items + total |
| POST | `/orders/{id}/cancel` | Cancel and return stock (409 if already cancelled) |

Example order:

```json
POST /orders
{ "customer_name": "Asha",
  "items": [ {"product_id": 1, "quantity": 4}, {"product_id": 2, "quantity": 2} ] }
```

If stock is short, the response is `400` and lists **every** short product:

```json
{ "detail": "Insufficient stock: Notebook (requested 99, available 3)",
  "shortages": [ {"product_id": 2, "name": "Notebook", "requested": 99, "available": 3} ] }
```

## Stock movement history

Every change to a product's stock writes a row to `stock_movements`, so the API
can answer "why is this 7?":

```json
GET /products/1/movements
[ {"reason": "initial",    "change": 10, "balance_after": 10},
  {"reason": "order",      "change": -4, "balance_after": 6,  "order_id": 1},
  {"reason": "order",      "change": -2, "balance_after": 4,  "order_id": 2},
  {"reason": "cancel",     "change": 4,  "balance_after": 8,  "order_id": 1},
  {"reason": "adjustment", "change": -1, "balance_after": 7} ]
```

Reasons: `initial` (product created), `restock`, `order`, `cancel`, and `adjustment`
(stock set directly with PATCH). The movement is written in the **same transaction**
as the stock change, so the history always adds up: the sum of `change` equals the
current `stock_qty`, and the tests check this.

## Design decisions

- **Check everything, then write.** `place_order` runs in one transaction with two
  phases. Phase 1 reads every line and collects all problems. Phase 2 writes only if
  nothing failed. Any error rolls back, so an order never half-succeeds.
- **`BEGIN IMMEDIATE`.** The write lock is taken before the stock check, so nothing
  else can change stock between "is there enough?" and "take it".
- **Duplicate lines are merged.** Two lines of 2 Notebooks are checked as 4. Otherwise
  each line passes on its own and together they oversell.
- **Money is integer paise.** `₹45.50` is stored as `4550`. Floats can't represent
  most decimal amounts exactly, so totals drift.
- **`unit_price_paise` is copied onto each order line.** Changing a product's price
  later doesn't change what past orders show.
- **Order totals are computed, not stored.** They're summed from the lines with SQL,
  so the total can never disagree with the lines.
- **Products in orders can't be deleted** (`ON DELETE RESTRICT` → `409`). Deleting one
  would break order history.
- **Cancelling twice is refused.** Otherwise stock would be returned twice.
- **Layering.** `services.py` holds all business rules in plain Python. `main.py` only
  validates input and maps errors to status codes (404 / 400 / 409 / 422).

## Files

```
db.py         connection + schema
services.py   business logic (products, orders, the stock rule)
main.py       FastAPI routes and request/response models
tests/        service tests (unittest) + API tests (pytest)
```