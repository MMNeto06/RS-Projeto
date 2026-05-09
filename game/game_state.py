"""
game/game_state.py
Estado local do jogo: jogador, mapa, outros jogadores e estado da partida.

GameState é o "cérebro" do cliente — guarda tudo o que o renderer precisa
de mostrar e que o GameNode precisa de enviar via gRPC.
"""

import time
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

from dnd_classes import DnDClass, get_class

# ── MAPA ──────────────────────────────────────────────────────────

MAP_W = 20
MAP_H = 10

MATCH_DURATION = 600   # 10 minutos em segundos


# ── ESTADO DA PARTIDA ─────────────────────────────────────────────

class MatchPhase(Enum):
    LOBBY    = auto()
    STARTING = auto()   # contagem decrescente
    ACTIVE   = auto()   # em jogo
    ENDED    = auto()


# ── JOGADOR LOCAL ─────────────────────────────────────────────────

@dataclass
class LocalPlayer:
    username:   str
    node_id:    str
    dnd_class:  DnDClass

    hp:         int   = 0
    x:          int   = 0
    y:          int   = 0
    alive:      bool  = True
    kills:      int   = 0
    deaths:     int   = 0
    match_id:   str   = ""
    status_effects: List[str] = field(default_factory=list)

    # Cooldowns das abilidades: {ability_name → turno em que fica disponível}
    ability_cooldowns: Dict[str, int] = field(default_factory=dict)
    _turn: int = 0

    def __post_init__(self):
        self.hp = self.dnd_class.hp_base

    @property
    def max_hp(self) -> int:
        return self.dnd_class.hp_base

    @property
    def armor_class(self) -> int:
        return self.dnd_class.armor_class_base

    def take_damage(self, amount: int) -> int:
        """Aplica dano e devolve o HP restante."""
        self.hp = max(0, self.hp - amount)
        if self.hp == 0:
            self.alive = False
            self.deaths += 1
        return self.hp

    def heal(self, amount: int) -> int:
        if self.alive:
            self.hp = min(self.max_hp, self.hp + amount)
        return self.hp

    def respawn(self, x: int, y: int):
        self.hp    = self.max_hp
        self.alive = True
        self.x, self.y = x, y
        self.status_effects.clear()

    def is_ability_ready(self, ability_name: str) -> bool:
        ready_at = self.ability_cooldowns.get(ability_name, 0)
        return self._turn >= ready_at

    def use_ability(self, ability_name: str, cooldown_turns: int):
        self.ability_cooldowns[ability_name] = self._turn + cooldown_turns

    def advance_turn(self):
        self._turn += 1


# ── PEER REMOTO (estado recebido via StateUpdate) ─────────────────

@dataclass
class RemotePlayer:
    username:       str
    node_id:        str
    grpc_addr:      str
    hp:             int
    max_hp:         int
    x:              int
    y:              int
    alive:          bool
    class_name:     str
    armor_class:    int
    kills:          int
    deaths:         int
    status_effects: List[str] = field(default_factory=list)
    last_seen:      float = field(default_factory=time.time)


# ── ESTADO GLOBAL DA PARTIDA ──────────────────────────────────────

class GameState:
    def __init__(self, local: LocalPlayer):
        self.local:  LocalPlayer                = local
        self.others: Dict[str, RemotePlayer]    = {}  # node_id → RemotePlayer
        self.phase:  MatchPhase                 = MatchPhase.LOBBY
        self.match_id: str                      = ""
        self.start_time: Optional[float]        = None
        self.event_log: List[str]               = []   # últimas N mensagens de evento

    # ── TEMPO ─────────────────────────────────────────────────────

    def time_remaining(self) -> int:
        """Segundos restantes da partida (0 se não iniciada ou terminada)."""
        if self.phase != MatchPhase.ACTIVE or self.start_time is None:
            return 0
        elapsed = time.time() - self.start_time
        return max(0, int(MATCH_DURATION - elapsed))

    def check_match_end(self) -> bool:
        """Devolve True se a partida deve terminar (tempo esgotado)."""
        return self.phase == MatchPhase.ACTIVE and self.time_remaining() == 0

    # ── EVENTOS ───────────────────────────────────────────────────

    def log_event(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.event_log.append(entry)
        if len(self.event_log) > 8:   # manter só as últimas 8 linhas
            self.event_log.pop(0)

    # ── ACTUALIZAR ESTADO REMOTO ──────────────────────────────────

    def update_remote(self, pb_state) -> None:
        """Recebe um game_pb2.PlayerState e actualiza o dict de others."""
        nid = pb_state.username   # usamos username como chave temporária
        # (idealmente seria node_id, mas o proto usa username como campo único)
        existing = self.others.get(nid)
        self.others[nid] = RemotePlayer(
            username=pb_state.username,
            node_id=nid,
            grpc_addr=existing.grpc_addr if existing else "",
            hp=pb_state.hp,
            max_hp=pb_state.max_hp,
            x=pb_state.x,
            y=pb_state.y,
            alive=pb_state.alive,
            class_name=pb_state.class_name,
            armor_class=pb_state.armor_class,
            kills=pb_state.kills,
            deaths=pb_state.deaths,
            status_effects=list(pb_state.status_effects),
            last_seen=time.time(),
        )

    # ── SPAWN ALEATÓRIO ───────────────────────────────────────────

    def random_spawn(self) -> tuple[int, int]:
        occupied = {(p.x, p.y) for p in self.others.values()}
        occupied.add((self.local.x, self.local.y))
        while True:
            x = random.randint(0, MAP_W - 1)
            y = random.randint(0, MAP_H - 1)
            if (x, y) not in occupied:
                return x, y

    # ── SCOREBOARD ────────────────────────────────────────────────

    def scoreboard(self) -> List[tuple[str, int, int]]:
        """Devolve [(username, kills, deaths), …] ordenado por kills."""
        rows = [(self.local.username, self.local.kills, self.local.deaths)]
        for p in self.others.values():
            rows.append((p.username, p.kills, p.deaths))
        return sorted(rows, key=lambda r: -r[1])