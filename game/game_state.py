"""
game/game_state.py
Estado local do jogo: jogador, mapa, outros jogadores e estado da partida.
"""

import time
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

from dnd_classes import DnDClass

# ── MAPA ──────────────────────────────────────────────────────────

MAP_W = 20
MAP_H = 10

MATCH_DURATION = 300   # segundos (5 minutos)


# ── ESTADO DA PARTIDA ─────────────────────────────────────────────

class MatchPhase(Enum):
    LOBBY    = auto()
    STARTING = auto()
    ACTIVE   = auto()
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

    # Cooldowns das abilidades: {ability_name → timestamp em que fica disponível}
    ability_cooldowns: Dict[str, float] = field(default_factory=dict)

    # Efeitos activos: {effect_name → timestamp de expiração}
    active_effects: Dict[str, float] = field(default_factory=dict)

    # Alvo do Hunter's Mark (Ranger)
    marked_target: str = ""

    def __post_init__(self):
        self.hp = self.dnd_class.hp_base

    @property
    def max_hp(self) -> int:
        return self.dnd_class.hp_base

    @property
    def armor_class(self) -> int:
        """AC base, modificada por efeitos activos."""
        base = self.dnd_class.armor_class_base
        if self.is_effect_active("mage_armor"):
            return max(base, 13)
        if self.is_effect_active("fortified"):
            return base + 2
        if self.is_effect_active("patient_defense"):
            return base + 3
        if self.is_effect_active("aura_of_protection"):
            return base + 3
        return base

    # ── COOLDOWNS ─────────────────────────────────────────────────

    def is_ability_ready(self, ability_name: str) -> bool:
        return time.time() >= self.ability_cooldowns.get(ability_name, 0)

    def cooldown_remaining(self, ability_name: str) -> float:
        return max(0.0, self.ability_cooldowns.get(ability_name, 0) - time.time())

    def use_ability(self, ability_name: str, cooldown_secs: int):
        if cooldown_secs > 0:
            self.ability_cooldowns[ability_name] = time.time() + cooldown_secs

    # ── EFEITOS ACTIVOS ───────────────────────────────────────────

    def is_effect_active(self, effect_name: str) -> bool:
        return time.time() < self.active_effects.get(effect_name, 0)

    def set_effect(self, effect_name: str, duration_secs: float, label: str = ""):
        """Activa um efeito por `duration_secs` segundos e adiciona label ao status_effects."""
        self.active_effects[effect_name] = time.time() + duration_secs
        if label and label not in self.status_effects:
            self.status_effects.append(label)

    def tick_effects(self):
        """Remove efeitos expirados e limpa o status_effects correspondente."""
        now = time.time()
        _labels = {
            "mage_armor":        "Mage Armor",
            "disengaged":        "Disengaged",
            "sneak_attack":      "Sneak Attack↑",
            "action_surge":      "+2d8 Surge↑",
            "hunters_mark":      "Hunter's Mark",
            "fortified":         "Fortified",
            "cheap_shot":        "Cheap Shot↑",
            "blessed":           "Blessed",
            "eagle_eye":         "Eagle Eye",
            # novas classes
            "twinned_spell":     "Twinned↑",
            "divine_smite":      "Divine Smite↑",
            "aura_of_protection":"Aura +3AC",
            "patient_defense":   "Patient Defense",
            "hex_mark":          "Hex",
        }
        expired = [k for k, v in self.active_effects.items() if now >= v]
        for k in expired:
            label = _labels.get(k)
            if label and label in self.status_effects:
                self.status_effects.remove(label)
            del self.active_effects[k]
        # Se Hunter's Mark ou Hex expiraram, limpa o alvo marcado
        if not self.is_effect_active("hunters_mark") and not self.is_effect_active("hex_mark"):
            self.marked_target = ""

    # ── DANO / CURA ───────────────────────────────────────────────

    def take_damage(self, amount: int) -> int:
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

    # ── RESET DE PARTIDA ──────────────────────────────────────────

    def reset_for_new_match(self, x: int, y: int):
        """Repõe o jogador para uma nova partida (kills/deaths a 0, HP cheio, sem cooldowns)."""
        self.hp     = self.max_hp
        self.alive  = True
        self.kills  = 0
        self.deaths = 0
        self.x, self.y = x, y
        self.status_effects.clear()
        self.ability_cooldowns.clear()
        self.active_effects.clear()
        self.marked_target = ""


# ── PEER REMOTO ───────────────────────────────────────────────────

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
        self.local:      LocalPlayer             = local
        self.others:     Dict[str, RemotePlayer] = {}
        self.phase:      MatchPhase              = MatchPhase.LOBBY
        self.match_id:   str                     = ""
        self.start_time: Optional[float]         = None
        self.event_log:  List[str]               = []

    # ── TEMPO ─────────────────────────────────────────────────────

    def time_remaining(self) -> int:
        if self.phase != MatchPhase.ACTIVE or self.start_time is None:
            return 0
        return max(0, int(MATCH_DURATION - (time.time() - self.start_time)))

    def check_match_end(self) -> bool:
        return self.phase == MatchPhase.ACTIVE and self.time_remaining() == 0

    # ── EVENTOS ───────────────────────────────────────────────────

    def log_event(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.event_log.append(f"[{ts}] {msg}")
        if len(self.event_log) > 8:
            self.event_log.pop(0)

    # ── ACTUALIZAR ESTADO REMOTO ──────────────────────────────────

    def update_remote(self, pb_state) -> None:
        nid      = pb_state.username
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

    # ── RESET REMOTES PARA NOVA PARTIDA ───────────────────────────

    def reset_others_for_new_match(self):
        for p in self.others.values():
            p.kills  = 0
            p.deaths = 0

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
        rows = [(self.local.username, self.local.kills, self.local.deaths)]
        for p in self.others.values():
            rows.append((p.username, p.kills, p.deaths))
        return sorted(rows, key=lambda r: -r[1])