"""
Persist game event logs to disk as JSONL (one event per line).

Layout:
  games/
    {game_id}.jsonl          # event stream, append-only
    {game_id}.meta.json      # metadata: players, winner, timing, counts
"""
from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.game.state import GameEvent, GameState

logger = logging.getLogger(__name__)

GAMES_DIR = Path(__file__).parent.parent.parent / "games"


def _ensure_dir() -> None:
    GAMES_DIR.mkdir(exist_ok=True)


def _event_to_dict(ev: GameEvent) -> dict:
    return {
        "type": ev.type.value,
        "round": ev.round,
        "data": ev.data,
        "public": ev.public,
        "timestamp": ev.timestamp,
    }


class GameRecorder:
    """Append-only recorder — one instance per live game."""

    def __init__(self, game_id: str) -> None:
        _ensure_dir()
        self.game_id = game_id
        self.path = GAMES_DIR / f"{game_id}.jsonl"
        self.meta_path = GAMES_DIR / f"{game_id}.meta.json"
        self._lock = asyncio.Lock()
        self._started_at = datetime.now(timezone.utc).isoformat()

    async def append(self, ev: GameEvent) -> None:
        """Append one event as a JSON line. Safe to call from event loop."""
        line = json.dumps(_event_to_dict(ev), ensure_ascii=False)
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None, self._write_line, line
            )

    def _write_line(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def finalize(self, state: GameState) -> None:
        """Write metadata snapshot once the game ends."""
        meta = {
            "game_id": state.game_id,
            "started_at": self._started_at,
            "ended_at":   datetime.now(timezone.utc).isoformat(),
            "status":     state.status.value,
            "winner":     state.winner,
            "total_rounds": state.round,
            "player_count": len(state.players),
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "role": p.role.value,
                    "role_label": p.role_label,
                    "model_id": p.model_id,
                    "alive": p.is_alive,
                }
                for p in state.players
            ],
        }
        await asyncio.get_event_loop().run_in_executor(
            None, self._write_meta, meta
        )

    def _write_meta(self, meta: dict) -> None:
        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Read-side: list & replay
# ---------------------------------------------------------------------------

def list_games() -> list[dict]:
    """Return metadata of all finished games, newest first."""
    _ensure_dir()
    games = []
    for meta_file in GAMES_DIR.glob("*.meta.json"):
        try:
            with meta_file.open(encoding="utf-8") as f:
                games.append(json.load(f))
        except Exception as e:
            logger.warning("failed to read %s: %s", meta_file, e)
    games.sort(key=lambda m: m.get("ended_at", ""), reverse=True)
    return games


def load_events(game_id: str) -> Optional[list[dict]]:
    """Return all events (as dicts) from a past game, or None if not found."""
    _ensure_dir()
    path = GAMES_DIR / f"{game_id}.jsonl"
    if not path.exists():
        return None
    events: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events
