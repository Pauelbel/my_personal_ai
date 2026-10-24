"""Раздел «Чат»: проекты, сессии, история, ход агента (SSE), решение по карточке, «Стоп», выбор папки."""

import json
import os
import string
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from meepo.agent.loop import TurnError

router = APIRouter(prefix="/api")


class ProjectBody(BaseModel):
    name: str = ""
    path: str


class SessionBody(BaseModel):
    project_id: str | None = None


class TitleBody(BaseModel):
    title: str


class TurnBody(BaseModel):
    content: str = ""
    browser_mode: Literal["inspect", "interact"] = "inspect"
    # Картинки, вставленные в реплику чата вики: имена из POST /api/attachments.
    attachments: list[str] = []


class DecisionBody(BaseModel):
    action: str
    text: str = ""
    trust: bool = False
    card_id: str = ""
    # Карточка со списком: номера отмеченных пунктов.
    selected: list[int] | None = None


def _services(request: Request):
    return request.app.state.services


def _session(request: Request, session_id: str) -> dict:
    session = _services(request).sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "Сессия не найдена")
    return session


# ---------- проекты ----------

@router.get("/projects")
def list_projects(request: Request) -> list[dict]:
    return _services(request).projects.list()


@router.post("/projects", status_code=201)
def create_project(body: ProjectBody, request: Request) -> dict:
    try:
        return _services(request).projects.create(body.name, body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/projects/{project_id}")
def update_project(project_id: str, body: ProjectBody, request: Request) -> dict:
    try:
        project = _services(request).projects.update(project_id, body.name, body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if project is None:
        raise HTTPException(404, "Проект не найден")
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request) -> None:
    """Проект удаляется вместе с его сессиями; папка на диске остаётся."""
    services = _services(request)
    sessions = [session for session in services.sessions.list() if session["project_id"] == project_id]
    if any(services.turns.running(session["id"]) for session in sessions):
        raise HTTPException(409, "В сессии проекта идёт ход: сначала остановите его")
    if not services.projects.delete(project_id):
        raise HTTPException(404, "Проект не найден")
    for session in sessions:
        services.sessions.delete(session["id"])


@router.get("/folders")
def list_folders(path: str = "") -> dict:
    """Папки для выбора папки проекта: браузер не отдаёт странице настоящий путь, поэтому их показывает сервер.
    Только имена папок — не файлы. Без path — корни: домашняя папка и диски."""
    if not path:
        home = Path.home()
        roots = [{"name": f"Домашняя папка ({home.name})", "path": str(home)}]
        if os.name == "nt":
            roots += [{"name": f"Диск {letter}:", "path": f"{letter}:\\"}
                      for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
        else:
            roots.append({"name": "/", "path": "/"})
        return {"path": None, "parent": None, "folders": roots}
    folder = Path(path).expanduser()
    try:
        folder = folder.resolve(strict=True)
        children = sorted((entry.name for entry in os.scandir(folder)
                           if not entry.name.startswith((".", "$")) and entry.is_dir()), key=str.casefold)
    except PermissionError as exc:
        raise HTTPException(403, "Нет доступа к этой папке") from exc
    except OSError as exc:
        raise HTTPException(404, "Папка не найдена") from exc
    parent = str(folder.parent) if folder.parent != folder else None
    return {"path": str(folder), "parent": parent,
            "folders": [{"name": name, "path": str(folder / name)} for name in children[:1000]]}


# ---------- сессии ----------

@router.get("/sessions")
def list_sessions(request: Request) -> list[dict]:
    """Сессии проектов; служебная сессия чата вики видна только в разделе «Вики»."""
    return [session for session in _services(request).sessions.list() if session.get("kind", "chat") == "chat"]


@router.post("/sessions", status_code=201)
def create_session(body: SessionBody, request: Request) -> dict:
    services = _services(request)
    if body.project_id is not None and services.projects.get(body.project_id) is None:
        raise HTTPException(404, "Проект не найден")
    return services.sessions.create(body.project_id)


@router.put("/sessions/{session_id}/title")
def rename_session(session_id: str, body: TitleBody, request: Request) -> dict:
    _session(request, session_id)
    return _services(request).sessions.update(session_id, title=body.title.strip() or "Без названия")


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> None:
    services = _services(request)
    if services.turns.running(session_id):
        raise HTTPException(409, "В сессии идёт ход: сначала остановите его")
    if not services.sessions.delete(session_id):
        raise HTTPException(404, "Сессия не найдена")
    await services.browsers.close(session_id)


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, request: Request) -> list[dict]:
    _session(request, session_id)
    return _services(request).sessions.messages(session_id)


# ---------- ход ----------

@router.post("/sessions/{session_id}/turn")
async def start_turn(session_id: str, body: TurnBody, request: Request) -> StreamingResponse:
    services = _services(request)
    session = _session(request, session_id)
    if not body.content.strip() and not body.attachments:
        raise HTTPException(400, "Пустое сообщение")
    if missing := [name for name in body.attachments if services.attachments.path(name) is None]:
        raise HTTPException(400, f"Картинки не найдены: {', '.join(missing)} — вставьте их заново")
    agent = services.wiki_agent if session.get("kind") == "wiki" else services.agent
    request_id = uuid4().hex
    content, attachments = body.content.strip(), tuple(body.attachments)
    try:
        turn = services.turns.start(
            session_id, lambda emit: agent.run(session_id, content, request_id, emit, attachments, body.browser_mode))
    except TurnError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _follow(services.turns, turn, 0)


@router.get("/sessions/{session_id}/turn")
async def turn_state(session_id: str, request: Request) -> dict:
    turn = _services(request).turns.running(session_id)
    return {"running": turn is not None}


@router.get("/sessions/{session_id}/turn/stream")
async def follow_turn(session_id: str, request: Request, start: int = 0) -> StreamingResponse:
    """Подключиться к идущему ходу, например после перезагрузки страницы: события с номера start."""
    turns = _services(request).turns
    turn = turns.running(session_id)
    if turn is None:
        raise HTTPException(404, "Ход не идёт")
    return _follow(turns, turn, max(start, 0))


@router.post("/sessions/{session_id}/turn/stop", status_code=204)
async def stop_turn(session_id: str, request: Request) -> None:
    if not _services(request).turns.stop(session_id):
        raise HTTPException(404, "Ход не идёт")


@router.post("/sessions/{session_id}/approve", status_code=204)
async def approve(session_id: str, body: DecisionBody, request: Request) -> None:
    if not _services(request).approvals.decide(session_id, body.model_dump()):
        raise HTTPException(409, "Ход уже не ждёт этого решения")


def _follow(turns, turn, start: int) -> StreamingResponse:
    async def events():
        async for event in turns.follow(turn, start):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
