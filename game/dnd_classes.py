"""
game/dnd_classes.py
Definições das classes DnD jogáveis.

Cada classe tem:
  - hp_base, armor_class_base
  - weapon principal + dados de dano (ex: "1d8" → roll_damage)
  - lista de abilidades (nome + descrição curta para mostrar no UI)
  - damage_type padrão

Regras simplificadas (D&D 5e-inspired):
  - Ataque: d20 + modificador vs AC do alvo
  - Dano:   dado de arma + modificador
"""

import random
from dataclasses import dataclass, field
from typing import List


# ── DADOS ──────────────────────────────────────────────────────────

def roll(notation: str) -> int:
    """Lança dados no formato NdX (e.g. '2d6', '1d8'). Devolve o total."""
    n, x = notation.lower().split("d")
    return sum(random.randint(1, int(x)) for _ in range(int(n)))


# ── DEFINIÇÃO DE CLASSE ────────────────────────────────────────────

@dataclass
class Ability:
    name: str
    description: str
    cooldown_turns: int = 0   # 0 = pode usar sempre
    is_spell: bool = False


@dataclass
class DnDClass:
    name: str
    hp_base: int
    armor_class_base: int
    weapon: str
    damage_dice: str           # e.g. "1d8"
    damage_type: str           # e.g. "slashing"
    attack_modifier: int       # bónus ao d20 de ataque
    damage_modifier: int       # bónus ao dado de dano
    abilities: List[Ability] = field(default_factory=list)

    def roll_attack(self) -> int:
        return random.randint(1, 20) + self.attack_modifier

    def roll_damage(self) -> int:
        return max(1, roll(self.damage_dice) + self.damage_modifier)


# ── CLASSES DISPONÍVEIS ────────────────────────────────────────────

CLASSES: dict[str, DnDClass] = {

    "fighter": DnDClass(
        name="Fighter",
        hp_base=60,
        armor_class_base=16,
        weapon="Longsword",
        damage_dice="1d8",
        damage_type="slashing",
        attack_modifier=5,
        damage_modifier=3,
        abilities=[
            Ability("Second Wind",   "Recupera 1d10+3 HP",           cooldown_turns=5),
            Ability("Action Surge",  "+1 ataque extra neste turno",   cooldown_turns=6),
        ],
    ),

    "wizard": DnDClass(
        name="Wizard",
        hp_base=35,
        armor_class_base=12,
        weapon="Fireball",
        damage_dice="3d6",
        damage_type="fire",
        attack_modifier=7,
        damage_modifier=4,
        abilities=[
            Ability("Magic Missile", "3 dardos de força (1d4+1 cada)",  is_spell=True),
            Ability("Mage Armor",    "AC torna-se 13 por 3 turnos",      cooldown_turns=8, is_spell=True),
        ],
    ),

    "rogue": DnDClass(
        name="Rogue",
        hp_base=45,
        armor_class_base=14,
        weapon="Shortsword",
        damage_dice="1d6",
        damage_type="piercing",
        attack_modifier=6,
        damage_modifier=2,
        abilities=[
            Ability("Sneak Attack",  "+2d6 dano se aliado adjacente ao alvo", cooldown_turns=0),
            Ability("Disengage",     "Sai de combate sem provocar ataques",   cooldown_turns=3),
        ],
    ),

    "cleric": DnDClass(
        name="Cleric",
        hp_base=50,
        armor_class_base=15,
        weapon="Mace",
        damage_dice="1d6",
        damage_type="bludgeoning",
        attack_modifier=4,
        damage_modifier=2,
        abilities=[
            Ability("Cure Wounds",   "Cura 1d8+3 HP em si próprio",          cooldown_turns=4, is_spell=True),
            Ability("Sacred Flame",  "Ataque sagrado 1d8 radiante à distância", cooldown_turns=0, is_spell=True),
        ],
    ),

    "ranger": DnDClass(
        name="Ranger",
        hp_base=48,
        armor_class_base=14,
        weapon="Longbow",
        damage_dice="1d8",
        damage_type="piercing",
        attack_modifier=6,
        damage_modifier=3,
        abilities=[
            Ability("Hunter's Mark", "+1d6 dano no alvo marcado por 3 turnos", cooldown_turns=5),
            Ability("Volley",        "Ataca todos os alvos num raio 2",         cooldown_turns=6),
        ],
    ),
}


def get_class(name: str) -> DnDClass | None:
    return CLASSES.get(name.lower())


def list_classes() -> str:
    lines = ["\n[Classes disponíveis]"]
    for key, c in CLASSES.items():
        lines.append(
            f"  \033[1m{c.name:<10}\033[0m  HP:{c.hp_base:>3}  "
            f"AC:{c.armor_class_base:>2}  Arma: {c.weapon} ({c.damage_dice} {c.damage_type})"
        )
    return "\n".join(lines) + "\n"