from __future__ import annotations
import json
import logging
import random
import re
from typing import Optional

from backend.ai.bedrock_client import BedrockClient
from backend.ai.openai_client import OpenAICompatibleClient, detect_provider
from backend.ai.prompts import (
    build_game_context,
    system_prompt_werewolf,
    system_prompt_villager,
    system_prompt_seer,
    system_prompt_witch,
    system_prompt_hunter,
)
from backend.game.state import GameState, Player, RoleType

logger = logging.getLogger(__name__)


class AIPlayerAgent:
    """
    One agent per Player.  Owns a BedrockClient and constructs role-aware prompts.
    All LLM calls are async (Bedrock calls run in thread-pool executor).
    """

    def __init__(self, player: Player, region: str) -> None:
        self.player = player
        provider = detect_provider(player.model_id)
        if provider:
            _, base_url = provider
            self._client = OpenAICompatibleClient(
                model_id=player.model_id,
                api_key=player.api_key or "",
                base_url=base_url,
            )
        else:
            self._client = BedrockClient(
                model_id=player.model_id,
                region=region,
                aws_access_key_id=player.aws_access_key_id,
                aws_secret_access_key=player.aws_secret_access_key,
            )
        self._system: Optional[str] = None  # set once by engine after roles are known

    def set_system_prompt(self, allies: list[Player] | None = None) -> None:
        """Called by engine after role assignment."""
        p = self.player
        if p.role == RoleType.WEREWOLF:
            self._system = system_prompt_werewolf(p, allies or [])
        elif p.role == RoleType.VILLAGER:
            self._system = system_prompt_villager(p)
        elif p.role == RoleType.SEER:
            self._system = system_prompt_seer(p)
        elif p.role == RoleType.WITCH:
            self._system = system_prompt_witch(p)
        elif p.role == RoleType.HUNTER:
            self._system = system_prompt_hunter(p)

    # ------------------------------------------------------------------
    # Public action methods
    # ------------------------------------------------------------------

    async def speak(self, state: GameState) -> str:
        """Day-phase free speech."""
        ctx = build_game_context(state, self.player)
        prompt = (
            f"{ctx}\n\n"
            f"【你的任务】现在轮到你发言。请结合当前局势，发表你的看法或推理。"
            f"只输出发言内容，不超过80字，不要包含任何格式标记或角色名前缀。"
        )
        return await self._call(prompt)

    async def vote(self, state: GameState) -> int:
        """Day-phase vote. Returns player_id."""
        ctx = build_game_context(state, self.player)
        alive_ids = [p.id for p in state.alive_players() if p.id != self.player.id]
        prompt = (
            f"{ctx}\n\n"
            f"【你的任务】请投票驱逐你认为最可疑的玩家。"
            f"可选玩家编号：{alive_ids}。"
            f'只输出 JSON，格式为 {{"target_id": 编号}}，不要有任何其他文字。'
        )
        raw = await self._call(prompt)
        return self._parse_id(raw, alive_ids)

    async def werewolf_discuss(self, state: GameState) -> str:
        """Internal wolf discussion (private channel). Returns a short strategy note."""
        ctx = build_game_context(state, self.player)
        alive_good = state.alive_villager_side()
        targets = ", ".join(f"{p.id}号{p.name}" for p in alive_good)
        prompt = (
            f"{ctx}\n\n"
            f"【私狼频道】你正在与狼人同伴秘密商议今晚击杀目标。"
            f"当前好人阵营存活：{targets}。"
            f"请分析哪个目标对我们最有威胁，简要说明理由（50字以内）。"
            f"只输出你的分析，不要包含角色名前缀。"
        )
        return await self._call(prompt)

    async def werewolf_vote_kill(self, state: GameState) -> int:
        """Werewolf kill vote. Returns player_id."""
        ctx = build_game_context(state, self.player)
        alive_good = state.alive_villager_side()
        target_ids = [p.id for p in alive_good]
        prompt = (
            f"{ctx}\n\n"
            f"【私狼频道】请选择今晚要击杀的目标。"
            f"可选玩家编号：{target_ids}。"
            f'只输出 JSON，格式为 {{"target_id": 编号}}，不要有任何其他文字。'
        )
        raw = await self._call(prompt)
        return self._parse_id(raw, target_ids)

    async def seer_check(self, state: GameState) -> int:
        """Seer checks one player. Returns player_id."""
        ctx = build_game_context(state, self.player)
        # exclude already-checked players and self
        checked = set(state.seer_checks.keys())
        candidates = [
            p for p in state.alive_players()
            if p.id != self.player.id and p.id not in checked
        ]
        if not candidates:
            # all alive players already checked, pick any alive non-self
            candidates = [p for p in state.alive_players() if p.id != self.player.id]
        if not candidates:
            return -1
        candidate_ids = [p.id for p in candidates]
        prompt = (
            f"{ctx}\n\n"
            f"【预言家行动】今晚你可以查验一名玩家的阵营。"
            f"可查验玩家编号：{candidate_ids}。"
            f"请选择你最想查验的目标。"
            f'只输出 JSON，格式为 {{"target_id": 编号}}，不要有任何其他文字。'
        )
        raw = await self._call(prompt)
        return self._parse_id(raw, candidate_ids)

    async def witch_decide(self, state: GameState, kill_target: Optional[Player]) -> dict:
        """
        Witch decides tonight's action.
        Returns {"action": "save"|"poison"|"skip", "poison_target": Optional[int]}
        """
        ctx = build_game_context(state, self.player)
        kill_info = f"{kill_target.name}（{kill_target.id}号）" if kill_target else "无人被杀"

        options = []
        if not state.witch_save_used and kill_target:
            options.append('"save" - 使用解药救活被杀玩家')
        if not state.witch_poison_used:
            alive_others = [p for p in state.alive_players() if p.id != self.player.id]
            poison_ids = [p.id for p in alive_others]
            options.append(f'"poison" - 使用毒药（需指定 poison_target，可选：{poison_ids}）')
        options.append('"skip" - 什么都不做')

        prompt = (
            f"{ctx}\n\n"
            f"【女巫行动】今晚被狼人杀害的是：{kill_info}。\n"
            f"可选操作：\n" + "\n".join(f"  {o}" for o in options) + "\n\n"
            f'只输出 JSON，格式为 {{"action": "save"/"poison"/"skip", "poison_target": 编号或null}}，'
            f"不要有任何其他文字。"
        )
        raw = await self._call(prompt)
        return self._parse_witch_action(raw, state, kill_target)

    async def hunter_shoot(self, state: GameState) -> Optional[int]:
        """Hunter decides who to shoot on death. Returns player_id or None."""
        ctx = build_game_context(state, self.player)
        alive_others = [p for p in state.alive_players() if p.id != self.player.id]
        if not alive_others:
            return None
        target_ids = [p.id for p in alive_others]
        prompt = (
            f"{ctx}\n\n"
            f"【猎人技能】你已被淘汰，可以选择带走一名存活玩家，也可以放弃。"
            f"可选玩家编号：{target_ids}。"
            f'只输出 JSON，格式为 {{"target_id": 编号}} 或 {{"action": "skip"}}，不要有任何其他文字。'
        )
        raw = await self._call(prompt)
        return self._parse_hunter_shot(raw, target_ids)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call(self, user_content: str, max_tokens: int = 256) -> str:
        messages = [{"role": "user", "content": user_content}]
        try:
            result = await self._client.converse(
                messages=messages,
                system=self._system,
                max_tokens=max_tokens,
                temperature=0.85,
            )
            return result.strip()
        except Exception as e:
            logger.error("Agent %s LLM call failed: %s", self.player.name, e)
            return ""

    def _parse_id(self, raw: str, valid_ids: list[int]) -> int:
        """Parse a player_id from a JSON or plain-text response."""
        try:
            obj = json.loads(self._extract_json(raw))
            tid = int(obj.get("target_id", -1))
            if tid in valid_ids:
                return tid
        except Exception:
            pass
        # fallback: extract first number
        numbers = re.findall(r"\d+", raw)
        for n in numbers:
            if int(n) in valid_ids:
                return int(n)
        # random fallback
        logger.warning("Agent %s: could not parse id from %r, picking random", self.player.name, raw)
        return random.choice(valid_ids) if valid_ids else -1

    def _parse_witch_action(
        self, raw: str, state: GameState, kill_target: Optional[Player]
    ) -> dict:
        try:
            obj = json.loads(self._extract_json(raw))
            action = obj.get("action", "skip")
            if action == "save" and not state.witch_save_used and kill_target:
                return {"action": "save"}
            if action == "poison" and not state.witch_poison_used:
                alive_others = [p.id for p in state.alive_players() if p.id != self.player.id]
                pt = obj.get("poison_target")
                if pt and int(pt) in alive_others:
                    return {"action": "poison", "poison_target": int(pt)}
        except Exception:
            pass
        return {"action": "skip"}

    def _parse_hunter_shot(self, raw: str, valid_ids: list[int]) -> Optional[int]:
        try:
            obj = json.loads(self._extract_json(raw))
            if obj.get("action") == "skip":
                return None
            tid = int(obj.get("target_id", -1))
            if tid in valid_ids:
                return tid
        except Exception:
            pass
        numbers = re.findall(r"\d+", raw)
        for n in numbers:
            if int(n) in valid_ids:
                return int(n)
        return None  # hunter chooses not to shoot

    @staticmethod
    def _extract_json(text: str) -> str:
        """Extract JSON object from text that may contain extra prose."""
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        return match.group(0) if match else text
