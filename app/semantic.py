from __future__ import annotations

import gc
import json
import os
import re
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

MODEL_ID = "vikhyatk/moondream2"
MODEL_REVISION = "2025-06-21"
HF_HOME = os.getenv("HF_HOME", str(Path(__file__).resolve().parents[1] / "models" / "hf"))

TRANSLATIONS = {
    "sofa": "sofá", "couch": "sofá", "armchair": "sillón", "chair": "silla", "chairs": "sillas",
    "coffee table": "mesa de centro", "side table": "mesa auxiliar", "dining table": "mesa de comedor",
    "table": "mesa", "bed": "cama", "nightstand": "mesa de luz", "bedside table": "mesa de luz",
    "wardrobe": "ropero", "closet": "ropero", "cabinet": "armario", "dresser": "cómoda",
    "bookshelf": "biblioteca", "shelf": "estantería", "wall-mounted shelves": "estanterías de pared",
    "lamp": "lámpara", "floor lamp": "lámpara de pie", "desk lamp": "lámpara de escritorio",
    "mirror": "espejo", "artwork": "cuadro", "wall-mounted artwork": "cuadro", "wall mounted artwork": "cuadro", "painting": "cuadro", "picture": "cuadro",
    "sculpture": "escultura", "sculptures": "esculturas", "statue": "estatua", "vase": "florero", "plant": "planta",
    "rug": "alfombra", "carpet": "alfombra", "curtains": "cortinas", "television": "televisor",
    "tv": "televisor", "computer": "computadora", "laptop": "notebook", "monitor": "monitor",
    "keyboard": "teclado", "mouse": "mouse", "speaker": "parlante", "speakers": "parlantes",
    "refrigerator": "heladera", "fridge": "heladera", "microwave": "microondas", "oven": "horno",
    "toaster": "tostadora", "washing machine": "lavarropas", "dishwasher": "lavavajillas",
    "bottle": "botella", "bottles": "botellas", "glass": "vaso", "glasses": "vasos",
    "cup": "taza", "cups": "tazas", "plate": "plato", "plates": "platos", "bowl": "bol",
    "book": "libro", "books": "libros", "clock": "reloj", "fan": "ventilador",
    "vacuum cleaner": "aspiradora", "desk": "escritorio", "bench": "banco", "stool": "taburete",
    "ottoman": "puf", "basket": "canasto", "pillow": "almohadón", "pillows": "almohadones",
}
EXCLUDED = {
    "wall", "walls", "floor", "wooden floor", "ceiling", "door", "window", "windows", "room",
    "light fixture", "staircase", "wooden staircase", "stairs",
}


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()


def _spanish_name(value: str) -> str:
    cleaned = re.sub(r"^\s*\d+\s*[x×-]?\s*", "", value).strip(" .:-").casefold()
    return TRANSLATIONS.get(_key(cleaned), cleaned)


def _montage(frames: list[Path], output: Path, maximum: int = 6) -> Path:
    if not frames:
        raise ValueError("El video no produjo fotogramas")
    candidate_indexes = np.linspace(0, len(frames) - 1, min(maximum * 2, len(frames)), dtype=int).tolist()
    images: list[Image.Image] = []
    fingerprints: list[np.ndarray] = []
    for index in candidate_indexes:
        image = Image.open(frames[index]).convert("RGB")
        fingerprint = np.asarray(image.resize((32, 32)).convert("L"), dtype=np.float32)
        if fingerprints and min(float(np.mean(np.abs(fingerprint - old))) for old in fingerprints) < 9.0:
            image.close()
            continue
        images.append(image)
        fingerprints.append(fingerprint)
        if len(images) >= maximum:
            break
    if not images:
        images = [Image.open(frames[0]).convert("RGB")]
    if len(images) == 1:
        images[0].thumbnail((1280, 960), Image.Resampling.LANCZOS)
        images[0].save(output, quality=90, optimize=True)
        images[0].close()
        return output
    cell_width, cell_height = 640, 420
    columns = 2
    rows = (len(images) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(sheet)
    for position, image in enumerate(images):
        image.thumbnail((cell_width, cell_height - 28), Image.Resampling.LANCZOS)
        x = (position % columns) * cell_width + (cell_width - image.width) // 2
        y = (position // columns) * cell_height + 28
        sheet.paste(image, (x, y))
        draw.text(((position % columns) * cell_width + 10, (position // columns) * cell_height + 7),
                  f"Vista {position + 1}", fill="black")
    sheet.save(output, quality=88, optimize=True)
    for image in images:
        image.close()
    return output


def _parse_answer(answer: str) -> dict[str, tuple[int, float, str]]:
    match = re.search(r"\[[\s\S]*\]", answer)
    values: list[object]
    if match:
        try:
            parsed = json.loads(match.group(0))
            values = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            values = []
    else:
        values = []
    if not values:
        values = [part.strip() for part in re.split(r"[,;\n]", answer) if part.strip()]
    result: dict[str, tuple[int, float, str]] = {}
    for entry in values:
        quantity = 1
        description = "Identificado mediante análisis visual detallado."
        if isinstance(entry, dict):
            raw_name = str(entry.get("name") or entry.get("nombre") or "")
            try:
                quantity = max(1, min(99, int(entry.get("quantity") or entry.get("cantidad") or 1)))
            except (TypeError, ValueError):
                quantity = 1
            details = [str(entry.get(key, "")).strip() for key in ("material", "color")]
            details = [detail for detail in details if detail and detail.casefold() not in {"unknown", "desconocido", "n/a"}]
            if details:
                description = ", ".join(details).capitalize()
        else:
            raw_name = str(entry)
            amount = re.match(r"\s*(\d+)\s*[x×-]?\s+", raw_name)
            if amount:
                quantity = max(1, min(99, int(amount.group(1))))
        if not raw_name or _key(raw_name) in EXCLUDED:
            continue
        name = _spanish_name(raw_name)
        if not name or len(name) > 80:
            continue
        existing = result.get(name)
        if existing:
            result[name] = (min(99, existing[0] + quantity), max(existing[1], 0.72), existing[2])
        else:
            result[name] = (quantity, 0.72, description)
    return result


class SemanticAnalyzer:
    def __init__(self) -> None:
        os.environ.setdefault("HF_HOME", HF_HOME)
        import torch
        from transformers import AutoModelForCausalLM

        torch.set_num_threads(max(1, min(6, (os.cpu_count() or 4) - 1)))
        self._torch = torch
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            revision=MODEL_REVISION,
            trust_remote_code=True,
            device_map={"": "cpu"},
            dtype=torch.float32,
            local_files_only=True,
        )

    def detect(self, frames: list[Path], work_dir: Path) -> dict[str, tuple[int, float, str]]:
        montage = _montage(frames, work_dir / "montage.jpg")
        image = Image.open(montage).convert("RGB")
        prompt = (
            "List every visible movable household item that could be inventoried or sold. "
            "If the image contains panels with repeated views, count each physical object only once. "
            "Return only a JSON array. Each element must have name in Spanish, quantity as integer, "
            "material if visible, color if visible. Do not include walls, floors, ceilings, doors, "
            "windows, people, or permanent fixtures."
        )
        try:
            answer = self.model.query(image, prompt, reasoning=False)["answer"]
            return _parse_answer(answer)
        finally:
            image.close()

    def close(self) -> None:
        self.model = None
        gc.collect()


def semantic_model_available() -> bool:
    cache = Path(HF_HOME) / "hub" / "models--vikhyatk--moondream2"
    return cache.is_dir()
