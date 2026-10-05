"""End-to-end HTTP tests. Needs: pip install -r requirements.txt
Run:  pytest -v
"""
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client(tmp_path):
    app.state.db_path = str(tmp_path / "test.db")  # fresh DB per test
    with TestClient(app) as c:  # `with` runs the startup that creates tables
        yield c


def make(client, name, price, stock):
    r = client.post("/products", json={"name": name, "price_paise": price, "stock_qty": stock})
    assert r.status_code == 201
    return r.json()["id"]


def test_product_crud(client):
    pid = make(client, "Pen", 1000, 10)
    assert client.get(f"/products/{pid}").json()["stock_qty"] == 10
    r = client.patch(f"/products/{pid}", json={"stock_qty": 7})
    assert r.json()["stock_qty"] == 7 and r.json()["name"] == "Pen"
    assert client.delete(f"/products/{pid}").status_code == 204
    assert client.get(f"/products/{pid}").status_code == 404


def test_validation(client):
    assert client.post("/products", json={"name": "X", "price_paise": -1, "stock_qty": 1}).status_code == 422
    assert client.post("/orders", json={"customer_name": "A", "items": []}).status_code == 422


def test_order_flow(client):
    pen = make(client, "Pen", 1000, 10)
    book = make(client, "Notebook", 4550, 3)

    r = client.post("/orders", json={"customer_name": "Asha", "items": [
        {"product_id": pen, "quantity": 4}, {"product_id": book, "quantity": 2}]})
    assert r.status_code == 201
    order = r.json()
    assert order["total_paise"] == 4 * 1000 + 2 * 4550
    assert client.get(f"/products/{pen}").json()["stock_qty"] == 6

    assert len(client.get("/orders").json()) == 1
    assert len(client.get(f"/orders/{order['id']}").json()["items"]) == 2

    r = client.post(f"/orders/{order['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.get(f"/products/{pen}").json()["stock_qty"] == 10
    assert client.post(f"/orders/{order['id']}/cancel").status_code == 409


def test_short_order_is_400_and_changes_nothing(client):
    pen = make(client, "Pen", 1000, 10)
    book = make(client, "Notebook", 4550, 3)
    r = client.post("/orders", json={"customer_name": "Asha", "items": [
        {"product_id": pen, "quantity": 2}, {"product_id": book, "quantity": 99}]})
    assert r.status_code == 400
    assert r.json()["shortages"][0]["name"] == "Notebook"
    assert client.get(f"/products/{pen}").json()["stock_qty"] == 10
    assert client.get("/orders").json() == []


def test_low_stock_route_not_swallowed_by_id_route(client):
    make(client, "Pen", 1000, 10)
    make(client, "Notebook", 4550, 3)
    r = client.get("/products/low-stock?threshold=5")
    assert r.status_code == 200
    assert [p["name"] for p in r.json()] == ["Notebook"]


def test_delete_product_in_order_is_409(client):
    pen = make(client, "Pen", 1000, 10)
    client.post("/orders", json={"customer_name": "A", "items": [{"product_id": pen, "quantity": 1}]})
    assert client.delete(f"/products/{pen}").status_code == 409
