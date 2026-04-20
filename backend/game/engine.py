from __future__ import annotations
import asyncio
import logging
import random
from typing import TYPE_CHECKING

from backend.game.state import (
    EventType, GameConfig, GameState, GameStatus, Player, RoleType,
)
from backend.game.phases import DayPhase, NightPhase
from backend.game.persistence import GameRecorder

if TYPE_CHECKING:
    from backend.api.sse import SSEManager

logger = logging.getLogger(__name__)

# Role distribution by player count
ROLE_DISTRIBUTION: dict[int, dict[RoleType, int]] = {
    5:  {RoleType.WEREWOLF: 1, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.VILLAGER: 2},
    6:  {RoleType.WEREWOLF: 1, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.HUNTER: 1, RoleType.VILLAGER: 2},
    7:  {RoleType.WEREWOLF: 2, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.VILLAGER: 3},
    8:  {RoleType.WEREWOLF: 2, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.HUNTER: 1, RoleType.VILLAGER: 3},
    9:  {RoleType.WEREWOLF: 2, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.HUNTER: 1, RoleType.VILLAGER: 4},
    10: {RoleType.WEREWOLF: 2, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.HUNTER: 1, RoleType.VILLAGER: 5},
    12: {RoleType.WEREWOLF: 3, RoleType.SEER: 1, RoleType.WITCH: 1, RoleType.HUNTER: 1, RoleType.VILLAGER: 6},
}

MAX_ROUNDS = 30  # safety cap to prevent infinite loops


class _RecordingSSE:
    """Transparent wrapper: broadcasts to SSE and persists to disk."""
    def __init__(self, sse, recorder: GameRecorder) -> None:
        self._sse = sse
        self._recorder = recorder

    async def broadcast(self, event) -> None:
        try:
            await self._recorder.append(event)
        except Exception as e:
            logger.warning("recorder.append failed: %s", e)
        await self._sse.broadcast(event)


class GameEngine:
    """
    Orchestrates the full game lifecycle:
    role assignment → round loop (night → day → check win) → end.
    Runs as an asyncio background task.
    """

    def __init__(self, config: GameConfig, sse: "SSEManager") -> None:
        self.config = config
        self.state = GameState()
        self.recorder = GameRecorder(self.state.game_id)
        self.sse = _RecordingSSE(sse, self.recorder)
        self.pending_inputs: dict = {}  # player_id(str) -> asyncio.Future

    async def start(self) -> None:
        """Entry point — called once by the API route."""
        from backend.ai.player_agent import AIPlayerAgent
        from backend.ai.bedrock_client import BedrockClient
        from backend.ai.openai_client import detect_provider

        self.state.status = GameStatus.RUNNING
        n = len(self.config.player_configs)

        # Build one shared lightweight Bedrock client for structured decisions.
        # Only constructable if the quick model is a Bedrock model; otherwise
        # each agent falls back to its own main model for structured calls.
        fast_client = None
        qm = self.config.quick_model_id
        if qm and not detect_provider(qm):
            # Reuse credentials from first Bedrock player (if any), else env
            first_bedrock = next(
                (p for p in self.config.player_configs if not detect_provider(p.model_id)),
                None,
            )
            try:
                fast_client = BedrockClient(
                    model_id=qm,
                    region=self.config.aws_region,
                    aws_access_key_id=first_bedrock.aws_access_key_id if first_bedrock else None,
                    aws_secret_access_key=first_bedrock.aws_secret_access_key if first_bedrock else None,
                )
                logger.info("Fast model for structured decisions: %s", qm)
            except Exception as e:
                logger.warning("Failed to init fast Bedrock client (%s): %s", qm, e)

        from backend.ai.human_agent import HumanPlayerAgent

        # Build Player objects
        for i, pc in enumerate(self.config.player_configs, start=1):
            player = Player(
                id=i,
                name=pc.name,
                role=RoleType.VILLAGER,  # placeholder; assigned below
                model_id=pc.model_id,
                is_human=pc.is_human,
                aws_access_key_id=pc.aws_access_key_id,
                aws_secret_access_key=pc.aws_secret_access_key,
                api_key=pc.api_key,
            )
            if pc.is_human:
                agent = HumanPlayerAgent(player, self.state, self.sse, self.pending_inputs)
            else:
                agent = AIPlayerAgent(player, self.config.aws_region, fast_client=fast_client)
            player.agent = agent
            self.state.players.append(player)

        # Assign roles
        await self._assign_roles()

        # Announce game start
        ev = self.state.make_event(
            EventType.GAME_START,
            {
                "game_id": self.state.game_id,
                "player_count": n,
                "players": [
                    {"id": p.id, "name": p.name, "model_id": p.model_id}
                    for p in self.state.players
                ],
            },
        )
        await self.sse.broadcast(ev)
        await asyncio.sleep(0.3)

        # Announce role assignment (public: show names, hide roles)
        ev2 = self.state.make_event(
            EventType.ROLE_ASSIGN,
            {
                "assignments": [
                    {"id": p.id, "name": p.name}
                    for p in self.state.players
                ],
                "role_counts": {
                    r.value: sum(1 for p in self.state.players if p.role == r)
                    for r in RoleType
                    if any(p.role == r for p in self.state.players)
                },
            },
        )
        await self.sse.broadcast(ev2)
        await asyncio.sleep(0.5)

        # Main game loop
        try:
            for round_num in range(1, MAX_ROUNDS + 1):
                self.state.round = round_num
                winner = await self._run_round()
                if winner:
                    await self._end_game(winner)
                    return
            # Safety: shouldn't reach here
            await self._end_game("unknown")
        except asyncio.CancelledError:
            logger.info("Game %s cancelled", self.state.game_id)
        except Exception as e:
            logger.exception("Unexpected error in game %s", self.state.game_id)
            err_ev = self.state.make_event(EventType.ERROR, {"message": str(e)})
            await self.sse.broadcast(err_ev)

    async def _assign_roles(self) -> None:
        n = len(self.state.players)
        dist = ROLE_DISTRIBUTION.get(n)
        if dist is None:
            # Fallback: scale up wolves, fill rest with villagers
            wolves = max(1, n // 3)
            dist = {
                RoleType.WEREWOLF: wolves,
                RoleType.SEER: 1,
                RoleType.WITCH: 1,
                RoleType.HUNTER: 1 if n >= 6 else 0,
                RoleType.VILLAGER: n - wolves - (3 if n >= 6 else 2),
            }

        roles: list[RoleType] = []
        for role, count in dist.items():
            roles.extend([role] * count)

        # Pad or trim to exactly n
        while len(roles) < n:
            roles.append(RoleType.VILLAGER)
        roles = roles[:n]

        random.shuffle(roles)
        for player, role in zip(self.state.players, roles):
            player.role = role

        # Configure system prompts now that roles are known
        wolves = [p for p in self.state.players if p.role == RoleType.WEREWOLF]
        for player in self.state.players:
            allies = wolves if player.role == RoleType.WEREWOLF else []
            player.agent.set_system_prompt(allies)

        # Reveal role to human player via private event
        for player in self.state.players:
            if player.is_human:
                ally_list = [
                    {"id": p.id, "name": p.name}
                    for p in wolves if p.id != player.id
                ]
                ev = self.state.make_event(
                    EventType.HUMAN_ROLE_REVEAL,
                    {
                        "player_id":  player.id,
                        "role":       player.role.value,
                        "role_label": player.role_label,
                        "wolf_allies": ally_list,
                    },
                    public=False,
                )
                await self.sse.broadcast(ev)

    async def _run_round(self) -> "str | None":
        """Run one full round. Returns winner string or None."""
        # Night
        self.state.phase = "night"
        night = NightPhase(self.state, self.sse)
        await night.run()

        winner = self.state.check_win_condition()
        if winner:
            return winner

        # Day
        self.state.phase = "day"
        day = DayPhase(self.state, self.sse)
        await day.run()

        winner = self.state.check_win_condition()
        return winner

    async def _end_game(self, winner: str) -> None:
        self.state.winner = winner
        self.state.status = GameStatus.FINISHED
        self.state.phase = "finished"

        winner_label = "狼人" if winner == "werewolves" else "好人" if winner == "villagers" else "未知"
        reason = (
            "所有狼人已被消灭，好人获胜！"
            if winner == "villagers"
            else "狼人数量不少于好人，狼人获胜！"
        )

        # Reveal all roles
        all_roles = [
            {"id": p.id, "name": p.name, "role": p.role.value, "role_label": p.role_label, "alive": p.is_alive}
            for p in self.state.players
        ]

        # Aggregate token usage stats from all Bedrock clients
        from backend.ai.bedrock_client import BedrockClient
        clients_seen = set()
        total_in = total_out = cache_read = cache_write = 0
        for p in self.state.players:
            if not p.agent:
                continue
            for c in (p.agent._client, p.agent._fast_client):
                if isinstance(c, BedrockClient) and id(c) not in clients_seen:
                    clients_seen.add(id(c))
                    total_in    += c.total_input_tokens
                    total_out   += c.total_output_tokens
                    cache_read  += c.cache_read_tokens
                    cache_write += c.cache_write_tokens

        cache_savings_pct = int(100 * cache_read / max(1, total_in)) if total_in else 0

        ev = self.state.make_event(
            EventType.GAME_END,
            {
                "winner": winner,
                "winner_label": winner_label,
                "reason": reason,
                "all_roles": all_roles,
                "total_rounds": self.state.round,
                "usage": {
                    "input_tokens":       total_in,
                    "output_tokens":      total_out,
                    "cache_read_tokens":  cache_read,
                    "cache_write_tokens": cache_write,
                    "cache_hit_pct":      cache_savings_pct,
                },
            },
        )
        await self.sse.broadcast(ev)

        # Persist metadata snapshot for replay
        try:
            await self.recorder.finalize(self.state)
        except Exception as e:
            logger.warning("recorder.finalize failed: %s", e)
