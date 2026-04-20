from __future__ import annotations
import asyncio
import logging
import random
from typing import Optional, TYPE_CHECKING

from backend.game.state import EventType, GameEvent, GameState, Player, RoleType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

HUMAN_TIMEOUT = 180  # seconds before auto-skip


class HumanPlayerAgent:
    """
    Agent that pauses the game and waits for the human player's input.
    Instead of calling an LLM, it broadcasts a HUMAN_INPUT_REQUIRED event
    and awaits an asyncio.Future that is resolved by the HTTP input endpoint.
    """

    def __init__(self, player: Player, state: GameState, sse, pending_inputs: dict) -> None:
        self.player = player
        self._state = state
        self._sse = sse
        self._pending = pending_inputs
        self._wolf_allies: list[Player] = []

    def set_system_prompt(self, allies: list[Player] | None = None) -> None:
        """Store wolf allies for role reveal. No LLM system prompt needed."""
        self._wolf_allies = allies or []

    # ------------------------------------------------------------------
    # Public action methods (same interface as AIPlayerAgent)
    # ------------------------------------------------------------------

    async def speak(self, state: GameState) -> str:
        raw = await self._request(
            "speak",
            {"prompt": "轮到你发言，请说出你的看法或推理（支持中英文，不超过200字）"},
        )
        return raw.strip() or "（沉默）"

    async def last_words(self, state: GameState, cause: str) -> str:
        cause_text = {
            "wolf_kill":   "你昨晚被狼人杀害",
            "voted_out":   "你刚刚被投票放逐",
            "hunter_shot": "你被猎人带走",
        }.get(cause, "你已出局")
        raw = await self._request(
            "last_words",
            {"prompt": f"{cause_text}，这是你最后的发言机会（遗言）"},
            timeout=120,
        )
        return raw.strip() or "……"

    async def vote(self, state: GameState) -> int:
        candidates = [p for p in state.alive_players() if p.id != self.player.id]
        raw = await self._request(
            "vote",
            {
                "prompt": "请投票驱逐你认为最可疑的玩家",
                "candidates": [{"id": p.id, "name": p.name} for p in candidates],
            },
        )
        return _parse_int(raw, [p.id for p in candidates])

    async def werewolf_discuss(self, state: GameState) -> str:
        alive_good = state.alive_villager_side()
        raw = await self._request(
            "werewolf_discuss",
            {
                "prompt": "【私狼频道】请与同伴商议今晚击杀策略",
                "candidates": [{"id": p.id, "name": p.name} for p in alive_good],
            },
        )
        return raw.strip() or "（沉默）"

    async def werewolf_vote_kill(self, state: GameState) -> int:
        alive_good = state.alive_villager_side()
        raw = await self._request(
            "werewolf_vote_kill",
            {
                "prompt": "【私狼频道】请选择今晚要击杀的目标",
                "candidates": [{"id": p.id, "name": p.name} for p in alive_good],
            },
        )
        return _parse_int(raw, [p.id for p in alive_good])

    async def seer_check(self, state: GameState) -> int:
        checked = set(state.seer_checks.keys())
        candidates = [
            p for p in state.alive_players()
            if p.id != self.player.id and p.id not in checked
        ] or [p for p in state.alive_players() if p.id != self.player.id]
        if not candidates:
            return -1
        raw = await self._request(
            "seer_check",
            {
                "prompt": "请选择今晚要查验身份的玩家",
                "candidates": [{"id": p.id, "name": p.name} for p in candidates],
            },
        )
        return _parse_int(raw, [p.id for p in candidates])

    async def witch_decide(self, state: GameState, kill_target: Optional[Player]) -> dict:
        alive_others = [p for p in state.alive_players() if p.id != self.player.id]
        raw = await self._request(
            "witch_decide",
            {
                "prompt": "请决定今晚是否使用道具",
                "kill_target": {"id": kill_target.id, "name": kill_target.name}
                               if kill_target else None,
                "save_available": not state.witch_save_used and kill_target is not None,
                "poison_available": not state.witch_poison_used,
                "candidates": [{"id": p.id, "name": p.name} for p in alive_others],
            },
        )
        return _parse_witch(raw, state, self.player, kill_target)

    async def hunter_shoot(self, state: GameState) -> Optional[int]:
        alive_others = [p for p in state.alive_players() if p.id != self.player.id]
        if not alive_others:
            return None
        raw = await self._request(
            "hunter_shoot",
            {
                "prompt": "你是猎人！可以选择带走一名玩家，或选择放弃",
                "candidates": [{"id": p.id, "name": p.name} for p in alive_others],
            },
            timeout=60,
        )
        if not raw or raw.strip() == "skip":
            return None
        return _parse_int(raw, [p.id for p in alive_others], allow_none=True)

    # ------------------------------------------------------------------
    # Core: broadcast request, await Future
    # ------------------------------------------------------------------

    async def _request(self, action_type: str, extra: dict, timeout: float = HUMAN_TIMEOUT) -> str:
        key = str(self.player.id)
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[key] = future

        ev = self._state.make_event(
            EventType.HUMAN_INPUT_REQUIRED,
            {
                "player_id":   self.player.id,
                "player_name": self.player.name,
                "action_type": action_type,
                "timeout":     timeout,
                **extra,
            },
        )
        await self._sse.broadcast(ev)

        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return result or ""
        except asyncio.TimeoutError:
            logger.info("Human player %s timed out on %s", self.player.name, action_type)
            return ""
        finally:
            self._pending.pop(key, None)
            done_ev = self._state.make_event(
                EventType.HUMAN_INPUT_DONE,
                {"player_id": self.player.id, "action_type": action_type},
            )
            await self._sse.broadcast(done_ev)


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _parse_int(raw: str, valid_ids: list[int], allow_none: bool = False) -> Optional[int]:
    raw = (raw or "").strip()
    try:
        tid = int(raw)
        if tid in valid_ids:
            return tid
    except ValueError:
        pass
    # try extracting first number
    import re
    for m in re.findall(r"\d+", raw):
        if int(m) in valid_ids:
            return int(m)
    if allow_none:
        return None
    return random.choice(valid_ids) if valid_ids else -1


def _parse_witch(raw: str, state: GameState, player: Player, kill_target: Optional[Player]) -> dict:
    raw = (raw or "").strip().lower()
    if raw == "save" and not state.witch_save_used and kill_target:
        return {"action": "save"}
    if raw.startswith("poison:"):
        try:
            tid = int(raw.split(":")[1])
            alive_others = [p.id for p in state.alive_players() if p.id != player.id]
            if tid in alive_others and not state.witch_poison_used:
                return {"action": "poison", "poison_target": tid}
        except (ValueError, IndexError):
            pass
    return {"action": "skip"}
