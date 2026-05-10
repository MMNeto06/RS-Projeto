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

from dnd_classes import DnDClass, roll, get_class
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
        self.lobby.node_id = self.node_id

    async def register_self(self):
        addr = f"{self.host}:{self.grpc_port}"
        await self.kad.set(addr_key(self.node_id), addr)
        await self.kad.set(username_key(self.username), self.node_id)
        self.peers[self.node_id] = {
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
            if n.id.hex() in self.peers:
                continue
            addr = await self.kad.get(addr_key(n.id.hex()))
            if addr:
                await self._handshake(addr)

    async def _handshake(self, addr: str):
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as ch:
                stub = game_pb2_grpc.GameServiceStub(ch)
                resp = await asyncio.wait_for(
                    stub.Ping(game_pb2.PingRequest(from_user=self.node_id)),
                    timeout=2.0,
                )
                if resp.username not in self.peers:
                    self.peers[resp.username] = {
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

    # ── ATAQUE BÁSICO ─────────────────────────────────────────────

    async def send_attack(self, target_username: str):
        """
        Versão com validação: antes de enviar o ataque ao alvo,
        pede confirmação ao host do lobby.
        """
        peer = self._find_peer(target_username)
        if not peer:
            self.state.log_event(f"[!] Jogador '{target_username}' não encontrado.")
            return None

        lp          = self.state.local
        attack_roll = lp.dnd_class.roll_attack()
        damage_roll = lp.dnd_class.roll_damage()
        extra_notes = []

        # ── Efeitos que modificam o ataque ────────────────────────

        if lp.is_effect_active("action_surge"):
            surge_bonus = roll("2d8")
            damage_roll += surge_bonus
            del lp.active_effects["action_surge"]
            if "+2d8 Surge↑" in lp.status_effects:
                lp.status_effects.remove("+2d8 Surge↑")
            extra_notes.append(f"Action Surge +{surge_bonus}")

        if lp.is_effect_active("sneak_attack"):
            bonus = roll("2d6")
            damage_roll += bonus
            del lp.active_effects["sneak_attack"]
            if "Sneak Attack↑" in lp.status_effects:
                lp.status_effects.remove("Sneak Attack↑")
            extra_notes.append(f"Sneak Attack +{bonus}")

        if lp.is_effect_active("cheap_shot"):
            attack_roll = 999
            del lp.active_effects["cheap_shot"]
            if "Cheap Shot↑" in lp.status_effects:
                lp.status_effects.remove("Cheap Shot↑")
            extra_notes.append("Cheap Shot!")

        if lp.is_effect_active("blessed"):
            bonus = roll("1d4")
            attack_roll += bonus
            del lp.active_effects["blessed"]
            if "Blessed" in lp.status_effects:
                lp.status_effects.remove("Blessed")
            extra_notes.append(f"Blessed +{bonus}")

        if lp.is_effect_active("hunters_mark") and \
                lp.marked_target.lower() == target_username.lower():
            bonus = roll("1d6")
            damage_roll += bonus
            extra_notes.append(f"Hunter's Mark +{bonus}")

        if lp.is_effect_active("hex_mark") and \
                lp.marked_target.lower() == target_username.lower():
            bonus = roll("1d6")
            damage_roll += bonus
            extra_notes.append(f"Hex +{bonus}")

        if lp.is_effect_active("twinned_spell"):
            twin = lp.dnd_class.roll_damage()
            damage_roll += twin
            del lp.active_effects["twinned_spell"]
            if "Twinned↑" in lp.status_effects:
                lp.status_effects.remove("Twinned↑")
            extra_notes.append(f"Twinned Spell +{twin}")

        if lp.is_effect_active("divine_smite"):
            smite = roll("4d6")
            damage_roll += smite
            del lp.active_effects["divine_smite"]
            if "Divine Smite↑" in lp.status_effects:
                lp.status_effects.remove("Divine Smite↑")
            extra_notes.append(f"Divine Smite +{smite}")

        # ── Range check ───────────────────────────────────────────
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
        )

        if self.lobby.current_lobby and not self.lobby._is_host:
            approved = await self.lobby.request_validation(req)
            if not approved:
                return None   # host rejeitou; mensagem já foi mostrada

        result = await self._send(
            peer["grpc_addr"],
            lambda s, r: s.Attack(r),
            req,
        )
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


    # ── ABILIDADES ────────────────────────────────────────────────

    async def _use_ability(self, idx: int, target: str | None = None):
        """
        Executa a abilidade `idx` (0-based) da classe do jogador local.
        Algumas abilidades precisam de um `target` (username).
        """
        lp        = self.state.local
        abilities = lp.dnd_class.abilities

        if not (0 <= idx < len(abilities)):
            self.state.log_event("[!] Abilidade inválida.")
            return

        ab         = abilities[idx]
        class_name = lp.dnd_class.name.lower()

        if not lp.is_ability_ready(ab.name):
            remaining = lp.cooldown_remaining(ab.name)
            self.state.log_event(f"⏳ {ab.name} em cooldown ({remaining:.0f}s).")
            return

        used = False

        # ── FIGHTER ───────────────────────────────────────────────
        if class_name == "fighter":

            if ab.name == "Second Wind":
                heal = roll("1d10") + 3
                lp.heal(heal)
                self.state.log_event(
                    f"⚔ Second Wind: +{heal} HP ({lp.hp}/{lp.max_hp})"
                )
                used = True

            elif ab.name == "Action Surge":
                lp.set_effect("action_surge", 30, "+2d8 Surge↑")
                self.state.log_event("⚔ Action Surge: próximo ataque +2d8 dano extra!")
                used = True

            elif ab.name == "Fortify":
                lp.set_effect("fortified", 15, "Fortified")
                self.state.log_event(
                    f"⚔ Fortify: AC+2 durante 15s! (AC={lp.armor_class})"
                )
                used = True

        # ── WIZARD ────────────────────────────────────────────────
        elif class_name == "wizard":

            if ab.name == "Magic Missile":
                if not target:
                    self.state.log_event("🔮 Uso: 1 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                total = sum(roll("1d4") + 1 for _ in range(3))
                req = game_pb2.AttackRequest(
                    attacker=self.username,
                    attacker_class=lp.dnd_class.name,
                    weapon="Magic Missile",
                    attack_roll=999,      # auto-acerto
                    base_damage_roll=total,
                    target=target,
                    is_spell=True,
                    attacker_x=lp.x,
                    attacker_y=lp.y,
                    effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"🔮 Magic Missile → {target}: {result.damage_dealt} dano de força"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

            elif ab.name == "Mage Armor":
                lp.set_effect("mage_armor", 30, "Mage Armor")
                self.state.log_event(
                    f"🔮 Mage Armor activo! AC={lp.armor_class} durante 30s."
                )
                used = True

            elif ab.name == "Fireball":
                if not target:
                    self.state.log_event("🔥 Uso: 3 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = sum(roll("1d6") for _ in range(5))
                req = game_pb2.AttackRequest(
                    attacker=self.username,
                    attacker_class=lp.dnd_class.name,
                    weapon="Fireball",
                    attack_roll=999,      # auto-acerto
                    base_damage_roll=dmg,
                    target=target,
                    is_spell=True,
                    attacker_x=lp.x,
                    attacker_y=lp.y,
                    effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"🔥 Fireball → {target}: {result.damage_dealt} dano de fogo"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

        # ── ROGUE ─────────────────────────────────────────────────
        elif class_name == "rogue":

            if ab.name == "Sneak Attack":
                lp.set_effect("sneak_attack", 15, "Sneak Attack↑")
                self.state.log_event("🗡 Sneak Attack: próximo ataque +2d6!")
                used = True

            elif ab.name == "Disengage":
                lp.set_effect("disengaged", 5, "Disengaged")
                self.state.log_event("🗡 Disengage: imune a ataques por 5s!")
                used = True

            elif ab.name == "Cheap Shot":
                lp.set_effect("cheap_shot", 30, "Cheap Shot↑")
                self.state.log_event("🗡 Cheap Shot: próximo ataque é auto-acerto!")
                used = True

        # ── CLERIC ────────────────────────────────────────────────
        elif class_name == "cleric":

            if ab.name == "Cure Wounds":
                heal = roll("1d8") + 3
                lp.heal(heal)
                self.state.log_event(
                    f"✨ Cure Wounds: +{heal} HP ({lp.hp}/{lp.max_hp})"
                )
                used = True

            elif ab.name == "Sacred Flame":
                if not target:
                    self.state.log_event("✨ Uso: 2 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = roll("1d8")
                req = game_pb2.AttackRequest(
                    attacker=self.username,
                    attacker_class=lp.dnd_class.name,
                    weapon="Sacred Flame",
                    attack_roll=999,      # auto-acerto (saving throw simplificado)
                    base_damage_roll=dmg,
                    target=target,
                    is_spell=True,
                    attacker_x=lp.x,
                    attacker_y=lp.y,
                    effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"✨ Sacred Flame → {target}: {result.damage_dealt} dano radiante"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

            elif ab.name == "Bless":
                lp.set_effect("blessed", 30, "Blessed")
                self.state.log_event("✨ Bless: +1d4 ao próximo attack roll!")
                used = True

        # ── RANGER ────────────────────────────────────────────────
        elif class_name == "ranger":

            if ab.name == "Hunter's Mark":
                if not target:
                    self.state.log_event("🏹 Uso: 1 <username>")
                    return
                lp.marked_target = target
                lp.set_effect("hunters_mark", 30, "Hunter's Mark")
                self.state.log_event(
                    f"🏹 Hunter's Mark em {target}: +1d6 nos próximos 30s!"
                )
                used = True

            elif ab.name == "Volley":
                # Ataca todos os jogadores a distância Chebyshev ≤ 2
                nearby = [
                    p for p in self.state.others.values()
                    if p.alive and _chebyshev(lp.x, lp.y, p.x, p.y) <= 6
                ]
                if not nearby:
                    self.state.log_event("🏹 Volley: sem alvos no raio 6.")
                    return
                for t in nearby:
                    peer = self._find_peer(t.username)
                    if not peer:
                        continue
                    dmg = roll("1d8") + lp.dnd_class.damage_modifier
                    req = game_pb2.AttackRequest(
                        attacker=self.username,
                        attacker_class=lp.dnd_class.name,
                        weapon="Longbow",
                        attack_roll=lp.dnd_class.roll_attack(),
                        base_damage_roll=dmg,
                        target=t.username,
                        is_spell=False,
                        attacker_x=lp.x,
                        attacker_y=lp.y,
                        effective_range=6,
                    )
                    result = await self._send(
                        peer["grpc_addr"], lambda s, r: s.Attack(r), req
                    )
                    if result:
                        if result.hit:
                            self.state.log_event(
                                f"🏹 Volley → {t.username}: {result.damage_dealt} dano"
                                + (" 💀" if result.target_died else "")
                            )
                            if result.target_died:
                                lp.kills += 1
                        else:
                            self.state.log_event(f"🏹 Volley → {t.username}: falhado!")
                used = True

            elif ab.name == "Eagle Eye":
                lp.set_effect("eagle_eye", 20, "Eagle Eye")
                self.state.log_event(
                    f"🏹 Eagle Eye: alcance duplicado por 20s! "
                    f"(range {lp.dnd_class.attack_range}→{lp.dnd_class.attack_range * 2})"
                )
                used = True

        # ── SORCERER ──────────────────────────────────────────────
        elif class_name == "sorcerer":

            if ab.name == "Chaos Bolt":
                if not target:
                    self.state.log_event("🌀 Uso: 1 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = roll("3d6")
                req = game_pb2.AttackRequest(
                    attacker=self.username, attacker_class=lp.dnd_class.name,
                    weapon="Chaos Bolt", attack_roll=999, base_damage_roll=dmg,
                    target=target, is_spell=True,
                    attacker_x=lp.x, attacker_y=lp.y, effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"🌀 Chaos Bolt → {target}: {result.damage_dealt} dano caótico"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

            elif ab.name == "Twinned Spell":
                lp.set_effect("twinned_spell", 30, "Twinned↑")
                self.state.log_event("🌀 Twinned Spell: próximo ataque básico dispara duas vezes!")
                used = True

            elif ab.name == "Wild Magic Surge":
                if not target:
                    self.state.log_event("🌀 Uso: 3 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = sum(roll("1d4") for _ in range(5))
                req = game_pb2.AttackRequest(
                    attacker=self.username, attacker_class=lp.dnd_class.name,
                    weapon="Wild Magic Surge", attack_roll=999, base_damage_roll=dmg,
                    target=target, is_spell=True,
                    attacker_x=lp.x, attacker_y=lp.y, effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"🌀 Wild Magic Surge → {target}: {result.damage_dealt} dano mágico"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

        # ── PALADIN ───────────────────────────────────────────────
        elif class_name == "paladin":

            if ab.name == "Divine Smite":
                lp.set_effect("divine_smite", 30, "Divine Smite↑")
                self.state.log_event("⚡ Divine Smite: próximo ataque +4d6 dano radiante!")
                used = True

            elif ab.name == "Lay on Hands":
                lp.heal(25)
                self.state.log_event(f"⚡ Lay on Hands: +25 HP ({lp.hp}/{lp.max_hp})")
                used = True

            elif ab.name == "Aura of Protection":
                lp.set_effect("aura_of_protection", 15, "Aura +3AC")
                self.state.log_event(
                    f"⚡ Aura of Protection: AC+3 durante 15s! (AC={lp.armor_class})"
                )
                used = True

        # ── MONK ──────────────────────────────────────────────────
        elif class_name == "monk":

            if ab.name == "Flurry of Blows":
                if not target:
                    self.state.log_event("👊 Uso: 1 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                total_dmg = 0
                for _ in range(3):
                    dmg = roll("1d4") + 1
                    req = game_pb2.AttackRequest(
                        attacker=self.username, attacker_class=lp.dnd_class.name,
                        weapon="Unarmed Strike", attack_roll=999, base_damage_roll=dmg,
                        target=target, is_spell=False,
                        attacker_x=lp.x, attacker_y=lp.y, effective_range=ab.ability_range,
                    )
                    result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                    if result:
                        total_dmg += result.damage_dealt
                        if result.target_died:
                            lp.kills += 1
                            break   # alvo morreu, parar
                self.state.log_event(f"👊 Flurry of Blows → {target}: {total_dmg} dano total")
                used = True

            elif ab.name == "Patient Defense":
                lp.set_effect("patient_defense", 10, "Patient Defense")
                self.state.log_event(
                    f"👊 Patient Defense: AC+3 durante 10s! (AC={lp.armor_class})"
                )
                used = True

            elif ab.name == "Ki Blast":
                if not target:
                    self.state.log_event("👊 Uso: 3 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = roll("2d8")
                req = game_pb2.AttackRequest(
                    attacker=self.username, attacker_class=lp.dnd_class.name,
                    weapon="Ki Blast", attack_roll=999, base_damage_roll=dmg,
                    target=target, is_spell=False,
                    attacker_x=lp.x, attacker_y=lp.y, effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"👊 Ki Blast → {target}: {result.damage_dealt} dano de ki"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

        # ── WARLOCK ───────────────────────────────────────────────
        elif class_name == "warlock":

            if ab.name == "Hex":
                if not target:
                    self.state.log_event("🌑 Uso: 1 <username>")
                    return
                lp.marked_target = target
                lp.set_effect("hex_mark", 25, "Hex")
                self.state.log_event(
                    f"🌑 Hex em {target}: +1d6 nec. em cada ataque durante 25s!"
                )
                used = True

            elif ab.name == "Eldritch Blast":
                if not target:
                    self.state.log_event("🌑 Uso: 2 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = roll("2d8")
                req = game_pb2.AttackRequest(
                    attacker=self.username, attacker_class=lp.dnd_class.name,
                    weapon="Eldritch Blast", attack_roll=999, base_damage_roll=dmg,
                    target=target, is_spell=True,
                    attacker_x=lp.x, attacker_y=lp.y, effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result:
                    self.state.log_event(
                        f"🌑 Eldritch Blast → {target}: {result.damage_dealt} dano de força"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                used = True

            elif ab.name == "Life Drain":
                if not target:
                    self.state.log_event("🌑 Uso: 3 <username>")
                    return
                peer = self._find_peer(target)
                if not peer:
                    self.state.log_event(f"[!] '{target}' não encontrado.")
                    return
                if not self._check_range(target, ab.ability_range):
                    return
                dmg = roll("2d6")
                req = game_pb2.AttackRequest(
                    attacker=self.username, attacker_class=lp.dnd_class.name,
                    weapon="Life Drain", attack_roll=999, base_damage_roll=dmg,
                    target=target, is_spell=True,
                    attacker_x=lp.x, attacker_y=lp.y, effective_range=ab.ability_range,
                )
                result = await self._send(peer["grpc_addr"], lambda s, r: s.Attack(r), req)
                if result and result.hit:
                    heal_amount = max(1, result.damage_dealt // 2)
                    lp.heal(heal_amount)
                    self.state.log_event(
                        f"🌑 Life Drain → {target}: {result.damage_dealt} dano, "
                        f"+{heal_amount} HP"
                        + (" 💀" if result.target_died else "")
                    )
                    if result.target_died:
                        lp.kills += 1
                elif result:
                    self.state.log_event(f"🌑 Life Drain → {target}: falhado!")
                used = True

        if used:
            if ab.cooldown_secs > 0:
                lp.use_ability(ab.name, ab.cooldown_secs)
            await self.broadcast_state()

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
                from kademlia.network import Server as _KS
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
          a <user>         → atacar
          m w/a/s/d        → mover
          1 [user]         → abilidade 1 (com alvo opcional)
          2 [user]         → abilidade 2 (com alvo opcional)
          3 [user]         → abilidade 3 (com alvo opcional)
          s                → scoreboard
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

                elif cmd == "a" and len(parts) > 1:
                    await self.send_attack(parts[1])
                    await self.broadcast_state()

                elif cmd == "m" and len(parts) > 1:
                    moves = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}
                    direction = parts[1].lower()
                    if direction in moves:
                        await self.move(*moves[direction])
                    else:
                        self.state.log_event("[!] Direção inválida. Usa w/a/s/d")

                elif cmd.isdigit():
                    idx    = int(cmd) - 1
                    target = parts[1] if len(parts) > 1 else None
                    await self._use_ability(idx, target)

                elif cmd == "s":
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
                        "[!] Comandos: a <user> | m w/a/s/d | 1/2/3 [user] | s | q"
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