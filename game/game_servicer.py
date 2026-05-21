"""
game/game_servicer.py
Implementação dos métodos gRPC definidos no game.proto.
"""

import time
import asyncio
import json

import game_pb2
import game_pb2_grpc
from game_pb2 import GameEvent
from game_state import GameState, MatchPhase, RemotePlayer
from utils import chebyshev
from proto_helpers import local_player_to_proto, remote_player_to_proto

from lobby.lobby_models import Lobby
class GameServicer(game_pb2_grpc.GameServiceServicer):

    def __init__(self, state: GameState, node):
        self.state = state
        self.node  = node

    def _lookup_player(self, username: str) -> tuple:
        """
        Devolve (player_obj, is_local) para um username.
        player_obj é LocalPlayer se is_local=True, RemotePlayer caso contrário.
        Devolve (None, False) se o jogador não for encontrado.
        """
        if username == self.state.local.username:
            return self.state.local, True
        player = self.state.others.get(username)
        return player, False

    # ── CHAT ──────────────────────────────────────────────────────

    async def SendMessage(self, request, context):
        ts = time.strftime("%H:%M:%S", time.localtime(request.timestamp))
        print(f"\r\033[K[{ts}] \033[1m{request.sender}\033[0m: {request.content}")
        print("> ", end="", flush=True)
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── PING ──────────────────────────────────────────────────────

    async def Ping(self, request, context):
        return game_pb2.PingResponse(
            username=self.state.local.username, alive=True
        )

    # ── JOIN ──────────────────────────────────────────────────────

    async def Join(self, request, context):
        self.state.log_event(
            f"\033[32m{request.username}\033[0m ({request.class_name}) entrou no jogo."
        )
        nid = request.username
        if nid not in self.state.others:
            self.state.others[nid] = RemotePlayer(
                username=request.username,
                node_id=nid,
                grpc_addr=f"{request.grpc_address}:{request.port}",
                hp=0, max_hp=0, x=0, y=0,
                alive=True,
                class_name=request.class_name,
                armor_class=0, kills=0, deaths=0,
            )
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── LEAVE ─────────────────────────────────────────────────────

    async def Leave(self, request, context):
        self.state.others.pop(request.username, None)
        self.state.log_event(f"\033[31m{request.username}\033[0m saiu do jogo.")
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── ATTACK ────────────────────────────────────────────────────

    async def Attack(self, request, context):
        local = self.state.local

        if request.target != local.username or not local.alive:
            return game_pb2.AttackResult(
                hit=False, damage_dealt=0,
                target_hp_remaining=local.hp, target_died=False,
            )

        # ── Validação de range (segunda linha de defesa) ───────────
        # O check principal acontece no atacante (game_node), mas validamos
        # aqui também com a posição que o atacante declarou.
        if request.effective_range > 0:
            dist = chebyshev(
                request.attacker_x, request.attacker_y,
                local.x, local.y,
            )
            if dist > request.effective_range:
                self.state.log_event(
                    f"{request.attacker} tentou atacar fora de alcance "
                    f"(dist={dist}, range={request.effective_range}) — \033[33mrejeitado.\033[0m"
                )
                return game_pb2.AttackResult(
                    hit=False, damage_dealt=0,
                    target_hp_remaining=local.hp, target_died=False,
                )

        # ── Disengage — o jogador local está imune ─────────────────
        if local.is_effect_active("disengaged"):
            self.state.log_event(
                f"{request.attacker} tentou atacar — \033[32mDesviaste (Disengage)!\033[0m"
            )
            return game_pb2.AttackResult(
                hit=False, damage_dealt=0,
                target_hp_remaining=local.hp, target_died=False,
            )

        # attack_roll=999 significa auto-acerto (Magic Missile, Sacred Flame, Cheap Shot)
        # Ray of Frost do Wizard usa o roll normal (attack_modifier=4, range=4)
        hit = (request.attack_roll == 999) or (request.attack_roll >= local.armor_class)

        damage = 0
        died   = False

        if hit:
            damage = max(1, request.base_damage_roll)
            remaining = local.take_damage(damage)
            died = not local.alive

            msg = (
                f"\033[31m{request.attacker}\033[0m atacou com {request.weapon} "
                f"→ \033[1m{damage} dano\033[0m"
                + (" — \033[31mMORRESTE!\033[0m" if died else f" (HP: {remaining}/{local.max_hp})")
            )
            self.state.log_event(msg)

            if died:
                asyncio.create_task(self._respawn_after(3))
        else:
            self.state.log_event(
                f"{request.attacker} tentou atacar — \033[33mfalhado!\033[0m"
            )

        return game_pb2.AttackResult(
            hit=hit,
            damage_dealt=damage,
            target_hp_remaining=local.hp,
            status_effect="dead" if died else "",
            target_died=died,
        )

    async def _respawn_after(self, seconds: int):
        await asyncio.sleep(seconds)
        x, y = self.state.random_spawn()
        self.state.local.respawn(x, y)
        self.state.log_event(f"\033[32mRenasceste em ({x},{y})!\033[0m")
        await self.node.broadcast_state()

    async def _respawn_remote(self, username: str, seconds: int = 3):
        await asyncio.sleep(seconds) #wait for n seconds to fulfill the respawn cooldown
        
        target = next((p for p in self.state.others.values() #get the player
                       if p.username.lower() == username.lower()), None)
        if not target:
            return
        
        x, y = self.state.random_spawn() #respawn logic
        target.hp = target.max_hp
        target.alive = True
        target.x, target.y = x, y
        target.status_effects.clear()
        self.state.log_event(f"\033[32m{target.username}\033[0m renasceu em ({x},{y})!")

        ps = remote_player_to_proto(target, self.state.match_id)

        # broadcast update
        tasks = []
        seen = set()
        for nid, p in self.node.peers.items():
            if nid == self.node.node_id:
                continue
            addr = p.get("grpc_addr")
            if not addr:
                continue
            tasks.append(self.node.send_rpc(addr, 'StateUpdate', ps))
            seen.add(addr)

        for _, o in self.state.others.items():
            addr = getattr(o, "grpc_addr", "")
            if addr and addr not in seen:
                tasks.append(self.node.send_rpc(addr, 'StateUpdate', ps))
                seen.add(addr)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── STATE UPDATE ──────────────────────────────────────────────

    async def StateUpdate(self, request, context):
        self.state.update_remote(request)
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── BROADCAST EVENT ───────────────────────────────────────────

    async def BroadcastEvent(self, request, context):
        if request.type == GameEvent.PLAYER_DIED:
            self.state.log_event(f"💀 \033[31m{request.payload}\033[0m morreu!")

        elif request.type == GameEvent.MATCH_STARTED:
            if self.state.phase != MatchPhase.ACTIVE:
                self.state.phase      = MatchPhase.ACTIVE
                self.state.match_id   = request.match_id
                self.state.start_time = request.timestamp
                self.state.log_event("\033[32m⚔  A partida começou!\033[0m")

        elif request.type == GameEvent.MATCH_ENDED:
            self.state.phase = MatchPhase.ENDED
            self.state.log_event(
                f"\033[33m🏆 Partida terminada! Vencedor: {request.payload}\033[0m"
            )

        elif request.type == GameEvent.MATCH_RESET:
            x, y = self.state.random_spawn()
            self.state.local.reset_for_new_match(x, y)
            self.state.reset_others_for_new_match()
            self.state.phase      = MatchPhase.ACTIVE
            self.state.start_time = float(request.timestamp)
            self.state.log_event("🔄 Nova partida iniciada!")
            await self.node.broadcast_state()

        return game_pb2.Ack(received=True, by=self.state.local.username)
    
    

    # ── VALIDATE ATTACK ───────────────────────────────────────────

    async def ValidateAttack(self, request, context):
        if not self.node.lobby.is_host:
            return game_pb2.ValidateAttackResponse(approved=True, reason="not host")

        approved, reason = self.node.lobby.validate_attack(
            attacker_class=request.attacker_class,
            damage=request.damage,
            weapon=request.weapon,
            is_spell=request.is_spell,
        )

        if not approved:
            self.state.log_event(
                f"[Host] ⚠ Ataque rejeitado de \033[31m{request.attacker}\033[0m "
                f"({request.attacker_class}): {reason}"
            )

        return game_pb2.ValidateAttackResponse(approved=approved, reason=reason)
    
    async def HostAttack(self, request, context):
        # REceives attack as host to process
        if not getattr(self.node, "lobby", None) or not self.node.lobby.is_host:
            self.state.log_event("[HostAttack] Received HostAttack on non-host — rejected.")
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=0, target_died=False)
        return await self.resolve_attack(request, context)
    
    # ── LOBBY NOTIFY ──────────────────────────────────────────────

    async def LobbyNotify(self, request, context):
        Type = game_pb2.LobbyNotification.Type

        if request.type == Type.PLAYER_JOINED:
            self.state.log_event(
                f"\033[32m{request.username}\033[0m entrou no lobby {request.lobby_code}."
            )
        elif request.type == Type.PLAYER_LEFT:
            self.state.log_event(
                f"\033[31m{request.username}\033[0m saiu do lobby {request.lobby_code}."
            )
        elif request.type == Type.PLAYER_READY:
            self.state.log_event(
                f"\033[32m{request.username}\033[0m está \033[1mPRONTO\033[0m."
            )
        elif request.type == Type.PLAYER_UNREADY:
            self.state.log_event(
                f"\033[33m{request.username}\033[0m retirou o estado de pronto."
            )
        elif request.type == Type.LOBBY_STARTED:
            self.state.log_event(
                "\033[1m\033[32m⚔  O host iniciou o jogo!\033[0m"
            )
        elif request.type == Type.LOBBY_CLOSED:
            self.state.log_event(
                f"\033[31mO lobby {request.lobby_code} foi encerrado pelo host.\033[0m"
            )
 
        return game_pb2.LobbyNotificationAck(ok=True)
    
    async def LobbyGossip(self, request, context):
        try:
            incoming = json.loads(request.payload)
            code = incoming.get("code")
            if not code: 
                return game_pb2.Empty()

            lobby = Lobby.from_dict(incoming)
            
            existing = self.node.lobby.known_lobbies.get(code)
            if existing:
                existing_lobby, _ = existing
                if existing_lobby.version >= lobby.version:
                    return game_pb2.Empty()

            self.node.lobby.known_lobbies[code] = (lobby, time.time())

            if lobby.source != self.node.lobby.node_id:
                await self.node.lobby._gossip_lobby(lobby)

            return game_pb2.Empty()

        except Exception as e:
            print("[GOSSIP ERROR]", e)
            return game_pb2.Empty()
    
    
    async def resolve_attack(self, request, context):
        state = self.state
        node = self.node

        attacker, attackerIsHost = self._lookup_player(request.attacker)
        target, targetIsHost = self._lookup_player(request.target)

        if attacker is None:
            self.state.log_event(f"[Host] Ataque de desconhecido '{request.attacker}' — rejeitado.")
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=0, target_died=False)
        if target is None:
            self.state.log_event(f"[Host] Ataque a desconhecido '{request.target}' — rejeitado.")
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=0, target_died=False)

        # Validate the attack
        approved, reason = node.lobby.validate_attack(
            attacker_class=request.attacker_class,
            damage=request.base_damage_roll,
            weapon=request.weapon,
            is_spell=request.is_spell,
        )
        if not approved:
            self.state.log_event(
                f"[Host] ⚠ Ataque rejeitado de {request.attacker} ({request.attacker_class}): {reason}"
            )
            if(targetIsHost):
                current_hp = state.local.hp
            else:
                current_hp= target.hp
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=current_hp, target_died=False)


        #check if attack is in range
        if attackerIsHost:
            ax, ay = state.local.x, state.local.y
        else:
            ax, ay = getattr(attacker, 'x', request.attacker_x), getattr(attacker, 'y', request.attacker_y)
        if targetIsHost:
            tx, ty = state.local.x, state.local.y
        else:
            tx, ty = target.x, target.y
        
            
        dist = chebyshev(ax, ay, tx, ty)
        if dist > request.effective_range:
            self.state.log_event(
                f"{request.attacker} tentou atacar fora de alcance (dist={dist}, range={request.effective_range}) — rejeitado."
            )
            current_hp = state.local.hp if targetIsHost else target.hp
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=current_hp, target_died=False)


        # Calc hit
        if(targetIsHost):
            target_ac = state.local.armor_class
        else:
            target_ac = getattr(target, 'armor_class', 0)
            
        hit = request.attack_roll >= target_ac
        if(hit):
            dmg = max(1, request.base_damage_roll)
        else:
            dmg = 0
            
        #apply the damage
        died = False
        if hit:
            if targetIsHost:
                remaining = state.local.take_damage(dmg)
                died = not state.local.alive
                if died: asyncio.create_task(self._respawn_after(3))
            else:
                target.hp = max(0, target.hp - dmg)
                if target.hp == 0:
                    target.alive = False
                    target.deaths += 1
                    died = True
                remaining = target.hp
                
            if attackerIsHost:
                state.local.kills += (1 if died else 0)
            else:
                attacker.kills += (1 if died else 0)
                
            # If player who isnt the host died, respawn them
            if died and not targetIsHost:
                try:
                    asyncio.create_task(self._respawn_remote(target.username, 3))
                except Exception:
                    pass
        else:
            remaining = state.local.hp if targetIsHost else target.hp

        # create the target playerstate for broadcast
        if targetIsHost:
            ps_target = local_player_to_proto(state.local, state.match_id)
        else:
            ps_target = remote_player_to_proto(target, state.match_id)

        # Broadcast target state
        self.state.log_event("attack resolved by host and will be broadcast")
        tasks = []
        for nid, p in self.node.peers.items(): #broadcast to each peer in lobby
            if nid == self.node.node_id: # skip self
                continue
            addr = p.get("grpc_addr")
            if not addr:
                continue
            tasks.append(self.node.send_rpc(addr, 'StateUpdate', ps_target))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return game_pb2.AttackResult(hit=hit, damage_dealt=dmg, target_hp_remaining=remaining, status_effect="dead" if died else "", target_died=died)