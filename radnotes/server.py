"""Local web app. Binds to loopback; every API call needs the per-launch token."""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .notes import ChangedElsewhere, Library
from .pairing import Invitation, is_device_code, pair_with
from .radicle import RadError
from .render import render
from .store import NotFound

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "testserver"}
SYNC_AFTER_EDIT = 8      # seconds of quiet before changes are sent
SYNC_EVERY = 120         # seconds between checks for changes from other devices


class TextIn(BaseModel):
    text: str = ""
    base: str | None = None


class CodeIn(BaseModel):
    code: str


class NameIn(BaseModel):
    name: str


def create_app(lib: Library, token: str | None = None, autosync: bool = True) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    app = FastAPI(title="RadNotes", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.lib = lib
    state = {"edited": None, "last": None, "invitation": None}

    def background():
        while True:
            now = time.time()
            quiet = state["edited"] and now - state["edited"] > SYNC_AFTER_EDIT
            if state["last"] is None or quiet or now - state["last"] > SYNC_EVERY:
                state["edited"] = None
                state["last"] = now
                lib.sync_all()
            time.sleep(2)

    if autosync:
        threading.Thread(target=background, daemon=True).start()

    def touched():
        state["edited"] = time.time()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if ":" in host and not host.endswith("]"):
            host = host[: host.rfind(":")]
        if host not in LOOPBACK:
            return JSONResponse({"detail": "RadNotes only answers on localhost"}, 403)
        if request.url.path.startswith("/api/") and request.headers.get("x-radnotes-token") != token:
            return JSONResponse({"detail": "Reload the page"}, 401)
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(NotFound)
    async def _nf(_, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(ValueError)
    async def _bad(_, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(RadError)
    async def _rad(_, exc):
        return JSONResponse({"detail": str(exc)}, 502)

    @app.exception_handler(ChangedElsewhere)
    async def _changed(_, exc):
        return JSONResponse({"detail": str(exc), "current": exc.current}, 409)

    @app.get("/", response_class=HTMLResponse)
    def index():
        stamp = str(int(max(f.stat().st_mtime for f in STATIC.iterdir())))
        return (STATIC / "index.html").read_text("utf-8").replace("{{TOKEN}}", token).replace("{{V}}", stamp)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def spaces():
        return [{"id": sp.id, "title": sp.title, "code": sp.code} for sp in lib.spaces()]

    # notes ---------------------------------------------------------------------
    @app.get("/api/notes")
    def list_notes(q: str = "", space: str = ""):
        notes = lib.get(space)
        return {"notes": notes.search(q) if q else notes.list(), "status": notes.status(),
                "spaces": spaces(), "title": notes.title}

    @app.post("/api/notes")
    def new_note(body: TextIn, space: str = ""):
        notes = lib.get(space)
        note = notes.read(notes.create(body.text))
        touched()
        return note

    @app.get("/api/notes/{note_id}")
    def read_note(note_id: str, space: str = ""):
        return lib.get(space).read(note_id)

    @app.put("/api/notes/{note_id}")
    def write_note(note_id: str, body: TextIn, space: str = ""):
        result = lib.get(space).write(note_id, body.text, body.base)
        touched()
        return result

    @app.delete("/api/notes/{note_id}")
    def delete_note(note_id: str, space: str = ""):
        lib.get(space).delete(note_id)
        touched()
        return {"ok": True}

    @app.post("/api/render")
    def render_note(body: TextIn):
        return {"html": render(body.text)}

    @app.post("/api/sync")
    def sync_now(space: str = ""):
        state["edited"], state["last"] = None, time.time()
        return {**lib.sync_all(), "status": lib.get(space).status()}

    # sharing --------------------------------------------------------------------
    @app.post("/api/pair")
    def pair_start():
        old = state["invitation"]
        if old and old.active:
            return {"code": old.code, "state": old.state}
        inv = Invitation(lib.personal).start()
        state["invitation"] = inv
        return {"code": inv.code, "state": inv.state}

    @app.get("/api/pair")
    def pair_status():
        inv = state["invitation"]
        return {"code": inv.code, "state": inv.state, "paired": inv.paired} if inv else {"state": "none"}

    @app.post("/api/join")
    def join(body: CodeIn):
        if is_device_code(body.code):
            r = pair_with(lib.personal, body.code)
            return {"kind": "device", "kept": r["kept"]}
        sp = lib.join_space(body.code)
        return {"kind": "space", "space": sp.id, "title": sp.title}

    @app.post("/api/spaces")
    def create_space(body: NameIn):
        sp = lib.create_space(body.name)
        return {"id": sp.id, "title": sp.title, "code": sp.code}

    return app
