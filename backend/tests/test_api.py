from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from importlib import import_module
from typing import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def api_client(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Provide a TestClient wired to an isolated SQLite database."""
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    db_path = tmp_path_factory.mktemp("data") / "test_materials.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    for module in ("backend.main", "backend.load_shipments", "backend.database", "backend.orders"):
        sys.modules.pop(module, None)

    import_module("backend.database")
    import_module("backend.load_shipments")
    app_module = import_module("backend.main")

    with TestClient(app_module.app) as client:
        yield client

    if db_path.exists():
        os.remove(db_path)


def test_health_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "running"}


def test_material_lifecycle(api_client: TestClient) -> None:
    payload = {
        "type": "Integration Test Material",
        "humidity": 1.5,
        "density": 0.87,
        "weight": 250.0,
    }
    create_response = api_client.post("/api/materials", json=payload)
    assert create_response.status_code == 201, create_response.text
    material = create_response.json()
    assert material["type"] == payload["type"]
    assert pytest.approx(material["weight"], rel=1e-2) == payload["weight"]
    assert material["fill_ratio"] > 0
    assert "consumption_std_tons" in material

    adjust_payload = {"delta": -50.0, "reason": "Test deduction"}
    adjust_response = api_client.post(f"/api/materials/{material['id']}/adjust", json=adjust_payload)
    assert adjust_response.status_code == 200
    adjusted = adjust_response.json()
    assert pytest.approx(adjusted["weight"], rel=1e-2) == payload["weight"] + adjust_payload["delta"]


def test_delivery_creation_normalizes_status(api_client: TestClient) -> None:
    materials_response = api_client.get("/api/materials")
    assert materials_response.status_code == 200
    materials = materials_response.json()
    assert materials, "Expected default materials to be seeded"

    target_material = materials[0]
    delivery_payload = {
        "delivery_num": "TEST-DELIVERY-001",
        "material_code": target_material["type"],
        "incoming_weight_lb": 48000,
        "delivery_time": datetime.now(timezone.utc).isoformat(),
        "status": "Completed",
    }

    create_response = api_client.post("/api/deliveries", json=delivery_payload)
    assert create_response.status_code == 201, create_response.text
    delivery = create_response.json()
    assert delivery["delivery_num"] == delivery_payload["delivery_num"]
    assert delivery["status"] == "completed"
    assert "T" in delivery["delivery_time"]

    list_response = api_client.get("/api/deliveries")
    assert list_response.status_code == 200
    deliveries = list_response.json()
    assert any(item["delivery_num"] == delivery_payload["delivery_num"] for item in deliveries)


def test_orders_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/orders")
    assert response.status_code == 200
    orders = response.json()
    assert orders, "Expected seeded orders"
    first = orders[0]
    assert {"order_id", "material", "required_tons", "status"}.issubset(first.keys())


def test_material_aliases_map_to_seeded_rows(api_client: TestClient) -> None:
    materials_response = api_client.get("/api/materials")
    assert materials_response.status_code == 200
    materials = materials_response.json()

    sms_materials = [item for item in materials if item["type"] in {"SMS", "SMS Clay"}]
    assert len(sms_materials) == 1
    assert sms_materials[0]["type"] == "SMS Clay"
    assert pytest.approx(sms_materials[0]["open_orders_tons"], rel=1e-2) == 150.0

    seed_response = api_client.post(
        "/api/demo/seed",
        json={
            "inventory": [{"material": "SMS", "weight": 321.0}],
            "reset_deliveries": False,
            "reset_history": True,
        },
    )
    assert seed_response.status_code == 200, seed_response.text
    seeded_materials = seed_response.json()
    sms_materials = [item for item in seeded_materials if item["type"] in {"SMS", "SMS Clay"}]
    assert len(sms_materials) == 1
    assert sms_materials[0]["type"] == "SMS Clay"


def test_delivery_alias_reuses_existing_material(api_client: TestClient) -> None:
    delivery_payload = {
        "delivery_num": "TEST-DELIVERY-SMS-ALIAS",
        "material_code": "SMS",
        "incoming_weight_lb": 40000,
        "delivery_time": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
    }

    create_response = api_client.post("/api/deliveries", json=delivery_payload)
    assert create_response.status_code == 201, create_response.text

    materials_response = api_client.get("/api/materials")
    assert materials_response.status_code == 200
    materials = materials_response.json()
    sms_materials = [item for item in materials if item["type"] in {"SMS", "SMS Clay"}]
    assert len(sms_materials) == 1
    assert sms_materials[0]["type"] == "SMS Clay"
