from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Cookie,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import AuthStore
from .config import settings
from .content import CINEMATICS, GUIDE_STEPS, LORE, SCENARIOS, SCORE_RULES
from .game import GameManager
from .llm import LocalNarrator

COOKIE = "inh_session"
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

store = AuthStore(settings.database_path, settings.session_days)
narrator = LocalNarrator(settings.local_model_url, settings.local_model)
manager = GameManager(settings, narrator, store)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    yield
    for room in manager.rooms.values():
        if room.runner:
            room.runner.cancel()
    await asyncio.gather(
        *(room.runner for room in manager.rooms.values() if room.runner), return_exceptions=True
    )


app = FastAPI(title="I'm not a human", version="1.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = (
        "no-store"
        if request.url.path == "/" or request.url.path.startswith("/api/")
        else "public, max-age=3600"
    )
    return response


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=10, max_length=128)


class Registration(Credentials):
    nickname: str = Field(min_length=2, max_length=12)


class AccountDeletion(BaseModel):
    password: str = Field(min_length=10, max_length=128)


class RoomCreate(BaseModel):
    scenario_id: str
    mode: str = Field(pattern="^(solo|multi)$")
    password: str = Field(default="", max_length=20)
    hide_ids: bool = False


class RoomJoin(BaseModel):
    code: str = Field(min_length=4, max_length=4)
    password: str = Field(default="", max_length=20)


class RoomExit(BaseModel):
    action: str = Field(default="save", pattern="^(save|close)$")


def session_from_request(request: Request) -> tuple[dict, str] | None:
    token = request.cookies.get(COOKIE, "")
    return store.lookup_session(token, request.headers.get("user-agent", "")) if token else None


def require_user(request: Request, *, csrf: bool = False) -> tuple[dict, str]:
    session = session_from_request(request)
    if not session:
        raise HTTPException(401, "로그인이 필요합니다.")
    if csrf and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session[1]):
        raise HTTPException(403, "요청 검증 토큰이 올바르지 않습니다.")
    return session


def issue_session(response: Response, user: dict, user_agent: str) -> dict:
    token, csrf = store.create_session(user["id"], user_agent)
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"user": user, "csrf": csrf}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/privacy")
async def privacy():
    return FileResponse(STATIC / "privacy.html")


@app.get("/.well-known/assetlinks.json")
async def app_links():
    return FileResponse(STATIC / "assetlinks.json", media_type="application/json")


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "I'm not a human", "rooms": len(manager.rooms)}


@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    session = session_from_request(request)
    return {
        "authenticated": bool(session),
        "user": session[0] if session else None,
        "csrf": session[1] if session else None,
        "guide": GUIDE_STEPS,
        "scenarios": [
            {
                "id": scenario.scenario_id,
                "title": scenario.title,
                "subtitle": scenario.subtitle,
                "background": scenario.background,
                "record": scenario.record,
                "cinematic": CINEMATICS[scenario.scenario_id],
                "lore": LORE.get(scenario.scenario_id, ()),
            }
            for scenario in sorted(SCENARIOS.values(), key=lambda item: item.record)
        ],
        "score_rules": SCORE_RULES,
    }


@app.get("/api/auth/nickname-available")
async def nickname_available(nickname: str):
    try:
        available = await asyncio.to_thread(store.nickname_available, nickname)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"nickname": nickname.strip(), "available": available}


@app.post("/api/auth/register")
async def register(body: Registration, request: Request, response: Response):
    try:
        user = await asyncio.to_thread(store.register, body.username, body.password, body.nickname)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return issue_session(response, user, request.headers.get("user-agent", ""))


@app.post("/api/auth/login")
async def login(body: Credentials, request: Request, response: Response):
    user = await asyncio.to_thread(store.login, body.username, body.password)
    if not user:
        raise HTTPException(401, "아이디 또는 비밀번호가 맞지 않습니다.")
    return issue_session(response, user, request.headers.get("user-agent", ""))


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    require_user(request, csrf=True)
    store.delete_session(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/delete")
async def delete_account(body: AccountDeletion, request: Request, response: Response):
    user, _ = require_user(request, csrf=True)
    verified = await asyncio.to_thread(store.login, user["username"], body.password)
    if not verified:
        raise HTTPException(403, "현재 접근 암호가 맞지 않습니다.")
    room = manager.room_for(user["id"])
    if room and room.phase not in {"ended", "closed"}:
        raise HTTPException(409, "진행 중인 게임이나 대기실을 종료한 뒤 삭제해 주세요.")
    if room:
        if room.phase == "ended":
            await manager.leave_finished_game(user["id"])
        else:
            await manager.close_for_user(user["id"])
    await asyncio.to_thread(store.delete_account, user["id"])
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/onboarding/complete")
async def complete_onboarding(request: Request):
    user, _ = require_user(request, csrf=True)
    store.complete_onboarding(user["id"])
    user["onboarding_complete"] = True
    return {"user": user}


@app.get("/api/rooms")
async def room_list(request: Request):
    require_user(request)
    rooms = []
    for room in manager.rooms.values():
        if not room.started and room.mode == "multi":
            rooms.append(
                {
                    "code": room.code,
                    "scenario": room.scenario.title,
                    "players": sum(
                        not participant.is_bot for participant in room.participants.values()
                    ),
                    "max_players": room.max_humans,
                    "locked": bool(room.password_hash),
                }
            )
    return {"rooms": rooms}


@app.get("/api/rooms/current")
async def current_room(request: Request):
    user, _ = require_user(request)
    room = manager.room_for(user["id"])
    saved = None
    if not room:
        payload = store.load_game(user["id"])
        if payload and payload.get("scenario_id") in SCENARIOS:
            saved = {
                "scenario": SCENARIOS[payload["scenario_id"]].title,
                "day": payload.get("day", 1),
            }
    return {"room": room.snapshot(user["id"]) if room else None, "saved": saved}


@app.post("/api/rooms/exit")
async def exit_game(body: RoomExit, request: Request):
    user, _ = require_user(request, csrf=True)
    try:
        result = await manager.exit_game(user["id"], body.action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"result": result}


@app.post("/api/rooms/saved/delete")
async def delete_saved_game(request: Request):
    user, _ = require_user(request, csrf=True)
    store.delete_game(user["id"])
    return {"ok": True}


@app.post("/api/rooms/resume")
async def resume_game(request: Request):
    user, _ = require_user(request, csrf=True)
    try:
        room = await manager.resume(user["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"room": room.snapshot(user["id"])}


@app.get("/api/rankings")
async def rankings(request: Request, scenario_id: str = ""):
    user, _ = require_user(request)
    if scenario_id and scenario_id not in SCENARIOS:
        raise HTTPException(400, "존재하지 않는 시나리오입니다.")
    entries = store.rankings(scenario_id)
    for entry in entries:
        entry["scenario"] = SCENARIOS[entry["scenario_id"]].title
        entry["you"] = entry.pop("user_id") == user["id"]
    return {"entries": entries, "score_rules": SCORE_RULES}


@app.get("/api/history")
async def history(request: Request, scenario_id: str = ""):
    user, _ = require_user(request)
    if scenario_id and scenario_id not in SCENARIOS:
        raise HTTPException(400, "존재하지 않는 시나리오입니다.")
    result = store.history(user["id"], scenario_id)
    for entry in result["entries"]:
        entry["scenario"] = SCENARIOS[entry["scenario_id"]].title
    return {**result, "score_rules": SCORE_RULES}


@app.post("/api/rooms")
async def create_room(body: RoomCreate, request: Request):
    user, _ = require_user(request, csrf=True)
    if not user["onboarding_complete"]:
        raise HTTPException(409, "생존 안내를 먼저 완료하세요.")
    try:
        room = manager.create(
            user["id"],
            body.scenario_id,
            body.mode,
            body.password,
            nickname=user.get("nickname") or user["username"],
            hide_ids=body.hide_ids,
        )
        if body.mode == "solo":
            await manager.start(room, user["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"room": room.snapshot(user["id"])}


@app.post("/api/rooms/join")
async def join_room(body: RoomJoin, request: Request):
    user, _ = require_user(request, csrf=True)
    if not user["onboarding_complete"]:
        raise HTTPException(409, "생존 안내를 먼저 완료하세요.")
    try:
        room = manager.join(
            user["id"],
            body.code,
            body.password,
            nickname=user.get("nickname") or user["username"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await manager.broadcast(room)
    return {"room": room.snapshot(user["id"])}


@app.post("/api/rooms/start")
async def start_room(request: Request):
    user, _ = require_user(request, csrf=True)
    room = manager.room_for(user["id"])
    if not room:
        raise HTTPException(404, "참가 중인 방이 없습니다.")
    try:
        await manager.start(room, user["id"])
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"room": room.snapshot(user["id"])}


@app.post("/api/rooms/brief")
async def brief_room(request: Request):
    user, _ = require_user(request, csrf=True)
    room = manager.room_for(user["id"])
    if not room or room.started:
        raise HTTPException(404, "시작 대기 중인 방이 없습니다.")
    if room.host_user_id != user["id"]:
        raise HTTPException(403, "방을 만든 참가자만 시작할 수 있습니다.")
    humans = sum(not p.is_bot for p in room.participants.values())
    if room.mode == "multi" and humans < 2:
        raise HTTPException(403, "듀오 검증은 2명이 모여야 시작할 수 있습니다.")
    room.briefing = True
    await manager.broadcast(room)
    return {"ok": True}


@app.post("/api/rooms/leave")
async def leave_room(request: Request):
    user, _ = require_user(request, csrf=True)
    room = manager.room_for(user["id"])
    if room and room.phase == "ended":
        await manager.leave_finished_game(user["id"])
    elif room and not room.started and room.host_user_id != user["id"]:
        manager.leave(user["id"])
        if room.participants:
            await manager.broadcast(room)
    else:
        await manager.close_for_user(user["id"])
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, inh_session: str = Cookie(default="")):
    auth = (
        store.lookup_session(inh_session, websocket.headers.get("user-agent", ""))
        if inh_session
        else None
    )
    if not auth:
        await websocket.close(code=4401)
        return
    user, _ = auth
    room = manager.room_for(user["id"])
    if not room:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send_payload(payload: dict):
        async with send_lock:
            await websocket.send_json(payload)

    async def send_state(_: str = room.code):
        if manager.room_for(user["id"]) is room:
            await send_payload({"type": "state", "room": room.snapshot(user["id"])})

    manager.subscribe(room.code, send_state)
    await send_state()
    try:
        while True:
            message = await websocket.receive_json()
            action = message.get("action")
            client_id = str(message.get("client_id", ""))[:64]
            try:
                if action == "chat":
                    await manager.chat(room, user["id"], str(message.get("text", "")))
                    await send_payload({"type": "chat_ack", "client_id": client_id})
                elif action == "vote":
                    await manager.vote(room, user["id"], str(message.get("target_pid", "")))
                elif action == "decide":
                    await manager.decide(room, user["id"], str(message.get("choice", "")))
                elif action == "defend":
                    await manager.defend(room, user["id"], str(message.get("text", "")))
                elif action == "verdict":
                    await manager.verdict(room, user["id"], bool(message.get("approve")))
                elif action == "ping":
                    await send_payload({"type": "pong"})
                else:
                    raise ValueError("알 수 없는 행동입니다.")
            except ValueError as exc:
                await send_payload(
                    {"type": "error", "message": str(exc), "client_id": client_id}
                )
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(room.code, send_state)
