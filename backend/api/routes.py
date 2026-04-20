from __future__ import annotations
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from backend.api.bedrock_models import fetch_bedrock_models
from backend.api.sse import SSEManager
from backend.game.engine import GameEngine
from backend.game.persistence import list_games, load_events
from backend.game.state import GameConfig, GameStatus, RoleType
from pydantic import BaseModel

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

# Static fallback + non-Bedrock providers
STATIC_MODELS = [
    # --- Kimi (Moonshot) ---
    {"id": "moonshot-v1-8k",   "label": "moonshot-v1-8k",   "group": "Kimi"},
    {"id": "moonshot-v1-32k",  "label": "moonshot-v1-32k",  "group": "Kimi"},
    {"id": "moonshot-v1-128k", "label": "moonshot-v1-128k", "group": "Kimi"},
    # --- DeepSeek (API) ---
    {"id": "deepseek-chat",     "label": "deepseek-chat",     "group": "DeepSeek"},
    {"id": "deepseek-reasoner", "label": "deepseek-reasoner", "group": "DeepSeek"},
    # --- MiniMax (API) ---
    {"id": "MiniMax-Text-01", "label": "MiniMax-Text-01", "group": "MiniMax"},
    # --- GLM (Zhipu) ---
    {"id": "glm-4-flash", "label": "glm-4-flash", "group": "GLM"},
    {"id": "glm-4-air",   "label": "glm-4-air",   "group": "GLM"},
    {"id": "glm-4",       "label": "glm-4",        "group": "GLM"},
]


class BedrockModelRequest(BaseModel):
    region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None


def create_router(sse_manager: SSEManager, engine_holder: dict) -> APIRouter:
    router = APIRouter()

    # ------------------------------------------------------------------
    # Frontend
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def index():
        html_path = FRONTEND_DIR / "index.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @router.get("/style.css")
    async def style():
        return FileResponse(FRONTEND_DIR / "style.css", media_type="text/css")

    @router.get("/app.js")
    async def app_js():
        return FileResponse(FRONTEND_DIR / "app.js", media_type="application/javascript")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    @router.get("/api/models")
    async def get_models():
        """Return non-Bedrock static models. Bedrock models loaded separately."""
        return {"models": STATIC_MODELS}

    @router.post("/api/bedrock-models")
    async def get_bedrock_models(req: BedrockModelRequest):
        """Fetch live Bedrock model list: inference profiles + foundation models."""
        try:
            models = await fetch_bedrock_models(
                region=req.region,
                aws_access_key_id=req.aws_access_key_id,
                aws_secret_access_key=req.aws_secret_access_key,
            )
            return {"models": models, "count": len(models)}
        except Exception as e:
            logger.warning("fetch_bedrock_models failed: %s", e)
            raise HTTPException(500, f"无法获取 Bedrock 模型列表：{e}")

    @router.post("/api/games")
    async def start_game(config: GameConfig):
        n = len(config.player_configs)
        if n < 5 or n > 12:
            raise HTTPException(400, "玩家数量须在 5~12 人之间")

        # Stop any existing running game
        existing = engine_holder.get("task")
        if existing and not existing.done():
            existing.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(existing), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        engine = GameEngine(config, sse_manager)
        engine_holder["engine"] = engine
        task = asyncio.create_task(engine.start(), name="game_engine")
        engine_holder["task"] = task
        task.add_done_callback(_log_task_result)

        return {"game_id": engine.state.game_id, "status": "started"}

    @router.get("/api/games/current")
    async def get_current_game():
        engine: GameEngine | None = engine_holder.get("engine")
        if not engine:
            raise HTTPException(404, "没有正在进行的游戏")

        s = engine.state
        return {
            "game_id": s.game_id,
            "status": s.status.value,
            "round": s.round,
            "phase": s.phase,
            "winner": s.winner,
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "is_alive": p.is_alive,
                    # Only reveal role if game is finished
                    "role": p.role.value if s.status == GameStatus.FINISHED else None,
                    "role_label": p.role_label if s.status == GameStatus.FINISHED else None,
                }
                for p in s.players
            ],
        }

    @router.get("/api/games/history")
    async def get_history():
        """List metadata of all past games, newest first."""
        return {"games": list_games()}

    @router.get("/api/games/{game_id}/replay")
    async def replay_game(game_id: str, request: Request, speed: float = 4.0):
        """Replay a past game as SSE stream, pacing events with 'speed' multiplier."""
        events = load_events(game_id)
        if events is None:
            raise HTTPException(404, f"Game {game_id} not found")

        # Delay schedule between events (seconds) — proportional to event importance
        delay_by_type = {
            "speech":     1.8,
            "last_words": 2.5,
            "death":      1.2,
            "vote_tally": 0.25,
            "vote":       0.35,
            "phase_start":1.0,
            "system":     0.6,
            "game_start": 0.8,
            "role_assign":0.8,
        }

        async def stream():
            import json as _json
            for ev in events:
                if await request.is_disconnected():
                    return
                payload = _json.dumps(ev, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                base = delay_by_type.get(ev.get("type"), 0.4)
                await asyncio.sleep(max(0.02, base / max(0.5, speed)))

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.get("/api/games/events")
    async def game_events(request: Request):
        client_id, q = sse_manager.add_client()

        # Replay existing event log for late-joining clients
        engine: GameEngine | None = engine_holder.get("engine")
        if engine:
            for ev in engine.state.event_log:
                try:
                    q.put_nowait(
                        __import__("backend.api.sse", fromlist=["_serialize"])._serialize(ev)
                    )
                except Exception:
                    pass

        async def stream():
            async for chunk in sse_manager.event_stream(client_id, q):
                if await request.is_disconnected():
                    break
                yield chunk

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return router


def _log_task_result(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
        if exc:
            logger.error("Game engine task raised: %s", exc)
    except asyncio.CancelledError:
        pass
