"""
game/dnd_classes.py
Definições das classes DnD jogáveis.

Cada classe tem:
  - hp_base, armor_class_base
  - weapon principal + dados de dano (ex: "1d8" → roll_damage)
  - attack_range: distância máxima de ataque em tiles (distância de Chebyshev:
    max(|dx|, |dy|)). Range 1 = melee (8 células adjacentes).
  - lista de 3 abilidades, cada uma com um AbilityEffect declarativo
  - on_hit_modifiers: bónus passivos aplicados em cada ataque (basic e/ou ability)

Regras simplificadas (D&D 5e-inspired):
  - Ataque: d20 + attack_modifier vs AC do alvo
  - Dano:   dado de arma + damage_modifier (mínimo 1)
  - Range:  check feito no atacante (game_node) e validado no alvo (game_servicer)

──────────────────────────────────────────────────────────────────
ARQUITECTURA DE ABILIDADES
──────────────────────────────────────────────────────────────────
Cada Ability descreve o que faz através de um AbilityEffect com um `kind`:

  SELF_HEAL     — cura o próprio jogador (Second Wind, Cure Wounds, Lay on Hands)
  SELF_BUFF     — aplica um efeito activo local (Fortify, Mage Armor, Eagle Eye…)
  TARGET_DAMAGE — dano num alvo único, auto-acerto ou roll-to-hit
  TARGET_DRAIN  — dano num alvo + cura o atacante (Life Drain)
  MULTI_HIT     — N ataques separados no mesmo alvo (Flurry of Blows)
  AOE_DAMAGE    — ataca todos os inimigos no raio (Volley)
  TARGET_MARK   — marca um alvo com um efeito timed (Hunter's Mark, Hex)

OnHitModifier lista bónus passivos (por classe) que se activam quando um
determinado active_effect está presente:
  - basic_attack_only=True  → só se aplica no send_attack (não em abilidades)
  - basic_attack_only=False → aplica-se em send_attack E em abilidades de dano
"""

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ── DADOS ──────────────────────────────────────────────────────────

def roll(notation: str) -> int:
    """Lança dados no formato NdX (e.g. '2d6', '1d8'). Devolve o total."""
    n, x = notation.lower().split("d")
    return sum(random.randint(1, int(x)) for _ in range(int(n)))


def _max_roll(notation: str) -> int:
    """Devolve o valor máximo possível de uma notação de dados (e.g. '3d8' → 24)."""
    if not notation:
        return 0
    n, x = notation.lower().split("d")
    return int(n) * int(x)


def theoretical_max_damage(cls: "DnDClass") -> int:
    """
    Calcula o dano máximo teórico por AttackRequest para uma classe.
    Usado em validate_attack (host) para detectar trapaça.

    Considera ataque básico + on_hit_modifiers activos, e o pior caso de cada
    abilidade de dano. Para MULTI_HIT valida por hit individual (cada hit é um
    AttackRequest separado). Adiciona margem de +2 para variação aleatória.

    Esta função é a fonte de verdade: elimina a necessidade de manter
    MAX_DAMAGE_BY_CLASS hardcoded em lobby_manager.py.
    """
    # ── Ataque básico ──────────────────────────────────────────────
    base = _max_roll(cls.damage_dice) + cls.damage_modifier

    # On-hit modifiers que só se aplicam a ataques básicos
    basic_modifier_bonus = sum(
        _max_roll(m.damage_dice) for m in cls.on_hit_modifiers
        if m.damage_dice and m.basic_attack_only
    )
    # twin_weapon duplica o dado de arma base
    twin_bonus = sum(base for m in cls.on_hit_modifiers if m.twin_weapon)
    basic_max = base + basic_modifier_bonus + twin_bonus

    # On-hit modifiers não-básicos (aplicam-se também a abilidades de dano)
    non_basic_bonus = sum(
        _max_roll(m.damage_dice) for m in cls.on_hit_modifiers
        if m.damage_dice and not m.basic_attack_only
    )

    candidates = [basic_max]

    for ab in cls.abilities:
        if ab.effect is None:
            continue
        eff = ab.effect
        k   = eff.kind

        if k in (AbilityKind.TARGET_DAMAGE, AbilityKind.TARGET_DRAIN):
            dmg = _max_roll(eff.damage_dice) + eff.damage_fixed + non_basic_bonus
            candidates.append(dmg)

        elif k == AbilityKind.MULTI_HIT:
            # Cada hit chega como AttackRequest independente — validamos por hit
            per_hit = _max_roll(eff.damage_dice) + eff.damage_fixed
            candidates.append(per_hit)

        elif k == AbilityKind.AOE_DAMAGE:
            dmg = _max_roll(eff.damage_dice) + eff.damage_fixed
            if eff.use_class_damage_modifier:
                dmg += cls.damage_modifier
            dmg += non_basic_bonus
            candidates.append(dmg)

    return max(candidates) + 2  # margem anti-trapaça


# ── TIPOS DE EFEITO ────────────────────────────────────────────────

class AbilityKind(str, Enum):
    SELF_HEAL = "self_heal"
    SELF_BUFF = "self_buff"
    TARGET_DAMAGE = "target_damage"
    TARGET_DRAIN = "target_drain"
    MULTI_HIT = "multi_hit"
    AOE_DAMAGE = "aoe_damage"
    TARGET_MARK = "target_mark"


# ── EFEITO DE ABILIDADE ────────────────────────────────────────────

@dataclass
class AbilityEffect:
    """Descrição declarativa do que uma Ability faz quando activada."""
    kind: AbilityKind

    # ── SELF_HEAL ─────────────────────────────────────────────────
    heal_dice:  str = ""   # e.g. "1d10"
    heal_fixed: int = 0    # bónus fixo (e.g. +3) ou valor total (Lay on Hands)

    # ── SELF_BUFF ─────────────────────────────────────────────────
    effect_key:      str   = ""    # chave em LocalPlayer.active_effects
    effect_label:    str   = ""    # label em LocalPlayer.status_effects
    effect_duration: float = 0.0   # segundos; 0 = não expira sozinho (consume-on-hit)

    # ── TARGET_DAMAGE / TARGET_DRAIN / MULTI_HIT / AOE_DAMAGE ────
    damage_dice:  str  = ""    # e.g. "3d6"
    damage_fixed: int  = 0     # bónus fixo ao dano
    auto_hit:     bool = True  # attack_roll=999 → ignora AC do alvo
    weapon_name:  str  = ""    # nome mostrado no log; usa ab.name se vazio

    # MULTI_HIT
    hit_count: int = 1

    # AOE_DAMAGE
    aoe_radius:              int  = 0
    use_class_damage_modifier: bool = False  # soma lp.dnd_class.damage_modifier ao dano
    auto_hit_aoe:            bool = False    # override: force 999 no AOE (roll normal se False)

    # ── TARGET_DRAIN ──────────────────────────────────────────────
    drain_ratio: float = 0.5   # fracção do dano devolvida em HP

    # ── TARGET_MARK ───────────────────────────────────────────────
    mark_effect_key: str   = ""    # chave de efeito (e.g. "hunters_mark")
    mark_label:      str   = ""    # label visível (e.g. "Hunter's Mark")
    mark_duration:   float = 30.0  # segundos

    # ── DISPLAY ───────────────────────────────────────────────────
    emoji: str = "✨"


# ── MODIFICADOR ON-HIT ─────────────────────────────────────────────

@dataclass
class OnHitModifier:
    """
    Bónus passivo activado pelo próximo ataque enquanto o efeito estiver activo.

    Exemplos:
      Action Surge → +2d8 dano, consume=True, basic_attack_only=True
      Hunter's Mark → +1d6 dano, consume=False, basic_attack_only=False,
                      marked_target_only=True
    """
    effect_key: str

    damage_dice:  str  = ""    # e.g. "2d6"
    attack_dice:  str  = ""    # e.g. "1d4" (bónus ao attack roll)
    auto_hit:     bool = False  # força attack_roll = 999
    twin_weapon:  bool = False  # rola o dado de arma da classe uma vez extra

    consume:            bool = True   # remove o efeito após activar
    basic_attack_only:  bool = True   # False → aplica-se também a abilidades de dano
    marked_target_only: bool = False  # só se aplica se target == local.marked_target

    label:        str = ""  # texto no log, e.g. "Sneak Attack"
    status_label: str = ""  # label a remover de status_effects quando consumido


# ── DEFINIÇÃO DE CLASSE ────────────────────────────────────────────

@dataclass
class Ability:
    name:         str
    description:  str
    cooldown_secs: int  = 0
    is_spell:     bool  = False
    ability_range: int  = 0     # 0 = herda attack_range da classe; >0 = override
    effect: Optional[AbilityEffect] = field(default=None)


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
    on_hit_modifiers: List[OnHitModifier] = field(default_factory=list)

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
    "fighter": DnDClass(
        name="Fighter",
        hp_base=50,
        armor_class_base=16,
        weapon="Longsword",
        damage_dice="3d8",
        attack_modifier=5,
        damage_modifier=3,
        attack_range=1,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="action_surge",
                damage_dice="2d8",
                consume=True,
                basic_attack_only=True,
                label="Action Surge",
                status_label="+2d8 Surge↑",
            ),
        ],
        abilities=[
            Ability(
                "Second Wind", "Recupera 1d10+3 HP",
                cooldown_secs=30,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_HEAL,
                    heal_dice="1d10", heal_fixed=3,
                    emoji="⚔",
                ),
            ),
            Ability(
                "Action Surge", "Próximo ataque +2d8 dano extra",
                cooldown_secs=60,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="action_surge",
                    effect_label="+2d8 Surge↑",
                    effect_duration=120.0,  # consume-on-hit; expiry é safety net
                    emoji="⚔",
                ),
            ),
            Ability(
                "Fortify", "AC+2 durante 15s",
                cooldown_secs=45,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="fortified",
                    effect_label="Fortified",
                    effect_duration=15.0,
                    emoji="⚔",
                ),
            ),
        ],
    ),

    # ── WIZARD ────────────────────────────────────────────────────
    "wizard": DnDClass(
        name="Wizard",
        hp_base=30,
        armor_class_base=12,
        weapon="Ray of Frost",
        damage_dice="1d6",
        attack_modifier=4,
        damage_modifier=0,
        attack_range=4,
        abilities=[
            Ability(
                "Magic Missile",
                "3 dardos auto-acerto (1d4+1 cada) [uso: 1 <user>]",
                cooldown_secs=12,
                is_spell=True,
                ability_range=7,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    # 3*(1d4+1) = 3d4+3
                    damage_dice="3d4", damage_fixed=3,
                    auto_hit=True,
                    weapon_name="Magic Missile",
                    emoji="🔮",
                ),
            ),
            Ability(
                "Mage Armor", "AC passa a 13 durante 30s",
                cooldown_secs=60,
                is_spell=True,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="mage_armor",
                    effect_label="Mage Armor",
                    effect_duration=30.0,
                    emoji="🔮",
                ),
            ),
            Ability(
                "Fireball",
                "Bola de fogo 5d6 [uso: 3 <user>]",
                cooldown_secs=90,
                is_spell=True,
                ability_range=8,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    damage_dice="5d6",
                    auto_hit=True,
                    weapon_name="Fireball",
                    emoji="🔥",
                ),
            ),
        ],
    ),

    # ── ROGUE ─────────────────────────────────────────────────────
    "rogue": DnDClass(
        name="Rogue",
        hp_base=35,
        armor_class_base=14,
        weapon="Shortsword",
        damage_dice="2d6",
        attack_modifier=6,
        damage_modifier=2,
        attack_range=1,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="sneak_attack",
                damage_dice="2d6",
                consume=True,
                basic_attack_only=True,
                label="Sneak Attack",
                status_label="Sneak Attack↑",
            ),
            OnHitModifier(
                effect_key="cheap_shot",
                auto_hit=True,
                consume=True,
                basic_attack_only=True,
                label="Cheap Shot",
                status_label="Cheap Shot↑",
            ),
        ],
        abilities=[
            Ability(
                "Sneak Attack", "Próximo ataque +2d6 dano",
                cooldown_secs=18,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="sneak_attack",
                    effect_label="Sneak Attack↑",
                    effect_duration=120.0,
                    emoji="🗡",
                ),
            ),
            Ability(
                "Disengage", "Imune a ataques por 5s",
                cooldown_secs=20,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="disengaged",
                    effect_label="Disengaged",
                    effect_duration=5.0,
                    emoji="🗡",
                ),
            ),
            Ability(
                "Cheap Shot", "Próximo ataque é auto-acerto",
                cooldown_secs=30,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="cheap_shot",
                    effect_label="Cheap Shot↑",
                    effect_duration=120.0,
                    emoji="🗡",
                ),
            ),
        ],
    ),

    # ── CLERIC ────────────────────────────────────────────────────
    "cleric": DnDClass(
        name="Cleric",
        hp_base=45,
        armor_class_base=15,
        weapon="Mace",
        damage_dice="2d6",
        attack_modifier=4,
        damage_modifier=2,
        attack_range=1,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="blessed",
                attack_dice="1d4",
                consume=True,
                basic_attack_only=True,
                label="Blessed",
                status_label="Blessed",
            ),
        ],
        abilities=[
            Ability(
                "Cure Wounds", "Cura 1d8+3 HP em si próprio",
                cooldown_secs=25,
                is_spell=True,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_HEAL,
                    heal_dice="1d8", heal_fixed=3,
                    emoji="✨",
                ),
            ),
            Ability(
                "Sacred Flame",
                "Ataque radiante 1d8 auto-acerto [uso: 2 <user>]",
                cooldown_secs=4,
                is_spell=True,
                ability_range=5,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    damage_dice="1d8",
                    auto_hit=True,
                    weapon_name="Sacred Flame",
                    emoji="✨",
                ),
            ),
            Ability(
                "Bless", "+1d4 ao próximo attack roll [uso: 4 <user>]",
                cooldown_secs=35,
                is_spell=True,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="blessed",
                    effect_label="Blessed",
                    effect_duration=120.0,
                    emoji="✨",
                ),
            ),
        ],
    ),

    # ── RANGER ────────────────────────────────────────────────────
    "ranger": DnDClass(
        name="Ranger",
        hp_base=38,
        armor_class_base=14,
        weapon="Longbow",
        damage_dice="1d8",
        attack_modifier=6,
        damage_modifier=3,
        attack_range=10,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="hunters_mark",
                damage_dice="1d6",
                consume=False,          # dura enquanto o efeito estiver activo
                basic_attack_only=False,  # aplica-se também ao Volley
                marked_target_only=True,
                label="Hunter's Mark",
                status_label="",        # não se remove; expira por tempo
            ),
        ],
        abilities=[
            Ability(
                "Hunter's Mark",
                "+1d6 no alvo marcado por 30s [uso: 1 <user>]",
                cooldown_secs=35,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_MARK,
                    mark_effect_key="hunters_mark",
                    mark_label="Hunter's Mark",
                    mark_duration=30.0,
                    emoji="🏹",
                ),
            ),
            Ability(
                "Volley", "Ataca todos os alvos no raio 6",
                cooldown_secs=45,
                effect=AbilityEffect(
                    kind=AbilityKind.AOE_DAMAGE,
                    damage_dice="1d8",
                    auto_hit=False,
                    use_class_damage_modifier=True,
                    aoe_radius=6,
                    emoji="🏹",
                    weapon_name="Longbow",
                ),
            ),
            Ability(
                "Eagle Eye", "Duplica o alcance de ataque por 20s",
                cooldown_secs=50,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="eagle_eye",
                    effect_label="Eagle Eye",
                    effect_duration=20.0,
                    emoji="🏹",
                ),
            ),
        ],
    ),

    # ── SORCERER ──────────────────────────────────────────────────
    "sorcerer": DnDClass(
        name="Sorcerer",
        hp_base=36,
        armor_class_base=12,
        weapon="Eldritch Blast",
        damage_dice="1d8",
        attack_modifier=3,
        damage_modifier=1,
        attack_range=5,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="twinned_spell",
                twin_weapon=True,
                consume=True,
                basic_attack_only=True,
                label="Twinned Spell",
                status_label="Twinned↑",
            ),
        ],
        abilities=[
            Ability(
                "Chaos Bolt",
                "3d6 auto-acerto caótico [uso: 1 <user>]",
                cooldown_secs=15,
                is_spell=True,
                ability_range=7,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    damage_dice="3d6",
                    auto_hit=True,
                    weapon_name="Chaos Bolt",
                    emoji="🌀",
                ),
            ),
            Ability(
                "Twinned Spell", "Próximo ataque básico dispara duas vezes (+1d8)",
                cooldown_secs=22,
                is_spell=True,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="twinned_spell",
                    effect_label="Twinned↑",
                    effect_duration=30.0,
                    emoji="🌀",
                ),
            ),
            Ability(
                "Wild Magic Surge",
                "5d4 auto-acerto de magia selvagem [uso: 3 <user>]",
                cooldown_secs=55,
                is_spell=True,
                ability_range=6,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    damage_dice="5d4",
                    auto_hit=True,
                    weapon_name="Wild Magic Surge",
                    emoji="🌀",
                ),
            ),
        ],
    ),

    # ── PALADIN ───────────────────────────────────────────────────
    "paladin": DnDClass(
        name="Paladin",
        hp_base=52,
        armor_class_base=17,
        weapon="Warhammer",
        damage_dice="2d6",
        attack_modifier=4,
        damage_modifier=4,
        attack_range=1,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="divine_smite",
                damage_dice="4d6",
                consume=True,
                basic_attack_only=True,
                label="Divine Smite",
                status_label="Divine Smite↑",
            ),
        ],
        abilities=[
            Ability(
                "Divine Smite", "Próximo ataque +4d6 dano radiante",
                cooldown_secs=38,
                is_spell=True,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="divine_smite",
                    effect_label="Divine Smite↑",
                    effect_duration=120.0,
                    emoji="⚡",
                ),
            ),
            Ability(
                "Lay on Hands", "Cura 25 HP fixo",
                cooldown_secs=50,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_HEAL,
                    heal_fixed=25,
                    emoji="⚡",
                ),
            ),
            Ability(
                "Aura of Protection", "AC+3 durante 15s",
                cooldown_secs=60,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="aura_of_protection",
                    effect_label="Aura +3AC",
                    effect_duration=15.0,
                    emoji="⚡",
                ),
            ),
        ],
    ),

    # ── MONK ──────────────────────────────────────────────────────
    "monk": DnDClass(
        name="Monk",
        hp_base=42,
        armor_class_base=15,
        weapon="Unarmed Strike",
        damage_dice="1d6",
        attack_modifier=6,
        damage_modifier=2,
        attack_range=1,
        abilities=[
            Ability(
                "Flurry of Blows",
                "3 golpes auto-acerto 1d4+1 [uso: 1 <user>]",
                cooldown_secs=12,
                ability_range=1,
                effect=AbilityEffect(
                    kind=AbilityKind.MULTI_HIT,
                    damage_dice="1d4", damage_fixed=1,
                    hit_count=3,
                    auto_hit=True,
                    weapon_name="Unarmed Strike",
                    emoji="👊",
                ),
            ),
            Ability(
                "Patient Defense", "AC+3 durante 10s",
                cooldown_secs=18,
                effect=AbilityEffect(
                    kind=AbilityKind.SELF_BUFF,
                    effect_key="patient_defense",
                    effect_label="Patient Defense",
                    effect_duration=10.0,
                    emoji="👊",
                ),
            ),
            Ability(
                "Ki Blast",
                "2d8 auto-acerto de ki [uso: 3 <user>]",
                cooldown_secs=28,
                ability_range=3,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    damage_dice="2d8",
                    auto_hit=True,
                    weapon_name="Ki Blast",
                    emoji="👊",
                ),
            ),
        ],
    ),

    # ── WARLOCK ───────────────────────────────────────────────────
    # hex_mark aplica-se a TODOS os ataques contra o alvo marcado
    # (basic_attack_only=False), incluindo Eldritch Blast e Life Drain.
    "warlock": DnDClass(
        name="Warlock",
        hp_base=32,
        armor_class_base=13,
        weapon="Dagger",
        damage_dice="1d6",
        attack_modifier=5,
        damage_modifier=2,
        attack_range=1,
        on_hit_modifiers=[
            OnHitModifier(
                effect_key="hex_mark",
                damage_dice="1d6",
                consume=False,
                basic_attack_only=False,  # aplica-se em send_attack E em abilities
                marked_target_only=True,
                label="Hex",
                status_label="",
            ),
        ],
        abilities=[
            Ability(
                "Hex",
                "+1d6 nec. em cada ataque durante 25s [uso: 1 <user>]",
                cooldown_secs=22,
                is_spell=True,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_MARK,
                    mark_effect_key="hex_mark",
                    mark_label="Hex",
                    mark_duration=25.0,
                    emoji="🌑",
                ),
            ),
            Ability(
                "Eldritch Blast",
                "2d8 auto-acerto de força [uso: 2 <user>]",
                cooldown_secs=12,
                is_spell=True,
                ability_range=8,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DAMAGE,
                    damage_dice="2d8",
                    auto_hit=True,
                    weapon_name="Eldritch Blast",
                    emoji="🌑",
                ),
            ),
            Ability(
                "Life Drain",
                "2d6 auto-acerto; cura metade do dano [uso: 3 <user>]",
                cooldown_secs=30,
                is_spell=True,
                ability_range=1,
                effect=AbilityEffect(
                    kind=AbilityKind.TARGET_DRAIN,
                    damage_dice="2d6",
                    auto_hit=True,
                    drain_ratio=0.5,
                    weapon_name="Life Drain",
                    emoji="🌑",
                ),
            ),
        ],
    ),
}


def get_class(name: str) -> DnDClass | None:
    return CLASSES.get(name.lower())


def max_damage_for_class(name: str) -> int:
    """Devolve o dano máximo teórico para uma classe pelo nome. Fallback = 50."""
    cls = get_class(name)
    return theoretical_max_damage(cls) if cls else 50


def list_classes() -> str:
    lines = ["\n[Classes disponíveis]"]
    for key, c in CLASSES.items():
        lines.append(
            f"  \033[1m{c.name:<10}\033[0m  HP:{c.hp_base:>3}  "
            f"AC:{c.armor_class_base:>2}  Range:{c.attack_range:>2}  "
            f"Arma: {c.weapon} ({c.damage_dice})"
        )
    return "\n".join(lines) + "\n"