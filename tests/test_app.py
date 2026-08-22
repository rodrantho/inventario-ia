from __future__ import annotations

import re

from fastapi.testclient import TestClient


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_user_inventory_item_and_exports(tmp_path, monkeypatch):
    from app import db as dbmod
    from app import main

    monkeypatch.setattr(dbmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(dbmod, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "CHUNK_DIR", tmp_path / "parts")
    monkeypatch.setattr(main, "COOKIE_SECURE", False)
    monkeypatch.setattr(main, "ALLOW_REGISTRATION", True)

    with TestClient(main.app, follow_redirects=True) as client:
        response = client.post("/register", data={
            "display_name": "Prueba", "email": "test@example.com",
            "password": "una-clave-segura", "password_confirm": "una-clave-segura",
        })
        assert response.status_code == 200
        assert "Mis inventarios" in response.text
        token = csrf(response.text)

        response = client.post("/inventories", data={
            "name": "Departamento", "country": "AR", "currency": "USD", "csrf_token": token,
        }, follow_redirects=False)
        assert response.status_code == 303
        inventory_url = response.headers["location"]

        detail = client.get(inventory_url)
        assert detail.status_code == 200
        token = csrf(detail.text)

        payload = b"x" * 2048
        started = client.post(f"{inventory_url}/videos/start", data={
            "room_name": "Living", "original_name": "recorrido.mp4", "content_type": "video/mp4",
            "expected_size": str(len(payload)), "total_chunks": "2", "csrf_token": token,
        })
        assert started.status_code == 200
        upload_id = started.json()["upload_id"]
        for index, chunk in enumerate((payload[:1024], payload[1024:])):
            part = client.put(f"/api/uploads/{upload_id}/chunks/{index}", content=chunk,
                              headers={"X-CSRF-Token": token, "Content-Type": "application/octet-stream"})
            assert part.status_code == 200
        completed = client.post(f"/api/uploads/{upload_id}/complete", headers={"X-CSRF-Token": token})
        assert completed.status_code == 200
        assert completed.json()["location"] == inventory_url

        response = client.post(f"{inventory_url}/items", data={
            "room_name": "Living", "name": "Silla", "quantity": "2", "final_price": "25.50",
            "description": "Madera", "csrf_token": token,
        }, follow_redirects=True)
        assert response.status_code == 200
        assert "Silla" in response.text
        assert "USD 51.00" in response.text

        inventory_id = int(inventory_url.rsplit("/", 1)[1])
        with dbmod.db() as conn:
            conn.execute(
                """INSERT INTO items(inventory_id,room_name,name,quantity,confidence,suggested_price,
                   price_min,price_max,currency,source_note,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (inventory_id, "Living", "Sofá", 1, 0.72, 60, 40, 80, "USD", "5 comparables", "ai"),
            )
        priced = client.get(inventory_url)
        assert "USD 60.00" in priced.text
        assert "40.00–80.00" in priced.text

        assert client.get(f"{inventory_url}/export.csv").status_code == 200
        xlsx = client.get(f"{inventory_url}/export.xlsx")
        assert xlsx.status_code == 200
        assert xlsx.content.startswith(b"PK")
        pdf = client.get(f"{inventory_url}/export.pdf")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")


def test_health(tmp_path, monkeypatch):
    from app import db as dbmod
    from app import main
    monkeypatch.setattr(dbmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test-health.db")
    monkeypatch.setattr(dbmod, "UPLOAD_DIR", tmp_path / "uploads")
    with TestClient(main.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
