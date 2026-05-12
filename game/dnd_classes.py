"""
game/dnd_classes.py
Definições das classes DnD jogáveis.

Cada classe tem:
  - hp_base, armor_class_base
  - weapon principal + dados de dano (ex: "1d8" → roll_damage)
  - attack_range: distância máxima de ataque em tiles (distância de Chebyshev:
    max(|dx|, |dy|)). Range 1 = melee (8 células adjacentes).
  - lista de 3 abilidades (nome + descrição + cooldown + alcance opcional)

Regras simplificadas (D&D 5e-inspired):
  - Ataque: d20 + attack_modifier vs AC do alvo
  - Dano:   dado de arma + damage_modifier (mínimo 1)
  - Range:  check feito no atacante (game_node) e validado no alvo (game_servicer)
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
    cooldown_secs: int = 0      # 0 = pode usar sempre (sem cooldown)
    is_spell: bool = False
    ability_range: int = 0      # 0 = herda o attack_range da classe; >0 = override


@dataclass
class DnDClass:
    name: str
    hp_base: int
    armor_class_base: int
    weapon: str
    damage_dice: str            # e.g. "3d8"
    attack_modifier: int        # bónus ao d20 de ataque
    damage_modifier: int        # bónus ao dado de dano
    attack_range: int           # tiles (Chebyshev). 1 = melee, 10 = longbow, etc.
    abilities: List[Ability] = field(default_factory=list)

    def roll_attack(self) -> int:
        return random.randint(1, 20) + self.attack_modifier

    def roll_damage(self) -> int:
        return max(1, roll(self.damage_dice) + self.damage_modifier)

    def effective_range(self, ability_name: str | None = None) -> int:
        """
        Devolve o alcance efectivo para um ataque normal ou para uma abilidade
        específica (se essa abilidade tiver ability_range > 0).
        """
        if ability_name:
            for ab in self.abilities:
                if ab.name == ability_name and ab.ability_range > 0:
                    return ab.ability_range
        return self.attack_range


# ── CLASSES DISPONÍVEIS ────────────────────────────────────────────

CLASSES: dict[str, DnDClass] = {

    # ── FIGHTER ───────────────────────────────────────────────────
    # Baseline de dano e tanque. Grande dano melee, boa AC e HP.
    # Fraqueza: range 1 — tem de estar mesmo ao lado.
    "fighter": DnDClass(
        name="Fighter",
        hp_base=50,
        armor_class_base=16,
        weapon="Longsword",
        damage_dice="3d8",          # 3–24 + 3 = 6–27 dano; média ~16.5
        attack_modifier=5,
        damage_modifier=3,
        attack_range=1,
        abilities=[
            Ability("Second Wind",  "Recupera 1d10+3 HP",                cooldown_secs=30),
            Ability("Action Surge", "Próximo ataque +2d8 dano extra",      cooldown_secs=60),
            Ability("Fortify",      "AC+2 durante 15s",                   cooldown_secs=45),
        ],
    ),

    # ── WIZARD ────────────────────────────────────────────────────
    # Fraco em melee, poderoso com spells. Ataque base é um soco (1d4).
    # Compensa com Magic Missile (spam curto alcance) e Fireball (nuke longo alcance).
    "wizard": DnDClass(
        name="Wizard",
        hp_base=30,
        armor_class_base=12,
        weapon="Ray of Frost",
        damage_dice="1d6",          # 1–6 + 0 = 1–6 dano; média ~3.5 — cantrip básico utilizável
        attack_modifier=4,          # feiticeiro sabe mirar, mas não luta a soco
        damage_modifier=0,
        attack_range=4,             # alcance curto-médio; preenche lacuna entre spells
        abilities=[
            Ability(
                "Magic Missile",
                "3 dardos auto-acerto (1d4+1 cada) [uso: 1 <user>]",
                cooldown_secs=12,
                is_spell=True,
                ability_range=7,    # alcance médio
            ),
            Ability(
                "Mage Armor",
                "AC passa a 13 durante 30s",
                cooldown_secs=60,
                is_spell=True,
            ),
            Ability(
                "Fireball",
                "Bola de fogo 5d6 [uso: 3 <user>]",
                cooldown_secs=90,   # nuke com grande cooldown
                is_spell=True,
                ability_range=8,    # alcance alto
            ),
        ],
    ),

    # ── ROGUE ─────────────────────────────────────────────────────
    # Alto ataque, dano médio. Mobilidade como kit defensivo.
    # Cheap Shot + Sneak Attack = burst combo.
    "rogue": DnDClass(
        name="Rogue",
        hp_base=35,
        armor_class_base=14,
        weapon="Shortsword",
        damage_dice="2d6",
        attack_modifier=6,
        damage_modifier=2,
        attack_range=1,
        abilities=[
            Ability("Sneak Attack", "Próximo ataque +2d6 dano",         cooldown_secs=18),
            Ability("Disengage",    "Imune a ataques por 5s",            cooldown_secs=20),
            Ability("Cheap Shot",   "Próximo ataque é auto-acerto",      cooldown_secs=30),
        ],
    ),

    # ── CLERIC ────────────────────────────────────────────────────
    # Tanque de suporte. Dano baixo mas cura, Sacred Flame a distância e Bless.
    "cleric": DnDClass(
        name="Cleric",
        hp_base=45,
        armor_class_base=15,
        weapon="Mace",
        damage_dice="2d6",
        attack_modifier=4,
        damage_modifier=2,
        attack_range=1,
        abilities=[
            Ability(
                "Cure Wounds",
                "Cura 1d8+3 HP em si próprio",
                cooldown_secs=25,
                is_spell=True,
            ),
            Ability(
                "Sacred Flame",
                "Ataque radiante 1d8 auto-acerto [uso: 2 <user>]",
                cooldown_secs=4,    # cantrip com cooldown mínimo — evita spam infinito à distância
                is_spell=True,
                ability_range=5,    # radiante à distância
            ),
            Ability(
                "Bless",
                "+1d4 ao próximo attack roll [uso: 4 <user>]",
                cooldown_secs=35,
                is_spell=True,
            ),
        ],
    ),

    # ── RANGER ────────────────────────────────────────────────────
    # O único com grande alcance base. Dano médio, Eagle Eye estende ainda mais.
    "ranger": DnDClass(
        name="Ranger",
        hp_base=38,
        armor_class_base=14,
        weapon="Longbow",
        damage_dice="1d8",
        attack_modifier=6,
        damage_modifier=3,
        attack_range=10,            # arco longo — domina distância
        abilities=[
            Ability(
                "Hunter's Mark",
                "+1d6 no alvo marcado por 30s [uso: 1 <user>]",
                cooldown_secs=35,
            ),
            Ability(
                "Volley",
                "Ataca todos os alvos no raio 6",
                cooldown_secs=45,
            ),
            Ability(
                "Eagle Eye",
                "Duplica o alcance de ataque por 20s",
                cooldown_secs=50,
            ),
        ],
    ),

    # ── SORCERER ──────────────────────────────────────────────────
    # Variação do Wizard: mais HP, ataque base ranged útil (Eldritch Blast),
    # spells caóticas e frequentes. Sem o poderio de Fireball, mas muito mais
    # sustentado. Twinned Spell duplica o próximo ataque básico.
    "sorcerer": DnDClass(
        name="Sorcerer",
        hp_base=36,
        armor_class_base=12,
        weapon="Eldritch Blast",
        damage_dice="1d8",          # 1–8 + 1 = 2–9; cantrip ranged sempre utilizável
        attack_modifier=3,
        damage_modifier=1,
        attack_range=5,             # range médio — preenche lacuna entre melee e Wizard
        abilities=[
            Ability(
                "Chaos Bolt",
                "3d6 auto-acerto caótico [uso: 1 <user>]",
                cooldown_secs=15,
                is_spell=True,
                ability_range=7,
            ),
            Ability(
                "Twinned Spell",
                "Próximo ataque básico dispara duas vezes (+1d8)",
                cooldown_secs=22,
                is_spell=True,
            ),
            Ability(
                "Wild Magic Surge",
                "5d4 auto-acerto de magia selvagem [uso: 3 <user>]",
                cooldown_secs=55,
                is_spell=True,
                ability_range=6,
            ),
        ],
    ),

    # ── PALADIN ───────────────────────────────────────────────────
    # O tanque com maior AC do jogo (17). Dano base sólido (2d6+4).
    # Divine Smite é o maior burst single-hit possível (+4d6), mas com cooldown
    # longo. Lay on Hands é o heal fixo mais alto do jogo. Joga lentamente.
    "paladin": DnDClass(
        name="Paladin",
        hp_base=52,
        armor_class_base=17,
        weapon="Warhammer",
        damage_dice="2d6",          # 2–12 + 4 = 6–16; dano base alto
        attack_modifier=4,
        damage_modifier=4,
        attack_range=1,
        abilities=[
            Ability(
                "Divine Smite",
                "Próximo ataque +4d6 dano radiante",
                cooldown_secs=38,
                is_spell=True,
            ),
            Ability("Lay on Hands",      "Cura 25 HP fixo",      cooldown_secs=50),
            Ability("Aura of Protection","AC+3 durante 15s",     cooldown_secs=60),
        ],
    ),

    # ── MONK ──────────────────────────────────────────────────────
    # O mais veloz em melee. Flurry of Blows (3 hits auto-acerto) é o maior
    # DPS por janela de tempo do jogo se bem encadeado. Sem armadura pesada,
    # compensa com Patient Defense. Ki Blast dá opção ranged curta.
    "monk": DnDClass(
        name="Monk",
        hp_base=42,
        armor_class_base=15,
        weapon="Unarmed Strike",
        damage_dice="1d6",          # 1–6 + 2 = 3–8; soco simples
        attack_modifier=6,
        damage_modifier=2,
        attack_range=1,
        abilities=[
            Ability(
                "Flurry of Blows",
                "3 golpes auto-acerto 1d4+1 [uso: 1 <user>]",
                cooldown_secs=12,
                ability_range=1,
            ),
            Ability("Patient Defense", "AC+3 durante 10s",       cooldown_secs=18),
            Ability(
                "Ki Blast",
                "2d8 auto-acerto de ki [uso: 3 <user>]",
                cooldown_secs=28,
                ability_range=3,
            ),
        ],
    ),

    # ── WARLOCK ───────────────────────────────────────────────────
    # Mistura de Rogue e Wizard. Dagger em melee, Eldritch Blast a longa
    # distância. Hex amplifica todos os ataques durante 25s (+1d6 cada).
    # Life Drain rouba HP do alvo — único sustain ofensivo do jogo.
    "warlock": DnDClass(
        name="Warlock",
        hp_base=32,
        armor_class_base=13,
        weapon="Dagger",
        damage_dice="1d6",          # 1–6 + 2 = 3–8; rápida mas fraca sem Hex
        attack_modifier=5,
        damage_modifier=2,
        attack_range=1,
        abilities=[
            Ability(
                "Hex",
                "+1d6 nec. em cada ataque durante 25s [uso: 1 <user>]",
                cooldown_secs=22,
                is_spell=True,
            ),
            Ability(
                "Eldritch Blast",
                "2d8 auto-acerto de força [uso: 2 <user>]",
                cooldown_secs=12,
                is_spell=True,
                ability_range=8,
            ),
            Ability(
                "Life Drain",
                "2d6 auto-acerto; cura metade do dano [uso: 3 <user>]",
                cooldown_secs=30,
                is_spell=True,
                ability_range=1,
            ),
        ],
    ),
}



    # ── SORCERER ──────────────────────────────────────────────────
    # Variação do Wizard: mais HP, ataque base ranged útil (Eldritch Blast),
    # spells caóticas e frequentes. Sem o poderio de Fireball, mas muito mais
    # sustentado. Twinned Spell duplica o próximo ataque básico.
    


def get_class(name: str) -> DnDClass | None:
    return CLASSES.get(name.lower())


def list_classes() -> str:
    lines = ["\n[Classes disponíveis]"]
    for key, c in CLASSES.items():
        lines.append(
            f"  \033[1m{c.name:<10}\033[0m  HP:{c.hp_base:>3}  "
            f"AC:{c.armor_class_base:>2}  Range:{c.attack_range:>2}  "
            f"Arma: {c.weapon} ({c.damage_dice})"
        )
    return "\n".join(lines) + "\n"