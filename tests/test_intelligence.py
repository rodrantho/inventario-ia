from app.pricing import _parse_number, _summarize
from app.semantic import _parse_answer
from app.pricing import PriceEstimate


def test_processing_persists_inline_price(tmp_path, monkeypatch):
    import app.db as dbmod
    from app import analysis

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "intelligence.db")
    monkeypatch.setattr(analysis, "extract_frames", lambda *_: [])
    monkeypatch.setattr(analysis, "detect_video", lambda *_: {"silla": (1, 0.8)})
    monkeypatch.setattr(analysis, "semantic_model_available", lambda: False)
    monkeypatch.setattr(
        analysis,
        "estimate_used_price",
        lambda *_: PriceEstimate(50, 35, 70, "https://example.test/item", "3 comparables", "media", 3),
    )
    dbmod.init_db()
    video = tmp_path / "room.mp4"
    video.write_bytes(b"test")
    with dbmod.db() as conn:
        user_id = conn.execute("INSERT INTO users(email,display_name,password_hash) VALUES('a@b.test','A','x')").lastrowid
        inventory_id = conn.execute(
            "INSERT INTO inventories(user_id,name,country,currency,status) VALUES(?,?,?,?,?)",
            (user_id, "Casa", "DE", "EUR", "queued"),
        ).lastrowid
        conn.execute(
            "INSERT INTO videos(inventory_id,room_name,original_name,stored_path,size_bytes,status) VALUES(?,?,?,?,?,'queued')",
            (inventory_id, "Living", "room.mp4", str(video), 4),
        )
    analysis.process_inventory(inventory_id)
    with dbmod.db() as conn:
        item = conn.execute("SELECT suggested_price,price_min,price_max,source_note FROM items").fetchone()
    assert tuple(item) == (50, 35, 70, "3 comparables")


def test_semantic_inventory_normalizes_and_excludes_structure():
    result = _parse_answer('["sofa", "wall-mounted artwork", "wooden floor", "2 chairs", "statue"]')
    assert result["sofá"][0] == 1
    assert result["cuadro"][0] == 1
    assert result["sillas"][0] == 2
    assert result["estatua"][0] == 1
    assert "wooden floor" not in result


def test_price_normalization_and_summary():
    assert _parse_number("23.000", "ARS") == 23000
    assert _parse_number("1.299,50", "EUR") == 1299.5
    estimate = _summarize([20, 40, 60, 80, 100], "https://example.test/item", "example.test")
    assert estimate.suggested == 60
    assert estimate.low == 40
    assert estimate.high == 80
    assert estimate.comparable_count == 5
