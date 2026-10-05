"""HTTP layer. Thin on purpose: validate input, call services, map errors.

Run:  uvicorn main:app --reload
Docs: http://127.0.0.1:8000/docs
"""
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import db
import services as svc

# One shared connection guarded by a lock. Simple and correct for SQLite
# in a small app: requests touching the DB run one at a time.
_lock = threading.Lock()
conn: sqlite3.Connection | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global conn
    conn = db.connect(app.state.db_path)
    db.init_db(conn)
    yield
    conn.close()


app = FastAPI(title="Inventory & Order Management", lifespan=lifespan)
app.state.db_path = db.DB_PATH


# ---------------------------- schemas ----------------------------

class ProductIn(BaseModel):
    name: str = Field(min_length=1)
    price_paise: int = Field(ge=0, description="Price in paise (₹1 = 100)")
    stock_qty: int = Field(ge=0)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    price_paise: int | None = Field(default=None, ge=0)
    stock_qty: int | None = Field(default=None, ge=0)


class Product(ProductIn):
    id: int


class RestockIn(BaseModel):
    quantity: int = Field(gt=0)
    note: str | None = None


class Movement(BaseModel):
    id: int
    product_id: int
    change: int
    balance_after: int
    reason: str
    order_id: int | None
    note: str | None
    created_at: str


class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class OrderIn(BaseModel):
    customer_name: str = Field(min_length=1)
    items: list[OrderItemIn] = Field(min_length=1)


class OrderLine(BaseModel):
    id: int
    product_id: int
    product_name: str
    quantity: int
    unit_price_paise: int
    line_total_paise: int


class OrderSummary(BaseModel):
    id: int
    customer_name: str
    created_at: str
    status: str
    total_paise: int


class OrderDetail(OrderSummary):
    items: list[OrderLine]


# ------------------------- error mapping -------------------------

@app.exception_handler(svc.NotFound)
async def _not_found(_: Request, exc: svc.NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(svc.Conflict)
async def _conflict(_: Request, exc: svc.Conflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(svc.InsufficientStock)
async def _short(_: Request, exc: svc.InsufficientStock):
    # 400 naming every product that was short, so the client can fix the
    # whole order in one go.
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "shortages": exc.shortages},
    )


@app.exception_handler(ValueError)
async def _bad_value(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------------------- products ----------------------------
# NOTE: /products/low-stock must be declared before /products/{product_id},
# otherwise FastAPI tries to parse "low-stock" as an id.

@app.post("/products", response_model=Product, status_code=201)
def create_product(body: ProductIn):
    with _lock:
        return svc.create_product(conn, **body.model_dump())


@app.get("/products", response_model=list[Product])
def list_products():
    with _lock:
        return svc.list_products(conn)


@app.get("/products/low-stock", response_model=list[Product])
def low_stock(threshold: int = Query(5, ge=0)):
    with _lock:
        return svc.low_stock(conn, threshold)


@app.get("/products/{product_id}", response_model=Product)
def get_product(product_id: int):
    with _lock:
        return svc.get_product(conn, product_id)


@app.patch("/products/{product_id}", response_model=Product)
def update_product(product_id: int, body: ProductUpdate):
    with _lock:
        return svc.update_product(conn, product_id, **body.model_dump())


@app.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: int):
    with _lock:
        svc.delete_product(conn, product_id)


@app.post("/products/{product_id}/restock", response_model=Product)
def restock(product_id: int, body: RestockIn):
    with _lock:
        return svc.restock(conn, product_id, body.quantity, body.note)


@app.get("/products/{product_id}/movements", response_model=list[Movement])
def list_movements(product_id: int):
    """Full history of stock changes; the last balance_after is the current stock."""
    with _lock:
        return svc.list_movements(conn, product_id)


# ----------------------------- orders -----------------------------

@app.post("/orders", response_model=OrderDetail, status_code=201)
def place_order(body: OrderIn):
    with _lock:
        return svc.place_order(
            conn, body.customer_name, [i.model_dump() for i in body.items]
        )


@app.get("/orders", response_model=list[OrderSummary])
def list_orders():
    with _lock:
        return svc.list_orders(conn)


@app.get("/orders/{order_id}", response_model=OrderDetail)
def get_order(order_id: int):
    with _lock:
        return svc.get_order(conn, order_id)


@app.post("/orders/{order_id}/cancel", response_model=OrderDetail)
def cancel_order(order_id: int):
    with _lock:
        return svc.cancel_order(conn, order_id)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")
