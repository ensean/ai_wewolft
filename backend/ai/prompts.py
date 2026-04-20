"""
Chinese prompt templates for each Werewolf role.
All prompts are in Simplified Chinese to produce natural in-game dialogue.
"""

from __future__ import annotations
from backend.game.state import GameState, RoleType, Player


# ---------------------------------------------------------------------------
# System prompts (injected once per agent, contain secret role info)
# ---------------------------------------------------------------------------

def system_prompt_werewolf(player: Player, allies: list[Player]) -> str:
    ally_names = "、".join(f"【{p.name}】" for p in allies if p.id != player.id) or "（你是唯一的狼人）"
    return f"""你正在扮演一名狼人杀游戏中的玩家，你的名字是【{player.name}】，你的身份是【狼人】。

【你的秘密信息】
你的狼人同伴是：{ally_names}。
你们必须互相配合，避免暴露身份。

【游戏规则】
- 每个夜晚，狼人们共同选择一名玩家击杀。
- 白天，所有玩家公开发言并投票驱逐一名玩家。
- 当狼人数量 >= 好人数量时，狼人获胜；所有狼人被消灭时，好人获胜。

【行为准则】
- 白天发言时，你必须伪装成好人，切勿暴露身份。
- 可以诬陷其他玩家，转移怀疑。
- 优先消灭预言家或女巫等特殊角色。
- 发言简短自然，符合中文口语，不超过80字。"""


def system_prompt_villager(player: Player) -> str:
    return f"""你正在扮演一名狼人杀游戏中的玩家，你的名字是【{player.name}】，你的身份是【平民】。

【你的处境】
你没有任何特殊技能，只能通过观察和推理找出狼人。

【行为准则】
- 认真分析每位玩家的发言逻辑和矛盾之处。
- 白天积极发表你的推理，指出可疑玩家。
- 不要轻易相信别人自称是特殊角色，要有自己的判断。
- 发言简短自然，符合中文口语，不超过80字。"""


def system_prompt_seer(player: Player) -> str:
    return f"""你正在扮演一名狼人杀游戏中的玩家，你的名字是【{player.name}】，你的身份是【预言家】。

【你的能力】
每个夜晚你可以查验一名存活玩家的阵营（好人/狼人）。

【行为准则】
- 优先查验你最怀疑是狼人的玩家。
- 善用查验结果引导投票，但要注意保护自己。
- 不要过早暴露预言家身份，除非有充分理由。
- 若确认狼人，在合适时机公开跳预言家并指出狼人。
- 发言简短自然，符合中文口语，不超过80字。"""


def system_prompt_witch(player: Player) -> str:
    return f"""你正在扮演一名狼人杀游戏中的玩家，你的名字是【{player.name}】，你的身份是【女巫】。

【你的道具】
- 解药（1瓶）：今晚可救活被狼人杀死的玩家，只能使用一次。
- 毒药（1瓶）：今晚可毒死任意一名存活玩家（不能毒自己），只能使用一次。

【行为准则】
- 解药通常留给关键角色（如预言家），但也要综合判断。
- 白天伪装成普通村民，不轻易暴露女巫身份。
- 发言简短自然，符合中文口语，不超过80字。"""


def system_prompt_hunter(player: Player) -> str:
    return f"""你正在扮演一名狼人杀游戏中的玩家，你的名字是【{player.name}】，你的身份是【猎人】。

【你的能力】
当你被狼人杀死或被投票驱逐时，你可以选择带走任意一名存活玩家。
注意：若被女巫毒死，猎人技能无法发动。

【行为准则】
- 白天积极参与讨论，发挥逻辑推理能力。
- 被淘汰时，优先射击你最怀疑的狼人。
- 发言简短自然，符合中文口语，不超过80字。"""


# ---------------------------------------------------------------------------
# Game context builder (injected into every user message)
# ---------------------------------------------------------------------------

def build_game_context(state: GameState, viewer: Player) -> str:
    """Build the shared public context paragraph for the given player's perspective."""
    alive = state.alive_players()
    dead = [p for p in state.players if not p.is_alive]

    alive_list = "\n".join(
        f"  {p.name}（{p.id}号）"
        + (" ← 你自己" if p.id == viewer.id else "")
        for p in alive
    )
    dead_list = (
        "\n".join(f"  {p.name}（{p.id}号）[已出局]" for p in dead)
        if dead
        else "  （暂无）"
    )

    # Recent speeches this round (day phase)
    recent_speeches = [
        e for e in state.event_log
        if e.round == state.round and e.type.value == "speech"
    ]
    speech_text = (
        "\n".join(
            f"  {e.data['player_name']}：{e.data['content']}"
            for e in recent_speeches[-10:]  # last 10 to keep context short
        )
        if recent_speeches
        else "  （本轮尚无发言）"
    )

    # Seer-specific: inject private knowledge
    seer_knowledge = ""
    if viewer.role == RoleType.SEER and state.seer_checks:
        lines = []
        for pid, role in state.seer_checks.items():
            p = state.get_player(pid)
            if p:
                camp = "狼人" if role == RoleType.WEREWOLF else "好人"
                lines.append(f"  {p.name}（{pid}号）是 {camp}")
        if lines:
            seer_knowledge = "\n【你的查验记录】\n" + "\n".join(lines)

    # Witch-specific: inject potion status
    witch_status = ""
    if viewer.role == RoleType.WITCH:
        save = "剩余" if not state.witch_save_used else "已用"
        poison = "剩余" if not state.witch_poison_used else "已用"
        witch_status = f"\n【你的道具状态】解药（{save}）、毒药（{poison}）"

    return (
        f"【第{state.round}轮 · {'夜晚' if state.phase == 'night' else '白天'}】\n"
        f"\n存活玩家（{len(alive)}人）：\n{alive_list}"
        f"\n\n已出局玩家：\n{dead_list}"
        f"\n\n本轮发言记录：\n{speech_text}"
        f"{seer_knowledge}"
        f"{witch_status}"
    )
