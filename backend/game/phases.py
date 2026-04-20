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

    async def _kill_and_announce(
        self,
        player: Player,
        cause: str,
        extra: dict | None = None,
    ) -> None:
        """Mark a player dead, broadcast DEATH, then their last words."""
        if not player.is_alive:
            return
        was_sheriff = (self.state.sheriff_id == player.id)
        player.is_alive = False
        data = {
            "player_id": player.id,
            "player_name": player.name,
            "cause": cause,
            "role_revealed": player.role.value,
            "role_label": player.role_label,
            "was_sheriff": was_sheriff,
        }
        if extra:
            data.update(extra)
        await self._broadcast(EventType.DEATH, data)
        await self._pause(0.5)

        # Last words — skip for witch-poison (classic rule: poisoned can't speak)
        if cause == "witch_poison":
            await self._sys_msg(f"💔 {player.name} 被毒死，无法留下遗言。")
            # Poisoned sheriff also loses badge silently (destroyed)
            if was_sheriff:
                self.state.sheriff_id = None
                self.state.sheriff_badge_destroyed = True
                await self._broadcast(
                    EventType.SHERIFF_BADGE_HANDOFF,
                    {"from_id": player.id, "from_name": player.name,
                     "action": "destroy", "reason": "poisoned"},
                )
            return

        await self._last_words(player, cause)

        # Sheriff death: handle badge handoff after last words
        if was_sheriff:
            await self._handle_sheriff_badge(player)

    async def _handle_sheriff_badge(self, dead_sheriff: Player) -> None:
        """Sheriff died: let them pass badge to another alive player or destroy it."""
        await self._sys_msg(f"👮 警长 {dead_sheriff.name} 出局，需决定警徽归属……")
        try:
            decision = await dead_sheriff.agent.badge_decision(self.state)
        except Exception as e:
            logger.error("badge_decision error: %s", e)
            decision = {"action": "destroy"}

        if decision.get("action") == "pass":
            tid = decision.get("target_id")
            target = self.state.get_player(tid) if tid else None
            if target and target.is_alive:
                self.state.sheriff_id = tid
                await self._broadcast(
                    EventType.SHERIFF_BADGE_HANDOFF,
                    {
                        "from_id": dead_sheriff.id, "from_name": dead_sheriff.name,
                        "to_id": tid, "to_name": target.name,
                        "action": "pass",
                    },
                )
                await self._sys_msg(f"👮 警徽移交给 {target.name}！")
                return

        # Destroy
        self.state.sheriff_id = None
        self.state.sheriff_badge_destroyed = True
        await self._broadcast(
            EventType.SHERIFF_BADGE_HANDOFF,
            {
                "from_id": dead_sheriff.id, "from_name": dead_sheriff.name,
                "action": "destroy",
            },
        )
        await self._sys_msg(f"💥 {dead_sheriff.name} 选择撕毁警徽，本局不再有警长。")

    async def _last_words(self, player: Player, cause: str) -> None:
        try:
            words = await player.agent.last_words(self.state, cause)
        except Exception as e:
            logger.error("Last words error [%s]: %s", player.name, e)
            words = ""
        words = (words or "").strip() or "……"
        await self._broadcast(
            EventType.LAST_WORDS,
            {
                "player_id": player.id,
                "player_name": player.name,
                "role_label": player.role_label,
                "content": words,
                "cause": cause,
            },
        )
        await self._pause(0.8)


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

        # 3.5. Sheriff election (only round 1, if sheriff not yet set and badge not destroyed)
        if (self.state.round == 1
                and self.state.sheriff_id is None
                and not self.state.sheriff_badge_destroyed):
            await self._run_sheriff_election()

        # 4. Discussion
        await self._discussion()

        # 5. Vote
        exiled = await self._vote()

        # 6. Hunter trigger on exile
        if exiled and exiled.role == RoleType.HUNTER:
            await self._trigger_hunter(exiled)

    # ------------------------------------------------------------------
    # Sheriff election
    # ------------------------------------------------------------------

    async def _run_sheriff_election(self) -> None:
        await self._broadcast(
            EventType.SHERIFF_CAMPAIGN_START,
            {"message": "💂 警长竞选开始！各玩家请决定是否上警。"},
        )
        await self._pause(0.5)

        alive = self.state.alive_players()

        # 1. Each alive player decides to run
        candidates: list[Player] = []
        for p in alive:
            try:
                if await p.agent.decide_run_for_sheriff(self.state):
                    candidates.append(p)
            except Exception as e:
                logger.error("decide_run_for_sheriff error [%s]: %s", p.name, e)

        if not candidates:
            await self._sys_msg("🏳️ 无人参选，本局无警长。")
            self.state.sheriff_badge_destroyed = True  # no future elections
            return

        await self._broadcast(
            EventType.SHERIFF_CANDIDATES,
            {"candidates": [{"id": p.id, "name": p.name} for p in candidates]},
        )

        # 2. If only one candidate, auto-elect
        if len(candidates) == 1:
            sole = candidates[0]
            self.state.sheriff_id = sole.id
            await self._broadcast(
                EventType.SHERIFF_ELECTED,
                {"sheriff_id": sole.id, "sheriff_name": sole.name, "uncontested": True},
            )
            await self._sys_msg(f"👮 {sole.name} 独自上警，自动当选为警长！")
            return

        # 3. Candidates give campaign speeches
        campaign_order = candidates.copy()
        random.shuffle(campaign_order)
        await self._sys_msg(
            f"💂 共 {len(candidates)} 人上警：{'、'.join(p.name for p in candidates)}，依次发言……"
        )
        for p in campaign_order:
            await self._pause(0.4)
            try:
                speech = await p.agent.sheriff_campaign_speech(self.state)
            except Exception as e:
                logger.error("sheriff_campaign_speech error [%s]: %s", p.name, e)
                speech = "（沉默）"
            await self._broadcast(
                EventType.SHERIFF_CAMPAIGN,
                {"player_id": p.id, "player_name": p.name, "content": speech},
            )

        # 4. Non-candidates vote
        candidate_ids = {p.id for p in candidates}
        voters = [p for p in alive if p.id not in candidate_ids]
        if not voters:
            await self._sys_msg("所有存活玩家都上警了，无投票者，本局无警长。")
            self.state.sheriff_badge_destroyed = True
            return

        await self._sys_msg("🗳️ 非候选玩家投票选举警长……")
        tally: dict[int, int] = {}
        for voter in voters:
            await self._pause(0.25)
            try:
                tid = await voter.agent.vote_for_sheriff(self.state, candidates)
            except Exception as e:
                logger.error("vote_for_sheriff error [%s]: %s", voter.name, e)
                tid = random.choice([p.id for p in candidates])
            if tid in candidate_ids:
                tally[tid] = tally.get(tid, 0) + 1
                target = self.state.get_player(tid)
                await self._broadcast(
                    EventType.SHERIFF_VOTE,
                    {
                        "voter_id": voter.id, "voter_name": voter.name,
                        "target_id": tid, "target_name": target.name if target else "?",
                    },
                )

        if not tally:
            await self._sys_msg("🏳️ 无有效票，本局无警长。")
            self.state.sheriff_badge_destroyed = True
            return

        max_votes = max(tally.values())
        top = [pid for pid, v in tally.items() if v == max_votes]
        if len(top) > 1:
            names = [self.state.get_player(p).name for p in top]
            await self._sys_msg(f"🏳️ 警长投票平票（{'、'.join(names)}），本局无警长。")
            self.state.sheriff_badge_destroyed = True
            return

        winner = self.state.get_player(top[0])
        self.state.sheriff_id = winner.id
        await self._broadcast(
            EventType.SHERIFF_ELECTED,
            {
                "sheriff_id": winner.id,
                "sheriff_name": winner.name,
                "vote_count": max_votes,
            },
        )
        await self._sys_msg(f"👮 {winner.name} 当选警长（{max_votes} 票）！")

    # ------------------------------------------------------------------

    async def _resolve_night_deaths(self) -> list[Player]:
        """Kill players per night actions; return list of newly dead players."""
        dead: list[Player] = []

        # Wolf kill (unless witch saved)
        if self.state.night_kill_target:
            target = self.state.get_player(self.state.night_kill_target)
            if target and target.is_alive:
                dead.append(target)
                await self._kill_and_announce(target, "wolf_kill")

        # Witch poison
        if self.state.witch_poison_target:
            target = self.state.get_player(self.state.witch_poison_target)
            if target and target.is_alive:
                dead.append(target)
                await self._kill_and_announce(target, "witch_poison")

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
        total = len(alive)
        await self._sys_msg("🗳️ 开始投票……")

        tally: dict[int, float] = {}  # target_id -> weighted votes
        for idx, player in enumerate(alive, start=1):
            await self._pause(0.3)
            try:
                target_id = await player.agent.vote(self.state)
            except Exception as e:
                logger.error("Vote error [%s]: %s", player.name, e)
                others = [p.id for p in alive if p.id != player.id]
                target_id = random.choice(others) if others else -1

            if target_id != -1:
                # Sheriff's vote counts as 1.5
                weight = 1.5 if player.id == self.state.sheriff_id else 1.0
                tally[target_id] = tally.get(target_id, 0) + weight
                target = self.state.get_player(target_id)
                await self._broadcast(
                    EventType.VOTE,
                    {
                        "voter_id": player.id,
                        "voter_name": player.name,
                        "target_id": target_id,
                        "target_name": target.name if target else "？",
                        "weight": weight,
                    },
                )
                await self._broadcast_tally(tally, voted=idx, total=total)

        if not tally:
            await self._sys_msg("🗳️ 没有有效票数，本轮平票无人出局。")
            return None

        max_votes = max(tally.values())
        top = [pid for pid, v in tally.items() if v == max_votes]

        # Final tally (emphasized)
        await self._broadcast_tally(tally, voted=len(alive), total=len(alive), final=True)

        if len(top) > 1:
            names = [self.state.get_player(p).name for p in top]
            await self._sys_msg(f"🗳️ 平票！{'、'.join(names)} 均获 {max_votes} 票，无人出局。")
            return None

        exiled_id = top[0]
        exiled = self.state.get_player(exiled_id)
        if exiled:
            await self._kill_and_announce(
                exiled, "voted_out", {"vote_count": max_votes}
            )
        return exiled

    async def _broadcast_tally(
        self, tally: dict[int, int], voted: int, total: int, final: bool = False
    ) -> None:
        items = []
        for tid, v in sorted(tally.items(), key=lambda x: -x[1]):
            p = self.state.get_player(tid)
            items.append({
                "target_id": tid,
                "target_name": p.name if p else "?",
                "votes": v,
            })
        await self._broadcast(
            EventType.VOTE_TALLY,
            {"items": items, "voted": voted, "total": total, "final": final},
        )

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
                await self._kill_and_announce(target, "hunter_shot")
        else:
            await self._sys_msg(f"🔫 {hunter.name} 选择不开枪。")
