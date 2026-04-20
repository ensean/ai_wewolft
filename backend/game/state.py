from __future__ import annotations
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
from dataclasses import dataclass, field
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Enums & Pydantic config models (used by API)
# ---------------------------------------------------------------------------

class RoleType(str, Enum):
    WEREWOLF = "werewolf"
    VILLAGER = "villager"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"


class GameStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    FINISHED = "finished"


class EventType(str, Enum):
    GAME_START = "game_start"
    ROLE_ASSIGN = "role_assign"
    PHASE_START = "phase_start"
    SPEECH = "speech"
    VOTE = "vote"
    DEATH = "death"
    SEER_RESULT = "seer_result"
    WITCH_ACTION = "witch_action"
    HUNTER_SHOT = "hunter_shot"
    WOLF_DISCUSS = "wolf_discuss"
    GAME_END = "game_end"
    ERROR = "error"
    SYSTEM = "system"
    LAST_WORDS = "last_words"
    VOTE_TALLY = "vote_tally"
    SHERIFF_ELECTED = "sheriff_elected"   # reserved for future use
    HUMAN_INPUT_REQUIRED = "human_input_required"
    HUMAN_INPUT_DONE = "human_input_done"
    HUMAN_ROLE_REVEAL = "human_role_reveal"


# ---------------------------------------------------------------------------
# Pydantic models for API request/response
# ---------------------------------------------------------------------------

class PlayerConfig(BaseModel):
    name: str
    model_id: str = ""
    is_human: bool = False
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    api_key: Optional[str] = None          # for OpenAI-compatible providers (Kimi/DeepSeek/MiniMax/GLM)


class GameConfig(BaseModel):
    player_configs: list[PlayerConfig]
    aws_region: str = "us-east-1"
    # Cheap Bedrock model used for structured decisions (votes, kill targets, checks).
    # Speeches still use each player's own model. Set to null to disable tiering.
    quick_model_id: Optional[str] = "us.amazon.nova-lite-v1:0"


# ---------------------------------------------------------------------------
# Runtime data classes (mutable, not Pydantic)
# ---------------------------------------------------------------------------

@dataclass
class Player:
    id: int                        # 1-based
    name: str
    role: RoleType
    model_id: str
    is_alive: bool = True
    is_human: bool = False
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    api_key: Optional[str] = None          # for OpenAI-compatible providers
    # runtime only: set by engine after construction
    agent: Any = field(default=None, repr=False)

    @property
    def role_label(self) -> str:
        labels = {
            RoleType.WEREWOLF: "狼人",
            RoleType.VILLAGER: "村民",
            RoleType.SEER: "预言家",
            RoleType.WITCH: "女巫",
            RoleType.HUNTER: "猎人",
        }
        return labels[self.role]


@dataclass
class GameEvent:
    type: EventType
    round: int
    data: dict[str, Any]
    public: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class GameState:
    game_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    players: list[Player] = field(default_factory=list)
    round: int = 0
    phase: str = "waiting"          # "night" | "day" | "waiting" | "finished"
    status: GameStatus = GameStatus.WAITING
    event_log: list[GameEvent] = field(default_factory=list)
    winner: Optional[str] = None    # "werewolves" | "villagers"

    # ---- night scratch pad (cleared each round) ----
    night_kill_target: Optional[int] = None     # player.id
    witch_save_used: bool = False
    witch_poison_used: bool = False
    witch_poison_target: Optional[int] = None   # player.id
    witch_saved_tonight: bool = False            # did witch save the kill target?
    seer_checks: dict[int, RoleType] = field(default_factory=dict)  # player_id -> role
    hunter_triggered: bool = False              # whether hunter can shoot

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.is_alive]

    def alive_werewolves(self) -> list[Player]:
        return [p for p in self.players if p.is_alive and p.role == RoleType.WEREWOLF]

    def alive_villager_side(self) -> list[Player]:
        return [p for p in self.players if p.is_alive and p.role != RoleType.WEREWOLF]

    def get_player(self, player_id: int) -> Optional[Player]:
        for p in self.players:
            if p.id == player_id:
                return p
        return None

    def check_win_condition(self) -> Optional[str]:
        wolves = len(self.alive_werewolves())
        good = len(self.alive_villager_side())
        if wolves == 0:
            return "villagers"
        if wolves >= good:
            return "werewolves"
        return None

    def reset_night_scratch(self) -> None:
        self.night_kill_target = None
        self.witch_saved_tonight = False
        self.witch_poison_target = None
        self.hunter_triggered = False

    def make_event(
        self,
        etype: EventType,
        data: dict[str, Any],
        public: bool = True,
    ) -> GameEvent:
        ev = GameEvent(type=etype, round=self.round, data=data, public=public)
        self.event_log.append(ev)
        return ev
