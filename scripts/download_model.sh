#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="$ROOT/models/yolo11n.onnx"
URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx"
mkdir -p "$ROOT/models"
if [[ ! -s "$MODEL" ]]; then
  curl --fail --location --retry 3 --output "$MODEL.tmp" "$URL"
  mv "$MODEL.tmp" "$MODEL"
fi
python3 - "$MODEL" <<'PY'
import sys
from pathlib import Path
p=Path(sys.argv[1])
if p.stat().st_size < 1_000_000:
    raise SystemExit("modelo incompleto")
print(f"Modelo listo: {p} ({p.stat().st_size / 1048576:.1f} MB)")
PY
