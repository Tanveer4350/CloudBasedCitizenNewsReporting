from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = ROOT / "uploads"
DATABASE = Path(os.environ.get("NEWS_DATABASE", ROOT / "news.db"))
SESSION_SECRET = os.environ.get("NEWS_SECRET", secrets.token_hex(32)).encode()
SESSION_SECRET_CONFIGURED = bool(os.environ.get("NEWS_SECRET"))
SESSION_SECONDS = 60 * 60 * 24 * 7
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "civic-press-media")
logger = logging.getLogger("civic_press")
ALLOWED_MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}
CATEGORIES = {
    "Community",
    "Education",
    "Environment",
    "Health",
    "Local Government",
    "Public Safety",
    "Transport",
    "Other",
}


class SupabaseError(Exception):
    def __init__(self, message: str, status_code: int = 503, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def cloud_mode() -> bool:
    if bool(SUPABASE_URL) != bool(SUPABASE_SERVICE_ROLE_KEY):
        raise RuntimeError(
            "Set both SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, or unset both."
        )
    if SUPABASE_URL and len(SESSION_SECRET) < 32:
        raise RuntimeError("NEWS_SECRET must contain at least 32 characters in cloud mode.")
    if SUPABASE_URL and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,61}", SUPABASE_BUCKET):
        raise RuntimeError("SUPABASE_BUCKET has an invalid name.")
    if SUPABASE_URL and not SESSION_SECRET_CONFIGURED:
        raise RuntimeError("Set a persistent NEWS_SECRET before enabling cloud mode.")
    return bool(SUPABASE_URL)


def supabase_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | list | None = None,
    content: bytes | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    request_headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }
    if headers:
        request_headers.update(headers)
    try:
        response = httpx.request(
            method,
            f"{SUPABASE_URL}{path}",
            params=params,
            json=json_body,
            content=content,
            headers=request_headers,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        logger.exception("Supabase request failed")
        raise SupabaseError(
            "Could not connect to Supabase. Check the project URL and network."
        ) from exc
    if response.is_error:
        try:
            error = response.json()
        except ValueError:
            error = {}
        message = error.get("message") or error.get("error") or "Supabase request failed."
        code = error.get("code")
        logger.error("Supabase request failed (%s): %s", response.status_code, message)
        raise SupabaseError(str(message), response.status_code, code)
    return response


def cloud_select(
    table: str,
    *,
    filters: dict[str, str] | None = None,
    order: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    params = {"select": "*"}
    for column, value in (filters or {}).items():
        params[column] = f"eq.{value}"
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    if offset is not None:
        params["offset"] = str(offset)
    response = supabase_request("GET", f"/rest/v1/{table}", params=params)
    return response.json()


def cloud_insert(table: str, record: dict) -> dict:
    response = supabase_request(
        "POST",
        f"/rest/v1/{table}",
        json_body=record,
        headers={"Prefer": "return=representation"},
    )
    rows = response.json()
    if not rows:
        raise SupabaseError(f"Supabase did not return the inserted {table} record.")
    return rows[0]


def cloud_update(table: str, record_id: str, values: dict) -> None:
    supabase_request(
        "PATCH",
        f"/rest/v1/{table}",
        params={"id": f"eq.{record_id}"},
        json_body=values,
        headers={"Prefer": "return=minimal"},
    )


def upload_cloud_media(object_path: str, media_type: str, content: bytes) -> None:
    supabase_request(
        "POST",
        f"/storage/v1/object/{SUPABASE_BUCKET}/{object_path}",
        content=content,
        headers={
            "Content-Type": media_type,
            "x-upsert": "false",
        },
    )


def delete_cloud_media(object_path: str) -> None:
    supabase_request(
        "DELETE",
        f"/storage/v1/object/{SUPABASE_BUCKET}/{object_path}",
    )


def get_cloud_media(object_path: str) -> bytes:
    response = supabase_request(
        "GET",
        f"/storage/v1/object/{SUPABASE_BUCKET}/{object_path}",
    )
    return response.content


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    if cloud_mode():
        cloud_select("users", limit=1)
        bucket = supabase_request(
            "GET", f"/storage/v1/bucket/{SUPABASE_BUCKET}"
        ).json()
        if bucket.get("public") is not False:
            raise RuntimeError("The Supabase media bucket must be private.")
        return
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('citizen', 'editor', 'admin')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                reporter_id TEXT NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                category TEXT NOT NULL,
                location TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                editor_note TEXT NOT NULL DEFAULT '',
                editor_id TEXT REFERENCES users(id),
                media_name TEXT,
                media_type TEXT,
                media_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reports_status_created
                ON reports(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS reports_reporter_created
                ON reports(reporter_id, created_at DESC);
            """
        )
        user_table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if user_table and "'admin'" not in user_table["sql"]:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                BEGIN;
                CREATE TABLE users_new (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('citizen', 'editor', 'admin')),
                    created_at TEXT NOT NULL
                );
                INSERT INTO users_new (id, email, password_hash, role, created_at)
                    SELECT id, email, password_hash, role, created_at FROM users;
                DROP TABLE users;
                ALTER TABLE users_new RENAME TO users;
                COMMIT;
                """
            )
            connection.execute("PRAGMA foreign_keys = ON")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, digest_hex = stored_hash.split(":", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 310_000
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_session(user_id: str) -> str:
    payload = f"{user_id}.{int(datetime.now(timezone.utc).timestamp()) + SESSION_SECONDS}"
    signature = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def session_user_id(token: str | None) -> str | None:
    if not token:
        return None
    try:
        user_id, expiry, signature = token.rsplit(".", 2)
        payload = f"{user_id}.{expiry}"
        expected = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if int(expiry) < int(datetime.now(timezone.utc).timestamp()):
            return None
        if not hmac.compare_digest(signature, expected):
            return None
        return user_id
    except (ValueError, TypeError):
        return None


def public_user(row: dict | sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


def get_current_user(request: Request) -> dict:
    user_id = session_user_id(request.cookies.get("news_session"))
    if not user_id:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    if cloud_mode():
        users = cloud_select("users", filters={"id": user_id}, limit=1)
        user = users[0] if users else None
    else:
        with database() as connection:
            user = connection.execute(
                "SELECT id, email, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Your session is no longer valid.")
    return public_user(user)


def require_editor(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] not in {"editor", "admin"}:
        raise HTTPException(status_code=403, detail="Editor access is required.")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


def serialize_report(row: dict | sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "category": row["category"],
        "location": row["location"],
        "status": row["status"],
        "editor_note": row["editor_note"],
        "media_url": f"/uploads/{row['media_path']}" if row["media_path"] else None,
        "media_type": row["media_type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class RegisterInput(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=128)


class LoginInput(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class ReviewInput(BaseModel):
    action: Literal["approve", "reject"]
    title: str | None = Field(default=None, min_length=3, max_length=120)
    body: str | None = Field(default=None, min_length=20, max_length=5000)
    category: str | None = None
    location: str | None = Field(default=None, min_length=2, max_length=120)
    editor_note: str = Field(default="", max_length=1000)


@asynccontextmanager
async def lifespan(_: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database()
    yield


app = FastAPI(
    title="Civic Press",
    description="A community reporting and editorial verification platform.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(SupabaseError)
async def supabase_error_handler(_: Request, exc: SupabaseError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                f"Cloud storage request failed: {exc}. Check the Supabase setup "
                "instructions in README.md."
            )
        },
    )


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    if cloud_mode():
        cloud_select("users", limit=1)
        supabase_request("GET", f"/storage/v1/bucket/{SUPABASE_BUCKET}")
    else:
        with database() as connection:
            connection.execute("SELECT 1")
    return {"status": "ok", "storage": "supabase" if cloud_mode() else "local"}


@app.get("/uploads/{filename}", include_in_schema=False)
def report_media(filename: str, request: Request):
    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp|mp4|webm)", filename):
        raise HTTPException(status_code=404, detail="Media not found.")
    if cloud_mode():
        reports = cloud_select(
            "reports",
            filters={"media_path": filename},
            limit=1,
        )
        report = reports[0] if reports else None
        if not report:
            raise HTTPException(status_code=404, detail="Media not found.")
        if report["status"] != "approved":
            user_id = session_user_id(request.cookies.get("news_session"))
            users = cloud_select("users", filters={"id": user_id}, limit=1) if user_id else []
            user = users[0] if users else None
            if not user or (
                user["id"] != report["reporter_id"]
                and user["role"] not in {"editor", "admin"}
            ):
                raise HTTPException(status_code=404, detail="Media not found.")
        return FastAPIResponse(
            get_cloud_media(filename),
            media_type=report["media_type"],
            headers={
                "Cache-Control": (
                    "public, max-age=3600"
                    if report["status"] == "approved"
                    else "private, no-store"
                ),
                "Content-Disposition": "inline",
            },
        )

    with database() as connection:
        report = connection.execute(
            "SELECT reporter_id, status, media_type FROM reports WHERE media_path = ?",
            (filename,),
        ).fetchone()
        if not report or not (UPLOAD_DIR / filename).is_file():
            raise HTTPException(status_code=404, detail="Media not found.")
        if report["status"] != "approved":
            user_id = session_user_id(request.cookies.get("news_session"))
            user = connection.execute(
                "SELECT id, role FROM users WHERE id = ?", (user_id,)
            ).fetchone() if user_id else None
            if not user or (
                user["id"] != report["reporter_id"]
                and user["role"] not in {"editor", "admin"}
            ):
                raise HTTPException(status_code=404, detail="Media not found.")
        cache_control = (
            "public, max-age=3600"
            if report["status"] == "approved"
            else "private, no-store"
        )
        return FileResponse(
            UPLOAD_DIR / filename,
            media_type=report["media_type"],
            headers={"Cache-Control": cache_control, "Content-Disposition": "inline"},
        )


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterInput, response: Response):
    email = payload.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    user_id = uuid.uuid4().hex
    if cloud_mode():
        try:
            cloud_insert(
                "users",
                {
                    "id": user_id,
                    "email": email,
                    "password_hash": hash_password(payload.password),
                    "role": "citizen",
                    "created_at": now_iso(),
                },
            )
        except SupabaseError as exc:
            if exc.code == "23505" or exc.status_code == 409:
                raise HTTPException(
                    status_code=409, detail="That email is already registered."
                ) from exc
            raise
    else:
        with database() as connection:
            try:
                connection.execute(
                    "INSERT INTO users (id, email, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, 'citizen', ?)",
                    (user_id, email, hash_password(payload.password), now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="That email is already registered.") from exc
    response_out = {"user": {"id": user_id, "email": email, "role": "citizen"}}
    response.set_cookie(
        "news_session",
        create_session(user_id),
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE") == "1",
        samesite="strict",
        max_age=SESSION_SECONDS,
        path="/",
    )
    return response_out


@app.post("/api/auth/login")
def login(payload: LoginInput, response: Response):
    if cloud_mode():
        users = cloud_select(
            "users", filters={"email": payload.email.strip().lower()}, limit=1
        )
        user = users[0] if users else None
    else:
        with database() as connection:
            user = connection.execute(
                "SELECT id, email, password_hash, role FROM users WHERE email = ?",
                (payload.email.strip().lower(),),
            ).fetchone()
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    response.set_cookie(
        "news_session",
        create_session(user["id"]),
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE") == "1",
        samesite="strict",
        max_age=SESSION_SECONDS,
        path="/",
    )
    return {"user": public_user(user)}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("news_session", path="/", samesite="strict")
    return {"status": "signed out"}


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}


@app.get("/api/news")
def published_news(
    q: str = "",
    category: str = "",
    location: str = "",
):
    if cloud_mode():
        filters = {"status": "approved"}
        if category.strip() and category in CATEGORIES:
            filters["category"] = category
        rows = cloud_select(
            "reports",
            filters=filters,
            order="created_at.desc",
            limit=100,
        )
        if location.strip():
            rows = [
                row for row in rows
                if location.casefold() in row["location"].casefold()
            ]
        if q.strip():
            term = q.strip().casefold()
            rows = [
                row for row in rows
                if term in row["title"].casefold() or term in row["body"].casefold()
            ]
        return {"reports": [serialize_report(row) for row in rows]}

    conditions = ["status = 'approved'"]
    parameters: list[str] = []
    if q.strip():
        term = f"%{q.strip()}%"
        conditions.append("(title LIKE ? OR body LIKE ?)")
        parameters.extend((term, term))
    if category.strip() and category in CATEGORIES:
        conditions.append("category = ?")
        parameters.append(category)
    if location.strip():
        conditions.append("location LIKE ?")
        parameters.append(f"%{location.strip()}%")
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM reports WHERE "
            + " AND ".join(conditions)
            + " ORDER BY created_at DESC LIMIT 100",
            parameters,
        ).fetchall()
    return {"reports": [serialize_report(row) for row in rows]}


@app.post("/api/reports", status_code=201)
async def submit_report(
    title: str = Form(min_length=3, max_length=120),
    body: str = Form(min_length=20, max_length=5000),
    category: str = Form(),
    location: str = Form(min_length=2, max_length=120),
    media: UploadFile | None = File(default=None),
    user: dict = Depends(get_current_user),
):
    title = title.strip()
    body = body.strip()
    location = location.strip()
    if not title or not body or not location:
        raise HTTPException(status_code=422, detail="Title, story, and location are required.")
    if category not in CATEGORIES:
        raise HTTPException(status_code=422, detail="Choose a valid news category.")

    media_name = media_type = media_path = None
    media_content = None
    media_uploaded = False
    if media and media.filename:
        media_type = (media.content_type or "").lower()
        suffix = ALLOWED_MEDIA_TYPES.get(media_type)
        if not suffix:
            raise HTTPException(status_code=415, detail="Upload a JPG, PNG, WebP, MP4, or WebM file.")
        content = await media.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Media files must be 8 MB or smaller.")
        media_path = f"{uuid.uuid4().hex}{suffix}"
        media_name = Path(media.filename).name[:255]
        if cloud_mode():
            media_content = content
        else:
            (UPLOAD_DIR / media_path).write_bytes(content)

    report_id = uuid.uuid4().hex
    timestamp = now_iso()
    if cloud_mode():
        try:
            if media_path and media_type and media_content is not None:
                await run_in_threadpool(
                    upload_cloud_media, media_path, media_type, media_content
                )
                media_uploaded = True
            await run_in_threadpool(
                cloud_insert,
                "reports",
                {
                    "id": report_id,
                    "reporter_id": user["id"],
                    "title": title,
                    "body": body,
                    "category": category,
                    "location": location,
                    "status": "pending",
                    "editor_note": "",
                    "media_name": media_name,
                    "media_type": media_type,
                    "media_path": media_path,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        except SupabaseError:
            if media_path and media_uploaded:
                try:
                    await run_in_threadpool(delete_cloud_media, media_path)
                except SupabaseError:
                    logger.exception("Could not clean up an unreferenced media upload")
            raise
    else:
        try:
            with database() as connection:
                connection.execute(
                    """INSERT INTO reports
                       (id, reporter_id, title, body, category, location, status,
                        media_name, media_type, media_path, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                    (
                        report_id, user["id"], title, body, category, location,
                        media_name, media_type, media_path, timestamp, timestamp,
                    ),
                )
        except sqlite3.Error:
            if media_path:
                (UPLOAD_DIR / media_path).unlink(missing_ok=True)
            raise
    return {"id": report_id, "status": "pending", "message": "Your report is in the editor review queue."}


@app.get("/api/my-reports")
def my_reports(user: dict = Depends(get_current_user)):
    if cloud_mode():
        rows = cloud_select(
            "reports",
            filters={"reporter_id": user["id"]},
            order="created_at.desc",
        )
        return {"reports": [serialize_report(row) for row in rows]}
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM reports WHERE reporter_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return {"reports": [serialize_report(row) for row in rows]}


@app.get("/api/editor/reports")
def editor_reports(
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
    _: dict = Depends(require_editor),
):
    if cloud_mode():
        filters = {} if status == "all" else {"status": status}
        rows = cloud_select(
            "reports",
            filters=filters,
            order="created_at.desc",
            limit=200,
        )
        return {"reports": [serialize_report(row) for row in rows]}
    with database() as connection:
        query = "SELECT * FROM reports"
        parameters: tuple[str, ...] = ()
        if status != "all":
            query += " WHERE status = ?"
            parameters = (status,)
        rows = connection.execute(
            query + " ORDER BY created_at DESC LIMIT 200", parameters
        ).fetchall()
    return {"reports": [serialize_report(row) for row in rows]}


@app.get("/api/admin/reports")
def admin_reports(
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
    _: dict = Depends(require_admin),
):
    if cloud_mode():
        filters = {} if status == "all" else {"status": status}
        rows = []
        offset = 0
        while True:
            page = cloud_select(
                "reports",
                filters=filters,
                order="created_at.desc",
                limit=200,
                offset=offset,
            )
            rows.extend(page)
            if len(page) < 200:
                break
            offset += len(page)
        return {"reports": [serialize_report(row) for row in rows]}
    with database() as connection:
        query = "SELECT * FROM reports"
        parameters: tuple[str, ...] = ()
        if status != "all":
            query += " WHERE status = ?"
            parameters = (status,)
        rows = connection.execute(
            query + " ORDER BY created_at DESC", parameters
        ).fetchall()
    return {"reports": [serialize_report(row) for row in rows]}


def update_report_review(report_id: str, payload: ReviewInput, reviewer: dict) -> dict:
    if cloud_mode():
        rows = cloud_select("reports", filters={"id": report_id}, limit=1)
        row = rows[0] if rows else None
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        title = payload.title.strip() if payload.title is not None else row["title"]
        body = payload.body.strip() if payload.body is not None else row["body"]
        category = payload.category if payload.category is not None else row["category"]
        location = payload.location.strip() if payload.location is not None else row["location"]
        if not title or not body or not location:
            raise HTTPException(status_code=422, detail="Title, story, and location cannot be blank.")
        if category not in CATEGORIES:
            raise HTTPException(status_code=422, detail="Choose a valid news category.")
        status = "approved" if payload.action == "approve" else "rejected"
        cloud_update(
            "reports",
            report_id,
            {
                "title": title,
                "body": body,
                "category": category,
                "location": location,
                "status": status,
                "editor_note": payload.editor_note.strip(),
                "editor_id": reviewer["id"],
                "updated_at": now_iso(),
            },
        )
        return {"id": report_id, "status": status}

    with database() as connection:
        row = connection.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Report not found.")
        title = payload.title.strip() if payload.title is not None else row["title"]
        body = payload.body.strip() if payload.body is not None else row["body"]
        category = payload.category if payload.category is not None else row["category"]
        location = payload.location.strip() if payload.location is not None else row["location"]
        if not title or not body or not location:
            raise HTTPException(status_code=422, detail="Title, story, and location cannot be blank.")
        if category not in CATEGORIES:
            raise HTTPException(status_code=422, detail="Choose a valid news category.")
        status = "approved" if payload.action == "approve" else "rejected"
        connection.execute(
            """UPDATE reports
               SET title = ?, body = ?, category = ?, location = ?, status = ?,
                   editor_note = ?, editor_id = ?, updated_at = ?
               WHERE id = ?""",
            (
                title, body, category, location, status,
                payload.editor_note.strip(), reviewer["id"], now_iso(), report_id,
            ),
        )
    return {"id": report_id, "status": status}


@app.patch("/api/editor/reports/{report_id}")
def review_report(
    report_id: str,
    payload: ReviewInput,
    editor: dict = Depends(require_editor),
):
    return update_report_review(report_id, payload, editor)


@app.patch("/api/admin/reports/{report_id}")
def admin_review_report(
    report_id: str,
    payload: ReviewInput,
    admin: dict = Depends(require_admin),
):
    return update_report_review(report_id, payload, admin)


def create_staff_account(role: Literal["editor", "admin"]) -> int:
    initialize_database()
    label = "Admin" if role == "admin" else "Editor"
    email = input(f"{label} email: ").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        print("Enter a valid email address.", file=sys.stderr)
        return 1
    password = getpass(f"{label} password (8+ characters): ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1
    if cloud_mode():
        try:
            cloud_insert(
                "users",
                {
                    "id": uuid.uuid4().hex,
                    "email": email,
                    "password_hash": hash_password(password),
                    "role": role,
                    "created_at": now_iso(),
                },
            )
        except SupabaseError as exc:
            if exc.code == "23505" or exc.status_code == 409:
                print("An account with that email already exists.", file=sys.stderr)
                return 1
            raise
    else:
        with database() as connection:
            try:
                connection.execute(
                    "INSERT INTO users (id, email, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, email, hash_password(password), role, now_iso()),
                )
            except sqlite3.IntegrityError:
                print("An account with that email already exists.", file=sys.stderr)
                return 1
    print(f"{label} account created for {email}.")
    return 0


def create_editor_account() -> int:
    return create_staff_account("editor")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Civic Press local news platform.")
    parser.add_argument("--create-editor", action="store_true", help="Create an editor login.")
    parser.add_argument("--create-admin", action="store_true", help="Create an administrator login.")
    arguments = parser.parse_args()
    if arguments.create_editor:
        raise SystemExit(create_editor_account())
    if arguments.create_admin:
        raise SystemExit(create_staff_account("admin"))
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
