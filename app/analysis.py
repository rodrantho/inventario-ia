from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from .db import MODEL_DIR, db
from .pricing import estimate_used_price
from .semantic import SemanticAnalyzer, semantic_model_available

MODEL_PATH = Path(os.getenv("INVENTARIO_YOLO_MODEL", MODEL_DIR / "yolo11n.onnx"))
MAX_FRAMES = int(os.getenv("INVENTARIO_MAX_FRAMES", "24"))
DETECTION_CONFIDENCE = float(os.getenv("INVENTARIO_DETECTION_CONFIDENCE", "0.38"))

COCO = [
    "persona", "bicicleta", "auto", "motocicleta", "avión", "autobús", "tren", "camión", "barco",
    "semáforo", "hidrante", "señal de stop", "parquímetro", "banco", "pájaro", "gato", "perro",
    "caballo", "oveja", "vaca", "elefante", "oso", "cebra", "jirafa", "mochila", "paraguas",
    "cartera", "corbata", "valija", "frisbee", "esquís", "snowboard", "pelota", "cometa",
    "bate", "guante", "skateboard", "tabla de surf", "raqueta", "botella", "copa", "taza",
    "tenedor", "cuchillo", "cuchara", "bol", "banana", "manzana", "sándwich", "naranja",
    "brócoli", "zanahoria", "hot dog", "pizza", "dona", "torta", "silla", "sofá", "planta en maceta",
    "cama", "mesa de comedor", "inodoro", "televisor", "notebook", "mouse", "control remoto",
    "teclado", "teléfono", "microondas", "horno", "tostadora", "lavamanos", "heladera", "libro",
    "reloj", "florero", "tijera", "oso de peluche", "secador de pelo", "cepillo de dientes",
]

# Clases razonables para un inventario doméstico. Se excluyen personas, vehículos y alimentos.
HOUSEHOLD_IDS = {
    *range(24, 34), *range(39, 49), 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68,
    69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79,
}

_jobs: queue.Queue[int] = queue.Queue()
_worker: threading.Thread | None = None
_stop = threading.Event()


def model_available() -> bool:
    return MODEL_PATH.is_file()


def enqueue(inventory_id: int) -> None:
    _jobs.put(inventory_id)


def recover_jobs() -> None:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT inventory_id FROM videos WHERE status IN ('queued','processing')"
        ).fetchall()
        conn.execute("UPDATE videos SET status='queued' WHERE status='processing'")
    for row in rows:
        enqueue(row["inventory_id"])


def start_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_worker_loop, name="inventory-video-worker", daemon=True)
    _worker.start()
    recover_jobs()


def stop_worker() -> None:
    _stop.set()
    _jobs.put(-1)
    if _worker:
        _worker.join(timeout=5)


def _worker_loop() -> None:
    while not _stop.is_set():
        try:
            inventory_id = _jobs.get(timeout=1)
        except queue.Empty:
            continue
        if inventory_id < 0:
            break
        try:
            process_inventory(inventory_id)
        except Exception as exc:
            with db() as conn:
                conn.execute(
                    "UPDATE inventories SET status='error', status_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"Error de procesamiento: {str(exc)[:300]}", inventory_id),
                )
        finally:
            _jobs.task_done()


def _run(command: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def extract_frames(video_path: Path, work_dir: Path) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    for old in work_dir.glob("frame-*.jpg"):
        old.unlink()
    probe = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
        str(video_path),
    ])
    try:
        duration = max(1.0, float(probe.stdout.strip()))
    except ValueError:
        duration = 60.0
    interval = max(2.0, duration / MAX_FRAMES)
    output = work_dir / "frame-%04d.jpg"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
        "-vf", f"fps=1/{interval:.3f},scale='min(960,iw)':-2", "-frames:v", str(MAX_FRAMES),
        "-q:v", "3", str(output),
    ])
    return sorted(work_dir.glob("frame-*.jpg"))


def _nms(boxes: list[list[int]], scores: list[float]) -> list[int]:
    if not boxes:
        return []
    result = cv2.dnn.NMSBoxes(boxes, scores, DETECTION_CONFIDENCE, 0.45)
    if result is None:
        return []
    return np.array(result).reshape(-1).astype(int).tolist()


def detect_frame(session: ort.InferenceSession, frame_path: Path) -> list[tuple[int, float]]:
    image = cv2.imread(str(frame_path))
    if image is None:
        return []
    height, width = image.shape[:2]
    blob = cv2.dnn.blobFromImage(image, 1 / 255.0, (640, 640), swapRB=True, crop=False)
    input_name = session.get_inputs()[0].name
    output = session.run(None, {input_name: blob})[0]
    predictions = np.squeeze(output)
    if predictions.ndim != 2:
        return []
    if predictions.shape[0] < predictions.shape[1] and predictions.shape[0] in (84, 85):
        predictions = predictions.T

    boxes: list[list[int]] = []
    scores: list[float] = []
    class_ids: list[int] = []
    x_factor, y_factor = width / 640.0, height / 640.0
    for row in predictions:
        classes = row[4:]
        class_id = int(np.argmax(classes))
        score = float(classes[class_id])
        if score < DETECTION_CONFIDENCE or class_id not in HOUSEHOLD_IDS:
            continue
        cx, cy, w, h = map(float, row[:4])
        left = int((cx - w / 2) * x_factor)
        top = int((cy - h / 2) * y_factor)
        boxes.append([left, top, int(w * x_factor), int(h * y_factor)])
        scores.append(score)
        class_ids.append(class_id)
    return [(class_ids[i], scores[i]) for i in _nms(boxes, scores)]


def detect_video(frames: list[Path]) -> dict[str, tuple[int, float]]:
    if not model_available():
        return {}
    session = ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])
    counts_by_frame: list[Counter[int]] = []
    confidences: defaultdict[int, list[float]] = defaultdict(list)
    for frame in frames:
        detections = detect_frame(session, frame)
        counts = Counter(class_id for class_id, _ in detections)
        counts_by_frame.append(counts)
        for class_id, score in detections:
            confidences[class_id].append(score)
    result: dict[str, tuple[int, float]] = {}
    all_ids = set().union(*(set(c) for c in counts_by_frame)) if counts_by_frame else set()
    for class_id in all_ids:
        # Máximo simultáneo observado: evita contar el mismo objeto en cada fotograma.
        quantity = max(c.get(class_id, 0) for c in counts_by_frame)
        confidence = max(confidences[class_id])
        result[COCO[class_id]] = (max(1, quantity), confidence)
    return result


def process_inventory(inventory_id: int) -> None:
    with db() as conn:
        inventory = conn.execute("SELECT * FROM inventories WHERE id=?", (inventory_id,)).fetchone()
        videos = conn.execute(
            "SELECT * FROM videos WHERE inventory_id=? AND status='queued' ORDER BY id", (inventory_id,)
        ).fetchall()
        if not inventory or not videos:
            return
        conn.execute(
            "UPDATE inventories SET status='processing', progress=10, status_message='Preparando videos', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (inventory_id,),
        )

    total = len(videos)
    had_error = False
    semantic: SemanticAnalyzer | None = None
    semantic_failed = False
    for index, video in enumerate(videos):
        video_id = video["id"]
        base_progress = 10 + int((index / total) * 75)
        try:
            with db() as conn:
                conn.execute("UPDATE videos SET status='processing', error=NULL WHERE id=?", (video_id,))
                conn.execute(
                    "UPDATE inventories SET progress=?, status_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (base_progress, f"Analizando {video['room_name']}", inventory_id),
                )
            video_path = Path(video["stored_path"])
            frames_dir = video_path.parent / f"frames-{video_id}"
            frames = extract_frames(video_path, frames_dir)
            yolo_detections = detect_video(frames)
            detections: dict[str, tuple[int, float, str]] = {}
            if semantic_model_available() and not semantic_failed:
                try:
                    with db() as conn:
                        conn.execute(
                            "UPDATE inventories SET status_message='Ejecutando análisis visual detallado; esta etapa puede demorar', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (inventory_id,),
                        )
                    if semantic is None:
                        semantic = SemanticAnalyzer()
                    detections.update(semantic.detect(frames, frames_dir))
                except Exception:
                    semantic_failed = True
            for name, (quantity, confidence) in yolo_detections.items():
                existing_detection = detections.get(name)
                if existing_detection:
                    detections[name] = (max(existing_detection[0], quantity), max(existing_detection[1], confidence),
                                        existing_detection[2])
                else:
                    detections[name] = (quantity, confidence, "Identificado por el detector de objetos.")
            with db() as conn:
                conn.execute(
                    "UPDATE inventories SET status_message='Buscando precios usados comparables', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (inventory_id,),
                )
            priced_detections = [
                (name, quantity, confidence, description,
                 estimate_used_price(name, inventory["country"], inventory["currency"]))
                for name, (quantity, confidence, description) in detections.items()
            ]
            with db() as conn:
                for name, quantity, confidence, description, estimate in priced_detections:
                    existing = conn.execute(
                        "SELECT id, quantity FROM items WHERE inventory_id=? AND room_name=? AND name=? AND created_by='ai'",
                        (inventory_id, video["room_name"], name),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            """UPDATE items SET quantity=?, confidence=?, description=?, suggested_price=?, price_min=?,
                               price_max=?, currency=?, source_url=?, source_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (max(existing["quantity"], quantity), confidence, description, estimate.suggested,
                             estimate.low, estimate.high, inventory["currency"], estimate.source_url,
                             estimate.note, existing["id"]),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO items(inventory_id, room_name, name, description, quantity, confidence,
                               suggested_price, price_min, price_max, currency, source_url, source_note, created_by)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'ai')""",
                            (inventory_id, video["room_name"], name, description, quantity, confidence,
                             estimate.suggested, estimate.low, estimate.high, inventory["currency"],
                             estimate.source_url, estimate.note),
                        )
                conn.execute(
                    "UPDATE videos SET status='complete', processed_at=CURRENT_TIMESTAMP WHERE id=?", (video_id,)
                )
            shutil.rmtree(frames_dir, ignore_errors=True)
        except Exception as exc:
            had_error = True
            with db() as conn:
                conn.execute("UPDATE videos SET status='error', error=? WHERE id=?", (str(exc)[:500], video_id))

    if semantic is not None:
        semantic.close()

    with db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) count FROM videos WHERE inventory_id=? AND status IN ('queued','processing')",
            (inventory_id,),
        ).fetchone()["count"]
        if remaining:
            enqueue(inventory_id)
            return
        item_count = conn.execute("SELECT COUNT(*) count FROM items WHERE inventory_id=?", (inventory_id,)).fetchone()["count"]
        if had_error:
            message = f"Procesamiento terminado con errores. {item_count} objetos en revisión"
            status = "review"
        elif not model_available():
            message = "Video procesado. Modelo visual no instalado: agregá objetos manualmente o instalalo"
            status = "review"
        else:
            message = f"Análisis terminado: {item_count} objetos para revisar"
            status = "review"
        conn.execute(
            "UPDATE inventories SET status=?, progress=100, status_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, message, inventory_id),
        )
