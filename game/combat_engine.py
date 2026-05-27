import game_pb2

from dnd_classes import AbilityEffect, AbilityKind, roll
from game_state import GameState
from utils import chebyshev


class CombatEngine:
    def __init__(self, state: GameState, peers: dict, route_attack_fn):
        self.state = state
        self._peers = peers
        self._route_attack = route_attack_fn

    def find_peer(self, username: str) -> dict | None:
        target_lower = username.lower()
        return next(
            (v for v in self._peers.values()
             if v.get("username", "").lower() == target_lower),
            None,
        )

    def check_range(self, target_username: str, effective_range: int) -> bool:
        target_state = self.state.others.get(target_username)
        if target_state is None:
            return True
        lp = self.state.local
        dist = chebyshev(lp.x, lp.y, target_state.x, target_state.y)
        if dist > effective_range:
            self.state.log_event(
                f"[!] {target_username} está fora de alcance "
                f"(dist={dist}, range={effective_range})."
            )
            return False
        return True

    # ── ON-HIT MODIFIER ENGINE ────────────────────────────────────

    def apply_on_hit_modifiers(
        self,
        target_username: str,
        attack_roll: int,
        damage_roll: int,
        is_ability: bool = False,
    ) -> tuple[int, int, list[str]]:

        lp = self.state.local
        extra_notes: list[str] = []

        for mod in lp.dnd_class.on_hit_modifiers:
            if is_ability and mod.basic_attack_only:
                continue
            if not lp.is_effect_active(mod.effect_key):
                continue
            if mod.marked_target_only and \
                    lp.marked_target.lower() != target_username.lower():
                continue

            if mod.auto_hit:
                attack_roll = 999
            if mod.damage_dice:
                b = roll(mod.damage_dice)
                damage_roll += b
                extra_notes.append(f"{mod.label} +{b}")
            if mod.attack_dice:
                b = roll(mod.attack_dice)
                attack_roll += b
                extra_notes.append(f"{mod.label} +{b}")
            if mod.twin_weapon:
                b = lp.dnd_class.roll_damage()
                damage_roll += b
                extra_notes.append(f"{mod.label} +{b}")
            if mod.consume:
                lp.active_effects.pop(mod.effect_key, None)
                if mod.status_label and mod.status_label in lp.status_effects:
                    lp.status_effects.remove(mod.status_label)

        return attack_roll, damage_roll, extra_notes


    async def send_attack(self, target_username: str):
        peer = self.find_peer(target_username)
        if not peer:
            self.state.log_event(f"[!] Jogador '{target_username}' não encontrado.")
            return None

        lp = self.state.local
        attack_roll = lp.dnd_class.roll_attack()
        damage_roll = lp.dnd_class.roll_damage()

        attack_roll, damage_roll, extra_notes = self.apply_on_hit_modifiers(
            target_username, attack_roll, damage_roll, is_ability=False
        )

        # Eagle Eye: duplica o alcance do ataque básico
        effective_rng = lp.dnd_class.attack_range
        if lp.is_effect_active("eagle_eye"):
            effective_rng *= 2

        if not self.check_range(target_username, effective_rng):
            return None

        req = game_pb2.AttackRequest(
            attacker=lp.username,
            attacker_class=lp.dnd_class.name,
            weapon=lp.dnd_class.weapon,
            attack_roll=attack_roll,
            base_damage_roll=damage_roll,
            target=target_username,
            is_spell=False,
            attacker_x=lp.x,
            attacker_y=lp.y,
            effective_range=effective_rng,
            ability_name="",
        )
        result = await self._route_attack(req, peer)

        if result is None:
            self.state.log_event("[!] Ataque falhou.")
            return None

        extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
        if result.hit:
            lp.kills += (1 if result.target_died else 0)
            self.state.log_event(
                f"Atacaste {target_username} com {lp.dnd_class.weapon} "
                f"-> {result.damage_dealt} dano{extras}"
                + (" 💀 kill!" if result.target_died else "")
            )
        else:
            self.state.log_event(
                f"Atacaste {target_username} — \033[33mfalhado!\033[0m{extras}"
            )
        return result


    async def use_ability(self, idx: int, target: str | None = None) -> bool:
        lp = self.state.local
        abilities = lp.dnd_class.abilities

        if idx < 0 or idx >= len(abilities):
            return False

        ab = abilities[idx]

        if not lp.is_ability_ready(ab.name):
            remaining = lp.cooldown_remaining(ab.name)
            self.state.log_event(f"⏳ {ab.name} em cooldown ({remaining:.0f}s).")
            return False

        if ab.effect is None:
            self.state.log_event(f"[!] {ab.name}: sem efeito definido.")
            return False

        used = await self._dispatch_ability(ab, ab.effect, target)

        if used:
            lp.use_ability(ab.name, ab.cooldown_secs)

        return used

    async def _dispatch_ability(
        self, ab, eff: AbilityEffect, target: str | None
    ) -> bool:

        K = AbilityKind
        k = eff.kind

        if k == K.SELF_HEAL:
            self._ab_self_heal(ab, eff)
            return True

        elif k == K.SELF_BUFF:
            self._ab_self_buff(ab, eff)
            return True

        elif k == K.TARGET_MARK:
            if not self._ab_validate_target(ab, eff, target):
                return False
            self._ab_target_mark(ab, eff, target)
            return True

        elif k == K.TARGET_DAMAGE:
            if not self._ab_validate_target(ab, eff, target):
                return False
            await self._ab_target_damage(ab, eff, target)
            return True

        elif k == K.TARGET_DRAIN:
            if not self._ab_validate_target(ab, eff, target):
                return False
            await self._ab_target_drain(ab, eff, target)
            return True

        elif k == K.MULTI_HIT:
            if not self._ab_validate_target(ab, eff, target):
                return False
            await self._ab_multi_hit(ab, eff, target)
            return True

        elif k == K.AOE_DAMAGE:
            return await self._ab_aoe(ab, eff)

        self.state.log_event(f"[!] {ab.name}: kind desconhecido '{k}'.")
        return False


    def _ab_self_heal(self, ab, eff: AbilityEffect) -> None:
        lp = self.state.local
        heal = (roll(eff.heal_dice) if eff.heal_dice else 0) + eff.heal_fixed
        lp.heal(heal)
        self.state.log_event(
            f"{eff.emoji} {ab.name}: +{heal} HP  ({lp.hp}/{lp.max_hp})"
        )

    def _ab_self_buff(self, ab, eff: AbilityEffect) -> None:
        lp = self.state.local
        lp.set_effect(eff.effect_key, eff.effect_duration, eff.effect_label)
        self.state.log_event(f"{eff.emoji} {ab.name}: {ab.description}")

    def _ab_target_mark(self, ab, eff: AbilityEffect, target: str) -> None:
        lp = self.state.local
        lp.marked_target = target
        lp.set_effect(eff.mark_effect_key, eff.mark_duration, eff.mark_label)
        self.state.log_event(
            f"{eff.emoji} {ab.name}: {target} marcado ({eff.mark_duration:.0f}s)"
        )

    def _ab_validate_target(
        self, ab, eff: AbilityEffect, target: str | None
    ) -> bool:
        if not target:
            self.state.log_event(f"{eff.emoji} Uso: <número> <username>")
            return False
        if not self.find_peer(target):
            self.state.log_event(f"[!] '{target}' não encontrado.")
            return False
        eff_range = ab.ability_range or self.state.local.dnd_class.attack_range
        return self.check_range(target, eff_range)

    async def _ab_target_damage(
        self, ab, eff: AbilityEffect, target: str
    ) -> None:
        lp = self.state.local
        peer = self.find_peer(target)

        dmg = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
        attack_roll = 999 if eff.auto_hit else lp.dnd_class.roll_attack()

        attack_roll, dmg, extra_notes = self.apply_on_hit_modifiers(
            target, attack_roll, dmg, is_ability=True
        )

        req = game_pb2.AttackRequest(
            attacker=lp.username,
            attacker_class=lp.dnd_class.name,
            weapon=eff.weapon_name or ab.name,
            attack_roll=attack_roll,
            base_damage_roll=dmg,
            target=target,
            is_spell=ab.is_spell,
            attacker_x=lp.x,
            attacker_y=lp.y,
            effective_range=ab.ability_range or lp.dnd_class.attack_range,
            ability_name=ab.name,
        )
        result = await self._route_attack(req, peer)
        if result:
            extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
            self.state.log_event(
                f"{eff.emoji} {ab.name} -> {target}: {result.damage_dealt} dano{extras}"
                + (" 💀" if result.target_died else "")
            )
            if result.target_died:
                lp.kills += 1

    async def _ab_target_drain(
        self, ab, eff: AbilityEffect, target: str
    ) -> None:
        lp = self.state.local
        peer = self.find_peer(target)

        dmg = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
        attack_roll = 999 if eff.auto_hit else lp.dnd_class.roll_attack()

        attack_roll, dmg, extra_notes = self.apply_on_hit_modifiers(
            target, attack_roll, dmg, is_ability=True
        )

        req = game_pb2.AttackRequest(
            attacker=lp.username,
            attacker_class=lp.dnd_class.name,
            weapon=eff.weapon_name or ab.name,
            attack_roll=attack_roll,
            base_damage_roll=dmg,
            target=target,
            is_spell=ab.is_spell,
            attacker_x=lp.x,
            attacker_y=lp.y,
            effective_range=ab.ability_range or lp.dnd_class.attack_range,
            ability_name=ab.name,
        )
        result = await self._route_attack(req, peer)
        if result and result.hit:
            heal = max(1, int(result.damage_dealt * eff.drain_ratio))
            lp.heal(heal)
            extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
            self.state.log_event(
                f"{eff.emoji} {ab.name} -> {target}: {result.damage_dealt} dano,"
                f" +{heal} HP{extras}"
                + (" 💀" if result.target_died else "")
            )
            if result.target_died:
                lp.kills += 1
        elif result:
            self.state.log_event(f"{eff.emoji} {ab.name} -> {target}: falhado!")

    async def _ab_multi_hit(
        self, ab, eff: AbilityEffect, target: str
    ) -> None:
        lp = self.state.local
        peer = self.find_peer(target)
        total_dmg = 0
        kills = 0

        for _ in range(eff.hit_count):
            dmg = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
            req = game_pb2.AttackRequest(
                attacker=lp.username,
                attacker_class=lp.dnd_class.name,
                weapon=eff.weapon_name or ab.name,
                attack_roll=999 if eff.auto_hit else lp.dnd_class.roll_attack(),
                base_damage_roll=dmg,
                target=target,
                is_spell=ab.is_spell,
                attacker_x=lp.x,
                attacker_y=lp.y,
                effective_range=ab.ability_range or lp.dnd_class.attack_range,
                ability_name=ab.name,
            )
            result = await self._route_attack(req, peer)
            if result and result.hit:
                total_dmg += result.damage_dealt
                if result.target_died:
                    kills += 1
                    break

        lp.kills += kills
        self.state.log_event(
            f"{eff.emoji} {ab.name} -> {target}: "
            f"{eff.hit_count} ataques, {total_dmg} dano total"
            + (" 💀" * kills if kills else "")
        )

    async def _ab_aoe(self, ab, eff: AbilityEffect) -> bool:
        lp = self.state.local
        nearby = [
            p for p in self.state.others.values()
            if p.alive and chebyshev(lp.x, lp.y, p.x, p.y) <= eff.aoe_radius
        ]
        if not nearby:
            self.state.log_event(
                f"{eff.emoji} {ab.name}: sem alvos no raio {eff.aoe_radius}."
            )
            return False

        for t in nearby:
            peer = self.find_peer(t.username)
            if not peer:
                continue

            dmg = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
            attack_roll = 999 if eff.auto_hit else lp.dnd_class.roll_attack()

            if eff.use_class_damage_modifier:
                dmg += lp.dnd_class.damage_modifier

            attack_roll, dmg, extra_notes = self.apply_on_hit_modifiers(
                t.username, attack_roll, dmg, is_ability=True
            )

            req = game_pb2.AttackRequest(
                attacker=lp.username,
                attacker_class=lp.dnd_class.name,
                weapon=eff.weapon_name or ab.name,
                attack_roll=attack_roll,
                base_damage_roll=dmg,
                target=t.username,
                is_spell=ab.is_spell,
                attacker_x=lp.x,
                attacker_y=lp.y,
                effective_range=eff.aoe_radius,
                ability_name=ab.name,
            )
            result = await self._route_attack(req, peer)
            if result:
                extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
                if result.hit:
                    self.state.log_event(
                        f"{eff.emoji} {ab.name} -> {t.username}: "
                        f"{result.damage_dealt} dano{extras}"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                else:
                    self.state.log_event(
                        f"{eff.emoji} {ab.name} -> {t.username}: falhado!"
                    )

        return True