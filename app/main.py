from __future__ import annotations

import csv
import io
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .analysis import enqueue, model_available, start_worker, stop_worker
from .db import DATA_DIR, ROOT, UPLOAD_DIR, audit, db, init_db
from .security import (
    COOKIE_NAME,
    SESSION_DAYS,
    create_session,
    csrf_valid,
    destroy_session,
    get_session,
    hash_password,
    verify_password,
)

MAX_UPLOAD_BYTES = int(os.getenv("INVENTARIO_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
MAX_CHUNK_BYTES = 9 * 1024 * 1024
CHUNK_DIR = DATA_DIR / "upload-parts"
ALLOW_REGISTRATION = os.getenv("INVENTARIO_ALLOW_REGISTRATION", "true").lower() == "true"
COOKIE_SECURE = os.getenv("INVENTARIO_COOKIE_SECURE", "false").lower() == "true"
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "application/octet-stream"}
COUNTRIES = {
    "AR": "Argentina", "DE": "Alemania", "ES": "España", "US": "Estados Unidos", "UY": "Uruguay",
    "CL": "Chile", "BR": "Brasil", "MX": "México", "IT": "Italia", "FR": "Francia", "OTHER": "Otro",
}
CURRENCIES = ["ARS", "USD", "EUR", "UYU", "CLP", "BRL", "MXN", "GBP"]

TEMPLATES = Jinja2Templates(directory=str(ROOT / "app" / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    start_worker()
    yield
    stop_worker()


app = FastAPI(title="Inventario IA", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["inventory.rodrantho.com", "127.0.0.1", "localhost", "testserver"])
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    if request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def session_for(request: Request) -> dict | None:
    return get_session(request.cookies.get(COOKIE_NAME))


def require_user(request: Request) -> dict:
    session = session_for(request)
    if not session:
        raise HTTPException(status_code=401, detail="Sesión requerida")
    return session


def require_csrf(request: Request, session: dict, form_token: str | None = None) -> None:
    supplied = form_token or request.headers.get("X-CSRF-Token")
    if not csrf_valid(session, supplied):
        raise HTTPException(status_code=403, detail="Token de seguridad inválido")


def render(request: Request, template: str, *, status_code: int = 200, **context):
    session = session_for(request)
    base = {
        "request": request,
        "session": session,
        "csrf": session["csrf_token"] if session else "",
        "allow_registration": ALLOW_REGISTRATION,
        "model_available": model_available(),
        "countries": COUNTRIES,
        "currencies": CURRENCIES,
    }
    base.update(context)
    return TEMPLATES.TemplateResponse(request, template, base, status_code=status_code)


def redirect(path: str, status_code: int = 303):
    return RedirectResponse(path, status_code=status_code)


def owned_inventory(conn, inventory_id: int, user_id: int):
    row = conn.execute("SELECT * FROM inventories WHERE id=? AND user_id=?", (inventory_id, user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Inventario no encontrado")
    return row


def clean_filename(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
        suffix = ".mp4"
    return f"{secrets.token_hex(16)}{suffix}"


@app.get("/health")
def health():
    return {"status": "ok", "model": model_available()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return redirect("/app") if session_for(request) else render(request, "login.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return redirect("/app") if session_for(request) else render(request, "login.html")


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not verify_password(user["password_hash"], password):
        time.sleep(0.4)
        return render(request, "login.html", status_code=400, error="Correo o contraseña incorrectos", email=email)
    token, _ = create_session(user["id"])
    response = redirect("/app")
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", path="/",
    )
    with db() as conn:
        audit(conn, user["id"], "login", "user", user["id"])
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if not ALLOW_REGISTRATION:
        return redirect("/login")
    return redirect("/app") if session_for(request) else render(request, "register.html")


@app.post("/register")
def register(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if not ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="Registro deshabilitado")
    display_name, email = display_name.strip(), email.strip().lower()
    if len(display_name) < 2 or len(display_name) > 80:
        return render(request, "register.html", status_code=400, error="Ingresá un nombre válido", email=email)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return render(request, "register.html", status_code=400, error="Ingresá un correo válido", email=email)
    if password != password_confirm:
        return render(request, "register.html", status_code=400, error="Las contraseñas no coinciden", email=email)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return render(request, "register.html", status_code=400, error=str(exc), email=email)
    try:
        with db() as conn:
            cursor = conn.execute(
                "INSERT INTO users(email, display_name, password_hash) VALUES(?,?,?)",
                (email, display_name, password_hash),
            )
            user_id = cursor.lastrowid
            audit(conn, user_id, "register", "user", user_id)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return render(request, "register.html", status_code=400, error="Ese correo ya está registrado", email=email)
        raise
    token, _ = create_session(user_id)
    response = redirect("/app")
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    destroy_session(request.cookies.get(COOKIE_NAME))
    response = redirect("/login")
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request):
    session = session_for(request)
    if not session:
        return redirect("/login")
    with db() as conn:
        inventories = conn.execute(
            """SELECT i.*, (SELECT COUNT(*) FROM videos v WHERE v.inventory_id=i.id) video_count,
                      (SELECT COUNT(*) FROM items x WHERE x.inventory_id=i.id) item_count
               FROM inventories i WHERE i.user_id=? ORDER BY i.updated_at DESC""",
            (session["user_id"],),
        ).fetchall()
    return render(request, "dashboard.html", inventories=inventories)


@app.post("/inventories")
def create_inventory(
    request: Request,
    name: str = Form(...),
    country: str = Form(...),
    currency: str = Form(...),
    csrf_token: str = Form(...),
):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    name = name.strip()
    if not name or len(name) > 120 or country not in COUNTRIES or currency not in CURRENCIES:
        raise HTTPException(status_code=400, detail="Datos inválidos")
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO inventories(user_id, name, country, currency) VALUES(?,?,?,?)",
            (session["user_id"], name, country, currency),
        )
        inventory_id = cursor.lastrowid
        audit(conn, session["user_id"], "create", "inventory", inventory_id, name)
    return redirect(f"/inventories/{inventory_id}")


@app.get("/inventories/{inventory_id}", response_class=HTMLResponse)
def inventory_detail(request: Request, inventory_id: int):
    session = session_for(request)
    if not session:
        return redirect("/login")
    with db() as conn:
        inventory = owned_inventory(conn, inventory_id, session["user_id"])
        videos = conn.execute("SELECT * FROM videos WHERE inventory_id=? ORDER BY id DESC", (inventory_id,)).fetchall()
        items = conn.execute(
            "SELECT * FROM items WHERE inventory_id=? ORDER BY room_name COLLATE NOCASE, name COLLATE NOCASE",
            (inventory_id,),
        ).fetchall()
        totals = conn.execute(
            """SELECT COALESCE(SUM(quantity * COALESCE(final_price, suggested_price, 0)),0) total,
                      COUNT(*) rows FROM items WHERE inventory_id=?""",
            (inventory_id,),
        ).fetchone()
    return render(request, "inventory.html", inventory=inventory, videos=videos, items=items, totals=totals)


@app.post("/inventories/{inventory_id}/videos")
async def upload_video(
    request: Request,
    inventory_id: int,
    room_name: str = Form(...),
    csrf_token: str = Form(...),
    video: UploadFile = File(...),
):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    room_name = room_name.strip()
    if not room_name or len(room_name) > 80:
        raise HTTPException(status_code=400, detail="Nombre de habitación inválido")
    if video.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(status_code=415, detail="Formato de video no admitido")
    with db() as conn:
        owned_inventory(conn, inventory_id, session["user_id"])
    inventory_dir = UPLOAD_DIR / str(inventory_id)
    inventory_dir.mkdir(parents=True, exist_ok=True)
    target = inventory_dir / clean_filename(video.filename or "video.mp4")
    size = 0
    try:
        with target.open("wb") as handle:
            while chunk := await video.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="El video supera el límite permitido")
                handle.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await video.close()
    if size < 1024:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="El archivo está vacío o es inválido")
    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO videos(inventory_id, room_name, original_name, stored_path, size_bytes)
               VALUES(?,?,?,?,?)""",
            (inventory_id, room_name, (video.filename or "video")[:255], str(target), size),
        )
        conn.execute(
            "UPDATE inventories SET status='queued', progress=5, status_message='Video recibido y en cola', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (inventory_id,),
        )
        audit(conn, session["user_id"], "upload", "video", cursor.lastrowid, room_name)
    enqueue(inventory_id)
    return redirect(f"/inventories/{inventory_id}")


@app.post("/inventories/{inventory_id}/videos/start")
def start_chunked_upload(
    request: Request,
    inventory_id: int,
    room_name: str = Form(...),
    original_name: str = Form(...),
    content_type: str = Form(...),
    expected_size: int = Form(...),
    total_chunks: int = Form(...),
    csrf_token: str = Form(...),
):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    room_name = room_name.strip()
    if not room_name or len(room_name) > 80:
        raise HTTPException(status_code=400, detail="Nombre de habitación inválido")
    if content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(status_code=415, detail="Formato de video no admitido")
    if expected_size < 1024 or expected_size > MAX_UPLOAD_BYTES or total_chunks < 1 or total_chunks > 256:
        raise HTTPException(status_code=400, detail="Tamaño o cantidad de partes inválida")
    upload_id = uuid.uuid4().hex
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        owned_inventory(conn, inventory_id, session["user_id"])
        expired = conn.execute(
            "SELECT id FROM upload_sessions WHERE created_at < datetime('now','-1 day')"
        ).fetchall()
        conn.execute("DELETE FROM upload_sessions WHERE created_at < datetime('now','-1 day')")
        conn.execute(
            """INSERT INTO upload_sessions(id, inventory_id, user_id, room_name, original_name,
               content_type, expected_size, total_chunks) VALUES(?,?,?,?,?,?,?,?)""",
            (upload_id, inventory_id, session["user_id"], room_name, original_name[:255], content_type,
             expected_size, total_chunks),
        )
    for row in expired:
        shutil.rmtree(CHUNK_DIR / row["id"], ignore_errors=True)
    (CHUNK_DIR / upload_id).mkdir(mode=0o700)
    return {"upload_id": upload_id}


@app.put("/api/uploads/{upload_id}/chunks/{chunk_index}")
async def upload_chunk(request: Request, upload_id: str, chunk_index: int):
    session = require_user(request)
    require_csrf(request, session)
    with db() as conn:
        upload = conn.execute(
            "SELECT * FROM upload_sessions WHERE id=? AND user_id=?", (upload_id, session["user_id"])
        ).fetchone()
    if not upload or chunk_index < 0 or chunk_index >= upload["total_chunks"]:
        raise HTTPException(status_code=404, detail="Carga no encontrada")
    directory = CHUNK_DIR / upload_id
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{chunk_index:04d}.part.tmp"
    final = directory / f"{chunk_index:04d}.part"
    size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_CHUNK_BYTES:
                    raise HTTPException(status_code=413, detail="Parte demasiado grande")
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Parte vacía")
        temporary.replace(final)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"received": size, "index": chunk_index}


@app.post("/api/uploads/{upload_id}/complete")
def complete_chunked_upload(request: Request, upload_id: str):
    session = require_user(request)
    require_csrf(request, session)
    with db() as conn:
        upload = conn.execute(
            "SELECT * FROM upload_sessions WHERE id=? AND user_id=?", (upload_id, session["user_id"])
        ).fetchone()
        if not upload:
            raise HTTPException(status_code=404, detail="Carga no encontrada")
        owned_inventory(conn, upload["inventory_id"], session["user_id"])
    directory = CHUNK_DIR / upload_id
    parts = [directory / f"{index:04d}.part" for index in range(upload["total_chunks"])]
    if not all(part.is_file() for part in parts):
        raise HTTPException(status_code=409, detail="Faltan partes del video")
    actual_size = sum(part.stat().st_size for part in parts)
    if actual_size != upload["expected_size"]:
        raise HTTPException(status_code=409, detail="El tamaño recibido no coincide")
    inventory_dir = UPLOAD_DIR / str(upload["inventory_id"])
    inventory_dir.mkdir(parents=True, exist_ok=True)
    target = inventory_dir / clean_filename(upload["original_name"])
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("wb") as output:
            for part in parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        temporary.replace(target)
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO videos(inventory_id, room_name, original_name, stored_path, size_bytes)
                   VALUES(?,?,?,?,?)""",
                (upload["inventory_id"], upload["room_name"], upload["original_name"], str(target), actual_size),
            )
            conn.execute("DELETE FROM upload_sessions WHERE id=?", (upload_id,))
            conn.execute(
                "UPDATE inventories SET status='queued', progress=5, status_message='Video recibido y en cola', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (upload["inventory_id"],),
            )
            audit(conn, session["user_id"], "upload", "video", cursor.lastrowid, upload["room_name"])
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    shutil.rmtree(directory, ignore_errors=True)
    enqueue(upload["inventory_id"])
    return {"ok": True, "location": f"/inventories/{upload['inventory_id']}"}


@app.get("/api/inventories/{inventory_id}/status")
def inventory_status(request: Request, inventory_id: int):
    session = require_user(request)
    with db() as conn:
        inventory = owned_inventory(conn, inventory_id, session["user_id"])
        item_count = conn.execute("SELECT COUNT(*) count FROM items WHERE inventory_id=?", (inventory_id,)).fetchone()["count"]
    return {
        "status": inventory["status"], "progress": inventory["progress"],
        "message": inventory["status_message"], "item_count": item_count,
    }


@app.post("/inventories/{inventory_id}/items")
def add_item(
    request: Request,
    inventory_id: int,
    room_name: str = Form(...),
    name: str = Form(...),
    quantity: int = Form(1),
    final_price: str = Form(""),
    description: str = Form(""),
    csrf_token: str = Form(...),
):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    with db() as conn:
        inventory = owned_inventory(conn, inventory_id, session["user_id"])
        try:
            price = float(final_price.replace(",", ".")) if final_price.strip() else None
        except ValueError:
            raise HTTPException(status_code=400, detail="Precio inválido")
        if not room_name.strip() or not name.strip() or quantity < 1 or quantity > 10000 or (price is not None and price < 0):
            raise HTTPException(status_code=400, detail="Datos inválidos")
        cursor = conn.execute(
            """INSERT INTO items(inventory_id, room_name, name, description, quantity, final_price, currency, created_by)
               VALUES(?,?,?,?,?,?,?,'manual')""",
            (inventory_id, room_name.strip()[:80], name.strip()[:120], description.strip()[:500], quantity, price,
             inventory["currency"]),
        )
        audit(conn, session["user_id"], "create", "item", cursor.lastrowid, name)
    return redirect(f"/inventories/{inventory_id}#items")


@app.post("/items/{item_id}/update")
def update_item(
    request: Request,
    item_id: int,
    room_name: str = Form(...),
    name: str = Form(...),
    quantity: int = Form(...),
    final_price: str = Form(""),
    description: str = Form(""),
    csrf_token: str = Form(...),
):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    with db() as conn:
        item = conn.execute(
            """SELECT x.* FROM items x JOIN inventories i ON i.id=x.inventory_id
               WHERE x.id=? AND i.user_id=?""", (item_id, session["user_id"]),
        ).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Objeto no encontrado")
        try:
            price = float(final_price.replace(",", ".")) if final_price.strip() else None
        except ValueError:
            raise HTTPException(status_code=400, detail="Precio inválido")
        if not room_name.strip() or not name.strip() or quantity < 1 or quantity > 10000 or (price is not None and price < 0):
            raise HTTPException(status_code=400, detail="Datos inválidos")
        conn.execute(
            """UPDATE items SET room_name=?, name=?, description=?, quantity=?, final_price=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (room_name.strip()[:80], name.strip()[:120], description.strip()[:500], quantity, price, item_id),
        )
        audit(conn, session["user_id"], "update", "item", item_id, name)
    return redirect(f"/inventories/{item['inventory_id']}#items")


@app.post("/items/{item_id}/delete")
def delete_item(request: Request, item_id: int, csrf_token: str = Form(...)):
    session = require_user(request)
    require_csrf(request, session, csrf_token)
    with db() as conn:
        item = conn.execute(
            """SELECT x.* FROM items x JOIN inventories i ON i.id=x.inventory_id
               WHERE x.id=? AND i.user_id=?""", (item_id, session["user_id"]),
        ).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Objeto no encontrado")
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        audit(conn, session["user_id"], "delete", "item", item_id, item["name"])
    return redirect(f"/inventories/{item['inventory_id']}#items")


def export_rows(inventory_id: int, user_id: int):
    with db() as conn:
        inventory = owned_inventory(conn, inventory_id, user_id)
        items = conn.execute(
            "SELECT * FROM items WHERE inventory_id=? ORDER BY room_name, name", (inventory_id,)
        ).fetchall()
    return inventory, items


@app.get("/inventories/{inventory_id}/export.csv")
def export_csv(request: Request, inventory_id: int):
    session = require_user(request)
    inventory, items = export_rows(inventory_id, session["user_id"])
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Habitación", "Objeto", "Descripción", "Cantidad", "Precio unitario", "Moneda", "Subtotal", "Fuente"])
    for item in items:
        price = item["final_price"] if item["final_price"] is not None else item["suggested_price"]
        writer.writerow([
            item["room_name"], item["name"], item["description"], item["quantity"], price or "",
            item["currency"], (price * item["quantity"]) if price is not None else "", item["source_url"] or "",
        ])
    data = "\ufeff" + output.getvalue()
    return Response(data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="inventario-{inventory_id}.csv"'
    })


@app.get("/inventories/{inventory_id}/export.xlsx")
def export_xlsx(request: Request, inventory_id: int):
    session = require_user(request)
    inventory, items = export_rows(inventory_id, session["user_id"])
    book = Workbook()
    sheet = book.active
    sheet.title = "Inventario"
    sheet.append([inventory["name"], COUNTRIES.get(inventory["country"], inventory["country"]), inventory["currency"]])
    sheet.append([])
    sheet.append(["Habitación", "Objeto", "Descripción", "Cantidad", "Precio unitario", "Moneda", "Subtotal", "Fuente"])
    for item in items:
        price = item["final_price"] if item["final_price"] is not None else item["suggested_price"]
        sheet.append([
            item["room_name"], item["name"], item["description"], item["quantity"], price,
            item["currency"], price * item["quantity"] if price is not None else None, item["source_url"],
        ])
    sheet.freeze_panes = "A4"
    sheet.auto_filter.ref = f"A3:H{max(3, sheet.max_row)}"
    widths = [20, 28, 38, 10, 16, 10, 16, 48]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[chr(64 + index)].width = width
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": f'attachment; filename="inventario-{inventory_id}.xlsx"'
    })


@app.get("/inventories/{inventory_id}/export.pdf")
def export_pdf(request: Request, inventory_id: int):
    session = require_user(request)
    inventory, items = export_rows(inventory_id, session["user_id"])
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    story = [Paragraph(inventory["name"], styles["Title"]),
             Paragraph(f"{COUNTRIES.get(inventory['country'], inventory['country'])} · {inventory['currency']}", styles["Normal"]),
             Spacer(1, 7 * mm)]
    rows = [["Habitación", "Objeto", "Cant.", "Precio", "Subtotal"]]
    total = 0.0
    for item in items:
        price = item["final_price"] if item["final_price"] is not None else item["suggested_price"]
        subtotal = (price or 0) * item["quantity"]
        total += subtotal
        rows.append([item["room_name"], item["name"], str(item["quantity"]),
                     f"{price:,.2f}" if price is not None else "—", f"{subtotal:,.2f}" if price is not None else "—"])
    table = Table(rows, colWidths=[34 * mm, 62 * mm, 16 * mm, 28 * mm, 28 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172554")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
    ]))
    story.extend([table, Spacer(1, 5 * mm), Paragraph(f"Total: {inventory['currency']} {total:,.2f}", styles["Heading2"])])
    document.build(story)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="inventario-{inventory_id}.pdf"'
    })
