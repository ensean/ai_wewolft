from __future__ import annotations
import asyncio
import logging
import random
from typing import TYPE_CHECKING, Callable, Awaitable

from backend.game.state import EventType, GameState, Player, RoleType

if TYPE_CHECKING:
    from backend.api.sse import SSEManager

logger = logging.getLogger(__name__)

# delay between LLM actions (seconds) — makes the game feel more natural
ACTION_DELAY = 1.0


class PhaseBase:
    def __init__(self, state: GameState, sse: "SSEManager") -> None:
        self.state = state
        self.sse = sse

    async def _broadcast(
        self,
        etype: EventType,
        data: dict,
        public: bool = True,
    ) -> None:
        ev = self.state.make_event(etype, data, public=public)
        await self.sse.broadcast(ev)

    async def _sys_msg(self, msg: str) -> None:
        await self._broadcast(EventType.SYSTEM, {"message": msg})

    async def _pause(self, seconds: float = ACTION_DELAY) -> None:
        await asyncio.sleep(seconds)


class NightPhase(PhaseBase):
    async def run(self) -> None:
        self.state.reset_night_scratch()
        await self._sys_msg(f"🌙 第{self.state.round}夜，天黑请闭眼……")
        await self._pause(0.5)

        await self._werewolf_action()
        await self._seer_action()
        await self._witch_action()

        await self._sys_msg("☀️ 天亮了……")
        await self._pause(0.5)

    # ---- Werewolves ----

    async def _werewolf_action(self) -> None:
        wolves = self.state.alive_werewolves()
        if not wolves:
            return

        await self._sys_msg("🐺 狼人请睁眼，开始商议……")

        # Each wolf discusses privately
        for wolf in wolves:
            try:
                opinion = await wolf.agent.werewolf_discuss(self.state)
                await self._broadcast(
                    EventType.WOLF_DISCUSS,
                    {"player_id": wolf.id, "player_name": wolf.name, "content": opinion},
                    public=False,  # private: only visible in host log
                )
                await self._pause(0.3)
            except Exception as e:
                logger.error("Wolf discuss error [%s]: %s", wolf.name, e)

        # Each wolf votes; majority wins
        votes: dict[int, int] = {}  # target_id -> count
        for wolf in wolves:
            try:
                target_id = await wolf.agent.werewolf_vote_kill(self.state)
                votes[target_id] = votes.get(target_id, 0) + 1
            except Exception as e:
                logger.error("Wolf vote error [%s]: %s", wolf.name, e)
                # fallback: random alive good guy
                good = self.state.alive_villager_side()
                if good:
                    tid = random.choice(good).id
                    votes[tid] = votes.get(tid, 0) + 1

        if not votes:
            return

        kill_id = max(votes, key=lambda k: votes[k])
        self.state.night_kill_target = kill_id
        target = self.state.get_player(kill_id)
        await self._broadcast(
            EventType.WOLF_DISCUSS,
            {
                "player_id": -1,
                "player_name": "系统",
                "content": f"狼人达成共识，今晚击杀目标：{target.name if target else '？'}",
            },
            public=False,
        )
        await self._sys_msg("🐺 狼人请闭眼……")

    # ---- Seer ----

    async def _seer_action(self) -> None:
        seers = [p for p in self.state.alive_players() if p.role == RoleType.SEER]
        if not seers:
            return

        seer = seers[0]
        await self._sys_msg("🔮 预言家请睁眼……")
        await self._pause(0.3)

        try:
            target_id = await seer.agent.seer_check(self.state)
        except Exception as e:
            logger.error("Seer check error: %s", e)
            candidates = [p for p in self.state.alive_players() if p.id != seer.id]
            target_id = random.choice(candidates).id if candidates else -1

        if target_id != -1:
            target = self.state.get_player(target_id)
            if target:
                self.state.seer_checks[target_id] = target.role
                camp = "狼人" if target.role == RoleType.WEREWOLF else "好人"
                await self._broadcast(
                    EventType.SEER_RESULT,
                    {
                        "seer_id": seer.id,
                        "seer_name": seer.name,
                        "target_id": target_id,
                        "target_name": target.name,
                        "result": camp,
                    },
                    public=False,
                )

        await self._sys_msg("🔮 预言家请闭眼……")

    # ---- Witch ----

    async def _witch_action(self) -> None:
        witches = [p for p in self.state.alive_players() if p.role == RoleType.WITCH]
        if not witches:
            return

        witch = witches[0]
        kill_target = (
            self.state.get_player(self.state.night_kill_target)
            if self.state.night_kill_target
            else None
        )

        # Skip witch turn if no actions possible
        if state_has_no_witch_options(self.state, kill_target):
            return

        await self._sys_msg("🧪 女巫请睁眼……")
        await self._pause(0.3)

        try:
            decision = await witch.agent.witch_decide(self.state, kill_target)
        except Exception as e:
            logger.error("Witch decide error: %s", e)
            decision = {"action": "skip"}

        action = decision.get("action", "skip")

        if action == "save" and kill_target and not self.state.witch_save_used:
            self.state.witch_save_used = True
            self.state.witch_saved_tonight = True
            self.state.night_kill_target = None  # cancel the kill
            await self._broadcast(
                EventType.WITCH_ACTION,
                {
                    "witch_id": witch.id,
                    "witch_name": witch.name,
                    "action": "save",
                    "target_name": kill_target.name,
                },
                public=False,
            )

        elif action == "poison" and not self.state.witch_poison_used:
            pt = decision.get("poison_target")
            if pt:
                self.state.witch_poison_used = True
                self.state.witch_poison_target = int(pt)
                pt_player = self.state.get_player(int(pt))
                await self._broadcast(
                    EventType.WITCH_ACTION,
                    {
                        "witch_id": witch.id,
                        "witch_name": witch.name,
                        "action": "poison",
                        "target_id": int(pt),
                        "target_name": pt_player.name if pt_player else "？",
                    },
                    public=False,
                )
        else:
            await self._broadcast(
                EventType.WITCH_ACTION,
                {"witch_id": witch.id, "witch_name": witch.name, "action": "skip"},
                public=False,
            )

        await self._sys_msg("🧪 女巫请闭眼……")


def state_has_no_witch_options(state: GameState, kill_target: "Player | None") -> bool:
    """Returns True if witch has no actions left (both potions used, no kill to save)."""
    if not state.witch_save_used and kill_target:
        return False
    if not state.witch_poison_used:
        return False
    return True


class DayPhase(PhaseBase):
    async def run(self) -> None:
        # 1. Resolve and announce night deaths
        deaths = await self._resolve_night_deaths()

        # 2. Hunter trigger on night death (only if killed by wolves, not witch poison)
        for dead_player in deaths:
            if dead_player.role == RoleType.HUNTER:
                # Hunter cannot shoot if killed by witch poison
                poisoned_by_witch = (
                    self.state.witch_poison_target == dead_player.id
                )
                if not poisoned_by_witch:
                    await self._trigger_hunter(dead_player)

        # 3. Check win after night
        if self.state.check_win_condition():
            return

        # 4. Discussion
        await self._discussion()

        # 5. Vote
        exiled = await self._vote()

        # 6. Hunter trigger on exile
        if exiled and exiled.role == RoleType.HUNTER:
            await self._trigger_hunter(exiled)

    # ------------------------------------------------------------------

    async def _resolve_night_deaths(self) -> list[Player]:
        """Kill players per night actions; return list of newly dead players."""
        dead: list[Player] = []

        # Wolf kill (unless witch saved)
        if self.state.night_kill_target:
            target = self.state.get_player(self.state.night_kill_target)
            if target and target.is_alive:
                target.is_alive = False
                dead.append(target)
                await self._broadcast(
                    EventType.DEATH,
                    {
                        "player_id": target.id,
                        "player_name": target.name,
                        "cause": "wolf_kill",
                        "role_revealed": target.role.value,
                    },
                )
                await self._pause(0.5)

        # Witch poison
        if self.state.witch_poison_target:
            target = self.state.get_player(self.state.witch_poison_target)
            if target and target.is_alive:
                target.is_alive = False
                dead.append(target)
                await self._broadcast(
                    EventType.DEATH,
                    {
                        "player_id": target.id,
                        "player_name": target.name,
                        "cause": "witch_poison",
                        "role_revealed": target.role.value,
                    },
                )
                await self._pause(0.5)

        if not dead:
            await self._sys_msg("🌤️ 昨晚是平安夜，没有人死亡。")

        return dead

    async def _discussion(self) -> None:
        alive = self.state.alive_players()
        order = alive.copy()
        random.shuffle(order)
        await self._sys_msg(f"💬 白天讨论开始，发言顺序：{'→'.join(p.name for p in order)}")

        for player in order:
            await self._pause(0.5)
            try:
                speech = await player.agent.speak(self.state)
            except Exception as e:
                logger.error("Speech error [%s]: %s", player.name, e)
                speech = "（沉默）"

            await self._broadcast(
                EventType.SPEECH,
                {
                    "player_id": player.id,
                    "player_name": player.name,
                    "content": speech,
                    "phase": "day",
                },
            )

    async def _vote(self) -> "Player | None":
        alive = self.state.alive_players()
        await self._sys_msg("🗳️ 开始投票……")

        tally: dict[int, int] = {}  # target_id -> votes
        for player in alive:
            await self._pause(0.3)
            try:
                target_id = await player.agent.vote(self.state)
            except Exception as e:
                logger.error("Vote error [%s]: %s", player.name, e)
                others = [p.id for p in alive if p.id != player.id]
                target_id = random.choice(others) if others else -1

            if target_id != -1:
                tally[target_id] = tally.get(target_id, 0) + 1
                target = self.state.get_player(target_id)
                await self._broadcast(
                    EventType.VOTE,
                    {
                        "voter_id": player.id,
                        "voter_name": player.name,
                        "target_id": target_id,
                        "target_name": target.name if target else "？",
                    },
                )

        if not tally:
            await self._sys_msg("🗳️ 没有有效票数，本轮平票无人出局。")
            return None

        max_votes = max(tally.values())
        top = [pid for pid, v in tally.items() if v == max_votes]

        if len(top) > 1:
            await self._sys_msg(f"🗳️ 平票！{[self.state.get_player(p).name for p in top]} 均获 {max_votes} 票，无人出局。")
            return None

        exiled_id = top[0]
        exiled = self.state.get_player(exiled_id)
        if exiled:
            exiled.is_alive = False
            await self._broadcast(
                EventType.DEATH,
                {
                    "player_id": exiled.id,
                    "player_name": exiled.name,
                    "cause": "voted_out",
                    "role_revealed": exiled.role.value,
                    "vote_count": max_votes,
                },
            )
        return exiled

    async def _trigger_hunter(self, hunter: Player) -> None:
        await self._sys_msg(f"🔫 {hunter.name} 是猎人！猎人请决定是否开枪……")
        try:
            shot_id = await hunter.agent.hunter_shoot(self.state)
        except Exception as e:
            logger.error("Hunter shoot error: %s", e)
            shot_id = None

        if shot_id is not None:
            target = self.state.get_player(shot_id)
            if target and target.is_alive:
                target.is_alive = False
                await self._broadcast(
                    EventType.HUNTER_SHOT,
                    {
                        "hunter_id": hunter.id,
                        "hunter_name": hunter.name,
                        "target_id": shot_id,
                        "target_name": target.name,
                        "role_revealed": target.role.value,
                    },
                )
        else:
            await self._sys_msg(f"🔫 {hunter.name} 选择不开枪。")
