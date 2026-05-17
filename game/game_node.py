"""
game/game_node.py
GameNode — integra DHT (Kademlia), servidor gRPC do jogo e loop de input.
"""

import asyncio
import time
import sys
import os

import grpc
from kademlia.network import Server as KademliaNode
import kademlia.node as kad_node
from kademlia.utils import digest

import game_pb2
import game_pb2_grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dht_keys import addr_key, username_key

from dnd_classes import AbilityEffect, AbilityKind, DnDClass, roll, get_class
from game_state   import GameState, LocalPlayer, MatchPhase, MAP_W, MAP_H
from game_servicer import GameServicer
from renderer      import render_frame
from input_reader  import InputReader
from lobby_manager import LobbyManager

# Segundos de exibição do ecrã de fim antes de reiniciar
_MATCH_END_PAUSE = 8


def _chebyshev(x1: int, y1: int, x2: int, y2: int) -> int:
    """Distância de Chebyshev entre dois pontos (max(|dx|, |dy|))."""
    return max(abs(x1 - x2), abs(y1 - y2))


class GameNode:
    def __init__(self, username: str, dnd_class: DnDClass,
                 host: str, grpc_port: int, kad_port: int,
                 bootstrap=None, match_id: str = ""):
        self.username   = username
        self.host       = host
        self.grpc_port  = grpc_port
        self.kad_port   = kad_port
        self.bootstrap  = bootstrap

        self.kad         = KademliaNode(ksize=20, alpha=3)
        self.grpc_server = None
        self.peers: dict = {}

        local = LocalPlayer(
            username=username,
            node_id="",
            dnd_class=dnd_class,
            match_id=match_id,
        )
        self.state = GameState(local)
        self.lobby = LobbyManager(
            kad=self.kad,
            node_id="",
            username=username,
            peers=self.peers,
            send_fn=self._send,
            host=self.host,
            kad_port=self.kad_port,
        )

        self._reader = InputReader(prompt="> ")

    @property
    def node_id(self) -> str:
        return self.kad.node.id.hex()

    # ── KADEMLIA ──────────────────────────────────────────────────

    async def start_kademlia(self):
        await self.kad.listen(self.kad_port)

        if self.bootstrap:
            print(f"[DHT] A ligar ao bootstrap {self.bootstrap}…")
            await self.kad.bootstrap([self.bootstrap])
            await asyncio.sleep(1)
            print("[DHT] Ligado!")
        else:
            print("[DHT] Primeiro node (bootstrap).")

        self.state.local.node_id = self.node_id

        # em start_kademlia, após as duas linhas de atribuição:
        print(f"[DEBUG] node_id após listen: '{self.node_id}'")
        print(f"[DEBUG] lobby.node_id após atribuição: '{self.lobby.node_id}'")

    async def register_self(self):
        addr = f"{self.host}:{self.grpc_port}"
        await self.kad.set(addr_key(self.node_id), addr)
        await self.kad.set(username_key(self.username), self.node_id)
        self.peers[self.node_id] = {
            "node_id":    self.node_id, 
            "username":   self.username,
            "grpc_addr":  addr,
            "last_seen":  time.time(),
            "class_name": self.state.local.dnd_class.name,
        }

    async def discover_peers(self):
        await self.kad.get("game:discover")
        target = kad_node.Node(digest("game:closest"))
        neighbors = self.kad.protocol.router.find_neighbors(target)
        for n in neighbors:
            if (n.ip, n.port) == (self.host, self.kad_port):
                continue
            nid = n.id.hex()
            if nid in self.peers:
                continue
            addr = await self.kad.get(addr_key(nid))
            if addr:
                await self._handshake(addr, remote_node_id=nid)

    async def _handshake(self, addr: str, remote_node_id: str = ""):
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as ch:
                stub = game_pb2_grpc.GameServiceStub(ch)
                resp = await asyncio.wait_for(
                    stub.Ping(game_pb2.PingRequest(from_user=self.node_id)),
                    timeout=2.0,
                )
                key = remote_node_id if remote_node_id else resp.username
                if key not in self.peers:
                    self.peers[key] = {
                        "node_id":    remote_node_id,
                        "username":   resp.username,
                        "grpc_addr":  addr,
                        "last_seen":  time.time(),
                        "class_name": "",
                    }
        except Exception:
            pass

    # ── gRPC ──────────────────────────────────────────────────────

    async def start_grpc_server(self):
        self.grpc_server = grpc.aio.server()
        game_pb2_grpc.add_GameServiceServicer_to_server(
            GameServicer(self.state, self),
            self.grpc_server,
        )
        self.grpc_server.add_insecure_port(f"0.0.0.0:{self.grpc_port}")
        await self.grpc_server.start()
        print(f"[gRPC] Servidor do jogo na porta {self.grpc_port}")

    async def _send(self, addr: str, rpc_fn, request, timeout: float = 3.0):
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as ch:
                stub = game_pb2_grpc.GameServiceStub(ch)
                return await asyncio.wait_for(rpc_fn(stub, request), timeout=timeout)
        except Exception:
            return None

    async def _route_attack_request(self, req, target_peer: dict | None = None):
        """Route an AttackRequest to the host (HostAttack) or fallback to the
        target peer's Attack RPC. Returns the RPC result or None on error.
        """
        result = None
        if self.lobby.current_lobby and not getattr(self.lobby, "_is_host", False):
            host_addr = getattr(self.lobby, "_host_addr", None)

            if host_addr:
                rpc_call = lambda s, r: s.HostAttack(r)
                result = await self._send(host_addr, rpc_call, req)
            else:
                self.state.log_event("[!] Não foi possível enviar o ataque.")
                return None
        else:
            local_host_addr = f"{self.host}:{self.grpc_port}"
            rpc_call = lambda s, r: s.HostAttack(r)
            result = await self._send(local_host_addr, rpc_call, req)

        if result is None:
            self.state.log_event("[!] Ataque falhou.")
            return None

        return result

    # ── HELPERS ───────────────────────────────────────────────────

    def _find_peer(self, username: str) -> dict | None:
        """Procura peer pelo username (case-insensitive)."""
        target_lower = username.lower()
        return next(
            (v for v in self.peers.values()
             if v.get("username", "").lower() == target_lower),
            None,
        )

    def _check_range(self, target_username: str, effective_range: int) -> bool:
        """
        Verifica se o alvo está dentro de `effective_range` tiles (Chebyshev).
        Devolve True se ok, False (e loga mensagem) se fora de alcance.
        Se não tiver posição do alvo, deixa passar (estado ainda não sincronizado).
        """
        target_state = self.state.others.get(target_username)
        if target_state is None:
            return True
        lp   = self.state.local
        dist = _chebyshev(lp.x, lp.y, target_state.x, target_state.y)
        if dist > effective_range:
            self.state.log_event(
                f"[!] {target_username} está fora de alcance "
                f"(dist={dist}, range={effective_range})."
            )
            return False
        return True

    # ── BROADCAST ─────────────────────────────────────────────────

    async def broadcast_state(self):
        lp = self.state.local
        lp.tick_effects()   # limpa efeitos expirados antes de enviar
        pb = game_pb2.PlayerState(
            username=lp.username,
            hp=lp.hp,
            max_hp=lp.max_hp,
            x=lp.x,
            y=lp.y,
            alive=lp.alive,
            class_name=lp.dnd_class.name,
            status_effects=lp.status_effects,
            armor_class=lp.armor_class,
            kills=lp.kills,
            deaths=lp.deaths,
            match_id=self.state.match_id,
        )
        tasks = [
            self._send(p["grpc_addr"], lambda s, r: s.StateUpdate(r), pb)
            for nid, p in self.peers.items()
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def announce_join(self):
        req = game_pb2.JoinRequest(
            username=self.username,
            class_name=self.state.local.dnd_class.name,
            grpc_address=self.host,
            port=self.grpc_port,
        )
        tasks = [
            self._send(p["grpc_addr"], lambda s, r: s.Join(r), req)
            for nid, p in self.peers.items()
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    # ── ON-HIT MODIFIER ENGINE ────────────────────────────────────

    def _apply_on_hit_modifiers(
        self,
        target_username: str,
        attack_roll: int,
        damage_roll: int,
        is_ability: bool = False,
    ) -> tuple[int, int, list[str]]:
        """
        Percorre os OnHitModifiers da classe e aplica os activos ao ataque.

        Parâmetros
        ----------
        target_username : alvo do ataque (para verificar marked_target_only)
        attack_roll     : valor inicial do roll de ataque
        damage_roll     : valor inicial do roll de dano
        is_ability      : True → salta modificadores com basic_attack_only=True

        Devolve (novo_attack_roll, novo_damage_roll, extra_notes)
        """
        lp = self.state.local
        extra_notes: list[str] = []

        for mod in lp.dnd_class.on_hit_modifiers:
            # Filtro: se este modifier é só para ataques básicos e estamos
            # a executar uma abilidade, ignorar.
            if is_ability and mod.basic_attack_only:
                continue

            if not lp.is_effect_active(mod.effect_key):
                continue

            if mod.marked_target_only and \
                    lp.marked_target.lower() != target_username.lower():
                continue

            # ── Aplicar ───────────────────────────────────────────
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

            # ── Consumir se aplicável ──────────────────────────────
            if mod.consume:
                lp.active_effects.pop(mod.effect_key, None)
                if mod.status_label and mod.status_label in lp.status_effects:
                    lp.status_effects.remove(mod.status_label)

        return attack_roll, damage_roll, extra_notes


    # ── ATAQUE BÁSICO ─────────────────────────────────────────────

    async def send_attack(self, target_username: str):
        """
        Ataque básico com arma da classe.
        Aplica on_hit_modifiers activos de forma genérica.
        """
        peer = self._find_peer(target_username)
        if not peer:
            self.state.log_event(f"[!] Jogador '{target_username}' não encontrado.")
            return None

        lp          = self.state.local
        attack_roll = lp.dnd_class.roll_attack()
        damage_roll = lp.dnd_class.roll_damage()

        # ── On-hit modifiers (Action Surge, Sneak Attack, Hex, etc.) ──
        attack_roll, damage_roll, extra_notes = self._apply_on_hit_modifiers(
            target_username, attack_roll, damage_roll, is_ability=False
        )

        # ── Eagle Eye: duplica o alcance do ataque básico ──────────
        effective_rng = lp.dnd_class.attack_range
        if lp.is_effect_active("eagle_eye"):
            effective_rng *= 2

        if not self._check_range(target_username, effective_rng):
            return None

        req = game_pb2.AttackRequest(
            attacker=self.username,
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
        result = await self._route_attack_request(req, peer)

        if result is None:
            self.state.log_event("[!] Ataque falhou.")
            return None

        if result:
            extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
            if result.hit:
                lp.kills += (1 if result.target_died else 0)
                self.state.log_event(
                    f"Atacaste {target_username} com {lp.dnd_class.weapon} "
                    f"→ {result.damage_dealt} dano{extras}"
                    + (" 💀 kill!" if result.target_died else "")
                )
            else:
                self.state.log_event(
                    f"Atacaste {target_username} — \033[33mfalhado!\033[0m{extras}"
                )
        return result


    # ── ABILITY DISPATCHER ────────────────────────────────────────

    async def _use_ability(self, idx: int, target: str | None = None):
        """
        Executa a abilidade `idx` (0-based) da classe do jogador local.
        Delega para _dispatch_ability que escolhe o handler correcto baseado
        em ab.effect.kind — sem nenhum if por classe ou por nome de abilidade.
        """
        lp        = self.state.local
        abilities = lp.dnd_class.abilities

        if idx < 0 or idx >= len(abilities):
            return

        ab = abilities[idx]

        if not lp.is_ability_ready(ab.name):
            remaining = lp.cooldown_remaining(ab.name)
            self.state.log_event(f"⏳ {ab.name} em cooldown ({remaining:.0f}s).")
            return

        if ab.effect is None:
            self.state.log_event(f"[!] {ab.name}: sem efeito definido.")
            return

        used = await self._dispatch_ability(ab, ab.effect, target)

        if used:
            lp.use_ability(ab.name, ab.cooldown_secs)
            await self.broadcast_state()

    async def _dispatch_ability(self, ab, eff: AbilityEffect, target: str | None,) -> bool:
        """
        Chama o handler correcto com base em eff.kind.
        Devolve True se a abilidade foi executada (cooldown deve ser aplicado),
        False se abortou cedo (ex: sem alvo fornecido).
        """
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


    # ── ABILITY HANDLERS ──────────────────────────────────────────

    def _ab_self_heal(self, ab, eff: AbilityEffect) -> None:
        """Cura o jogador local. Sem roteamento ao host (aplica localmente)."""
        lp   = self.state.local
        heal = (roll(eff.heal_dice) if eff.heal_dice else 0) + eff.heal_fixed
        lp.heal(heal)
        self.state.log_event(
            f"{eff.emoji} {ab.name}: +{heal} HP  ({lp.hp}/{lp.max_hp})"
        )

    def _ab_self_buff(self, ab, eff: AbilityEffect) -> None:
        """Activa um efeito temporário no jogador local. Sem roteamento ao host."""
        lp = self.state.local
        lp.set_effect(eff.effect_key, eff.effect_duration, eff.effect_label)
        self.state.log_event(f"{eff.emoji} {ab.name}: {ab.description}")

    def _ab_target_mark(self, ab, eff: AbilityEffect, target: str) -> None:
        """Marca um alvo (Hunter's Mark, Hex). Aplica localmente."""
        lp = self.state.local
        lp.marked_target = target
        lp.set_effect(eff.mark_effect_key, eff.mark_duration, eff.mark_label)
        self.state.log_event(
            f"{eff.emoji} {ab.name}: {target} marcado ({eff.mark_duration:.0f}s)"
        )

    def _ab_validate_target(
        self,
        ab,
        eff: AbilityEffect,
        target: str | None,
    ) -> bool:
        """Verifica que o alvo existe e está ao alcance. Loga e devolve False se não."""
        if not target:
            self.state.log_event(f"{eff.emoji} Uso: <número> <username>")
            return False
        if not self._find_peer(target):
            self.state.log_event(f"[!] '{target}' não encontrado.")
            return False
        eff_range = ab.ability_range or self.state.local.dnd_class.attack_range
        return self._check_range(target, eff_range)

    async def _ab_target_damage(
        self,
        ab,
        eff: AbilityEffect,
        target: str,
    ) -> None:
        """Dano num único alvo. Aplica on_hit_modifiers não-básicos (ex: Hex)."""
        lp   = self.state.local
        peer = self._find_peer(target)

        dmg          = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
        attack_roll  = 999 if eff.auto_hit else lp.dnd_class.roll_attack()

        # on_hit_modifiers não-básicos (e.g. Hex, Hunter's Mark)
        attack_roll, dmg, extra_notes = self._apply_on_hit_modifiers(
            target, attack_roll, dmg, is_ability=True
        )

        req = game_pb2.AttackRequest(
            attacker=self.username,
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
        result = await self._route_attack_request(req, peer)
        if result:
            extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
            self.state.log_event(
                f"{eff.emoji} {ab.name} → {target}: {result.damage_dealt} dano{extras}"
                + (" 💀" if result.target_died else "")
            )
            if result.target_died:
                lp.kills += 1

    async def _ab_target_drain(
        self,
        ab,
        eff: AbilityEffect,
        target: str,
    ) -> None:
        """Dano + devolve drain_ratio*dano em HP ao atacante."""
        lp   = self.state.local
        peer = self._find_peer(target)

        dmg         = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
        attack_roll = 999 if eff.auto_hit else lp.dnd_class.roll_attack()

        attack_roll, dmg, extra_notes = self._apply_on_hit_modifiers(
            target, attack_roll, dmg, is_ability=True
        )

        req = game_pb2.AttackRequest(
            attacker=self.username,
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
        result = await self._route_attack_request(req, peer)
        if result and result.hit:
            heal = max(1, int(result.damage_dealt * eff.drain_ratio))
            lp.heal(heal)
            extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
            self.state.log_event(
                f"{eff.emoji} {ab.name} → {target}: {result.damage_dealt} dano,"
                f" +{heal} HP{extras}"
                + (" 💀" if result.target_died else "")
            )
            if result.target_died:
                lp.kills += 1
        elif result:
            self.state.log_event(f"{eff.emoji} {ab.name} → {target}: falhado!")

    async def _ab_multi_hit(
        self,
        ab,
        eff: AbilityEffect,
        target: str,
    ) -> None:
        """N ataques separados no mesmo alvo. Para se o alvo morrer a meio."""
        lp         = self.state.local
        peer       = self._find_peer(target)
        total_dmg  = 0
        kills      = 0

        for _ in range(eff.hit_count):
            dmg = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
            req = game_pb2.AttackRequest(
                attacker=self.username,
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
            result = await self._route_attack_request(req, peer)
            if result and result.hit:
                total_dmg += result.damage_dealt
                if result.target_died:
                    kills += 1
                    break   # alvo morreu, parar

        lp.kills += kills
        self.state.log_event(
            f"{eff.emoji} {ab.name} → {target}: "
            f"{eff.hit_count} ataques, {total_dmg} dano total"
            + (" 💀" * kills if kills else "")
        )

    async def _ab_aoe(self, ab, eff: AbilityEffect) -> bool:
        """
        Ataca todos os inimigos vivos no raio eff.aoe_radius.
        Devolve False (sem cooldown) se não houver alvos.
        """
        lp      = self.state.local
        nearby  = [
            p for p in self.state.others.values()
            if p.alive and _chebyshev(lp.x, lp.y, p.x, p.y) <= eff.aoe_radius
        ]
        if not nearby:
            self.state.log_event(
                f"{eff.emoji} {ab.name}: sem alvos no raio {eff.aoe_radius}."
            )
            return False

        for t in nearby:
            peer = self._find_peer(t.username)
            if not peer:
                continue

            dmg         = (roll(eff.damage_dice) if eff.damage_dice else 0) + eff.damage_fixed
            attack_roll = 999 if eff.auto_hit else lp.dnd_class.roll_attack()

            if eff.use_class_damage_modifier:
                dmg += lp.dnd_class.damage_modifier

            # on_hit_modifiers não-básicos (ex: Hunter's Mark no Volley)
            attack_roll, dmg, extra_notes = self._apply_on_hit_modifiers(
                t.username, attack_roll, dmg, is_ability=True
            )

            req = game_pb2.AttackRequest(
                attacker=self.username,
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
            result = await self._route_attack_request(req, peer)
            if result:
                extras = f"  [{', '.join(extra_notes)}]" if extra_notes else ""
                if result.hit:
                    self.state.log_event(
                        f"{eff.emoji} {ab.name} → {t.username}: "
                        f"{result.damage_dealt} dano{extras}"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                else:
                    self.state.log_event(
                        f"{eff.emoji} {ab.name} → {t.username}: falhado!"
                    )

        return True


    # ── MOVIMENTO ─────────────────────────────────────────────────

    async def move(self, dx: int, dy: int):
        lp    = self.state.local
        lp.x  = max(0, min(MAP_W - 1, lp.x + dx))
        lp.y  = max(0, min(MAP_H - 1, lp.y + dy))
        await self.broadcast_state()

    # ── RENDER LOOP ───────────────────────────────────────────────

    async def _render_loop(self):
        while True:
            render_frame(
                self.state,
                clear=True,
                input_line=self._reader.prompt_line,
            )
            if self._reader.input_event:
                try:
                    await asyncio.wait_for(
                        self._reader.input_event.wait(),
                        timeout=1.0,
                    )
                    self._reader.input_event.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(1)

    # ── INPUT LOOP ────────────────────────────────────────────────

    def _print_lobby_help(self):
        print("""
            [Lobby] Comandos disponíveis:
            lobby create <nome> <maxJogadores>  → Criar um novo lobby
            lobby list                          → Listar lobbies abertos
            lobby join <Código>                 → Entrar num lobby
            lobby leave                         → Sair do lobby actual
            lobby start                         → Iniciar o jogo (só o host)
            lobby status                        → Ver quem está pronto
            ready                               → Marcar-se como pronto
            unready                             → Retirar estado de pronto
        """)


    async def _lobby_sync_loop(self):
        """
        Corre em background e monitoriza o estado do lobby na DHT.
        Quando o host muda para 'started', activa a fase ACTIVE.
        """
        while True:
            await asyncio.sleep(3)
            if not self.lobby.current_lobby:
                continue
            try:
                raw = await self.lobby.kad.get(
                    __import__("dht_keys", fromlist=["lobby_meta_key"]).lobby_meta_key(
                        self.lobby.current_lobby
                    )
                )
                if not raw:
                    continue
                import json
                meta = json.loads(raw)
                if meta.get("status") == "started":
                    from game_state import MatchPhase
                    if self.state.phase != MatchPhase.ACTIVE:
                        self.state.phase = MatchPhase.ACTIVE
                        self.state.log_event("🎮 O host iniciou o jogo!")
                elif meta.get("status") == "abandoned":
                    self.lobby.current_lobby = None
                    self.state.log_event("[Lobby] O host saiu — lobby encerrado.")
            except Exception:
                pass

    async def input_loop(self):
        """
        Comandos de lobby:
            lobby create <name> <maxPlayers>
            lobby list
            lobby join <Code>
            lobby leave
            lobby start
            lobby status
            ready
            unready

        Comandos:
          at <user>         → atacar
          w/a/s/d        → mover
          1 [user]         → abilidade 1 (com alvo opcional)
          2 [user]         → abilidade 2 (com alvo opcional)
          3 [user]         → abilidade 3 (com alvo opcional)
          sc                → scoreboard
          q                → sair
        """
        print("\n[Jogo] A entrar na partida…")
        await self.announce_join()

        x, y = self.state.random_spawn()
        self.state.local.x, self.state.local.y = x, y

        # ── Auto-start da partida ──────────────────────────────────
        if self.state.phase != MatchPhase.ACTIVE:
            self.state.phase      = MatchPhase.ACTIVE
            self.state.start_time = time.time()
            self.state.match_id   = self.state.local.match_id
            await self._broadcast_event(game_pb2.GameEvent.MATCH_STARTED)


        await self.broadcast_state()

        self._reader.start()
        render_task = asyncio.create_task(self._render_loop())
        sync_task = asyncio.create_task(self._lobby_sync_loop())

        try:
            moves = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}
            while True:
                try:
                    text = await self._reader.readline()
                except (KeyboardInterrupt, EOFError):
                    break

                text  = text.strip()
                if not text:
                    continue

                parts = text.split()
                cmd   = parts[0].lower()

                if cmd == "q":
                    break

                elif cmd == "at" and len(parts) > 1:
                    await self.send_attack(parts[1])
                    await self.broadcast_state()

                elif cmd in moves:
                    await self.move(*moves[cmd])

                elif cmd.isdigit():
                    idx    = int(cmd) - 1
                    target = parts[1] if len(parts) > 1 else None
                    await self._use_ability(idx, target)

                elif cmd == "sc":
                    self.state.log_event("── Scoreboard ──")
                    for uname, k, d in self.state.scoreboard():
                        self.state.log_event(f"  {uname}: {k}K / {d}D")

                elif cmd == "lobby": 
                    if len(parts) < 2: 
                        self._print_lobby_help()
                        continue

                    sub_cmd = parts[1].lower()

                    if sub_cmd == "create": 
                        if len(parts) < 4:
                            print("Uso: lobby create <nome> <maxJogadores>")
                            continue
                            
                        name = parts[2]
                        
                        try: 
                            max_players = int(parts[3])
                        except ValueError: 
                            print("[!] maxJogadores deve ser um número inteiro.")
                            continue
                        my_addr = f"{self.host}:{self.grpc_port}"
                        await self.lobby.create(name, max_players, my_addr)
                    
                    elif sub_cmd == "list": 
                        await self.lobby.list_open()
                    
                    elif sub_cmd == "join": 
                        if len(parts) < 3:
                            print("Uso: /lobby join <Código>")
                            continue
                        my_addr = f"{self.host}:{self.grpc_port}"
                        await self.lobby.join(parts[2].upper(), my_addr)
                    
                    elif sub_cmd == "leave": 
                        await self.lobby.leave()
                    
                    elif sub_cmd == "start": 
                        ok = await self.lobby.start() 

                        if ok: 
                            self.state.phase = __import__(
                                "game_state", fromlist=["MatchPhase"]
                            ).MatchPhase.ACTIVE
                            self.state.log_event("🎮 Jogo iniciado pelo host!")
                    
                    elif sub_cmd == "status": 
                        await self.lobby.status()

                    else: 
                        self._print_lobby_help()

                elif cmd == "ready": 
                    await self.lobby.set_ready(True)
                
                elif cmd == "unready": 
                    await self.lobby.set_ready(False)
                        
                else:
                    self.state.log_event(
                        "[!] Comandos: at <user> | w/a/s/d | 1/2/3 [user] | s | q"
                    )

        finally:
            render_task.cancel()
            sync_task.cancel()
            self._reader.stop()


    # ── MATCH END / RESET ─────────────────────────────────────────

    async def _broadcast_event(self, event_type, payload: str = ""):
        event = game_pb2.GameEvent(
            type=event_type,
            payload=payload,
            timestamp=int(time.time()),
            match_id=self.state.match_id,
            event_author=self.username,
        )
        tasks = [
            self._send(p["grpc_addr"], lambda s, r: s.BroadcastEvent(r), event)
            for nid, p in self.peers.items()
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_match_end(self):
        sb     = self.state.scoreboard()
        winner = sb[0][0] if sb else "ninguém"

        # Mostrar resultado
        self.state.log_event("━" * 30)
        self.state.log_event(f"🏆 FIM DE JOGO!  Vencedor: {winner}")
        for uname, k, d in sb:
            self.state.log_event(f"   {uname}: {k}K / {d}D")
        self.state.log_event(f"🔄 Nova partida em {_MATCH_END_PAUSE}s…")
        self.state.log_event("━" * 30)

        await self._broadcast_event(game_pb2.GameEvent.MATCH_ENDED, winner)
        await asyncio.sleep(_MATCH_END_PAUSE)
        await self._reset_match()

    async def _reset_match(self):
        """Repõe todos os jogadores e inicia nova partida."""
        x, y = self.state.random_spawn()
        self.state.local.reset_for_new_match(x, y)
        self.state.reset_others_for_new_match()
        self.state.phase      = MatchPhase.ACTIVE
        self.state.start_time = time.time()

        self.state.log_event("🔄 Nova partida iniciada!")
        await self._broadcast_event(game_pb2.GameEvent.MATCH_RESET)
        await self.broadcast_state()

    # ── REFRESH LOOP ──────────────────────────────────────────────

    async def _refresh_loop(self):
        while True:
            try:
                await self.register_self()
                await self.discover_peers()
                await self.broadcast_state()

                # Verificar fim de partida (apenas se ACTIVE para não re-trigger)
                if self.state.phase == MatchPhase.ACTIVE and self.state.check_match_end():
                    self.state.phase = MatchPhase.ENDED   # bloqueia re-trigger
                    asyncio.create_task(self._handle_match_end())

            except Exception:
                pass
            await asyncio.sleep(0.5)

    # ── ARRANQUE ──────────────────────────────────────────────────

    async def run(self):
        import logging
        logging.getLogger("kademlia").setLevel(logging.ERROR)

        await self.start_kademlia()
        await self.start_grpc_server()
        await self.register_self()
        asyncio.create_task(self._refresh_loop())
        await self.input_loop()

        leave_req = game_pb2.LeaveNotification(username=self.username)
        tasks = [
            self._send(p["grpc_addr"], lambda s, r: s.Leave(r), leave_req)
            for nid, p in self.peers.items()
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.grpc_server:
            await self.grpc_server.stop(grace=2)
        print("\n[Jogo] Saíste. Até à próxima!")