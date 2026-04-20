from __future__ import annotations
import asyncio
import logging
import random
from typing import TYPE_CHECKING

from backend.game.state import (
    EventType, GameConfig, GameState, GameStatus, Player, RoleType,
)
from backend.game.phases import DayPhase, NightPhase

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


class GameEngine:
    """
    Orchestrates the full game lifecycle:
    role assignment → round loop (night → day → check win) → end.
    Runs as an asyncio background task.
    """

    def __init__(self, config: GameConfig, sse: "SSEManager") -> None:
        self.config = config
        self.sse = sse
        self.state = GameState()

    async def start(self) -> None:
        """Entry point — called once by the API route."""
        from backend.ai.player_agent import AIPlayerAgent

        self.state.status = GameStatus.RUNNING
        n = len(self.config.player_configs)

        # Build Player objects
        for i, pc in enumerate(self.config.player_configs, start=1):
            player = Player(
                id=i,
                name=pc.name,
                role=RoleType.VILLAGER,  # placeholder; assigned below
                model_id=pc.model_id,
                aws_access_key_id=pc.aws_access_key_id,
                aws_secret_access_key=pc.aws_secret_access_key,
                api_key=pc.api_key,
            )
            agent = AIPlayerAgent(player, self.config.aws_region)
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

        ev = self.state.make_event(
            EventType.GAME_END,
            {
                "winner": winner,
                "winner_label": winner_label,
                "reason": reason,
                "all_roles": all_roles,
                "total_rounds": self.state.round,
            },
        )
        await self.sse.broadcast(ev)
