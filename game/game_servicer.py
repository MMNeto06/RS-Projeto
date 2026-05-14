"""
game/game_servicer.py
Implementação dos métodos gRPC definidos no game.proto.
"""

import time
import asyncio

import game_pb2
import game_pb2_grpc
from game_state import GameState, MatchPhase, RemotePlayer

import json


def _chebyshev(x1: int, y1: int, x2: int, y2: int) -> int:
    """Distância de Chebyshev entre dois pontos num grid (max de |dx|, |dy|)."""
    return max(abs(x1 - x2), abs(y1 - y2))


class GameServicer(game_pb2_grpc.GameServiceServicer):

    def __init__(self, state: GameState, node):
        self.state = state
        self.node  = node

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
            dist = _chebyshev(
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

        # build new revived playerstate
        ps = game_pb2.PlayerState(
            username=target.username,
            hp=target.hp,
            max_hp=target.max_hp,
            x=target.x,
            y=target.y,
            alive=target.alive,
            class_name=target.class_name,
            status_effects=target.status_effects,
            armor_class=target.armor_class,
            kills=target.kills,
            deaths=target.deaths,
            match_id=self.state.match_id,
        )

        # broadcast update
        tasks = []
        seen = set()
        for nid, p in self.node.peers.items():
            if nid == self.node.node_id:
                continue
            addr = p.get("grpc_addr")
            if not addr:
                continue
            tasks.append(self.node._send(addr, lambda s, r: s.StateUpdate(r), ps))
            seen.add(addr)

        for _, o in self.state.others.items():
            addr = getattr(o, "grpc_addr", "")
            if addr and addr not in seen:
                tasks.append(self.node._send(addr, lambda s, r: s.StateUpdate(r), ps))
                seen.add(addr)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── STATE UPDATE ──────────────────────────────────────────────

    async def StateUpdate(self, request, context):
        self.state.update_remote(request)
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── BROADCAST EVENT ───────────────────────────────────────────

    async def BroadcastEvent(self, request, context):
        from game_pb2 import GameEvent

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
        if not self.node.lobby._is_host:
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
        if not getattr(self.node, "lobby", None) or not self.node.lobby._is_host:
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
            code = incoming["code"]

            existing = self.node.lobby.known_lobbies.get(code)

            if existing:
                existing_lobby, existing_ts = existing
                if existing_lobby.get("version", 0) >= incoming.get("version", 0):
                    return game_pb2.Empty()

            self.node.lobby.known_lobbies[code] = (incoming, time.time())

            if incoming.get("source") != self.node.lobby.node_id:
                incoming["source"] = self.node.lobby.node_id
                await self.node.lobby._gossip_lobby(incoming)

            return game_pb2.Empty()

        except Exception as e:
            print("[GOSSIP ERROR]", e)
            return game_pb2.Empty()
    
    
    async def resolve_attack(self, request, context):
        state = self.state
        node = self.node

        # lookup users to extract stats
        def user_lookup(username):
            if username == state.local.username:
                return state.local, True
            return state.others.get(username), False

        attacker, attackerIsHost = user_lookup(request.attacker)
        target, targetIsHost = user_lookup(request.target)

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
        
            
        dist = _chebyshev(ax, ay, tx, ty)
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
            ps_target = game_pb2.PlayerState(
                username=state.local.username,
                hp=state.local.hp,
                max_hp=state.local.max_hp,
                x=state.local.x,
                y=state.local.y,
                alive=state.local.alive,
                class_name=state.local.dnd_class.name,
                status_effects=state.local.status_effects,
                armor_class=state.local.armor_class,
                kills=state.local.kills,
                deaths=state.local.deaths,
                match_id=state.match_id,
            )
        else:
            ps_target = game_pb2.PlayerState(
                username=target.username,
                hp=target.hp,
                max_hp=target.max_hp,
                x=target.x,
                y=target.y,
                alive=target.alive,
                class_name=target.class_name,
                status_effects=target.status_effects,
                armor_class=target.armor_class,
                kills=target.kills,
                deaths=target.deaths,
                match_id=state.match_id,
            )

        # Broadcast target state
        tasks = []
        for nid, p in self.node.peers.items(): #broadcast to each peer in lobby
            if nid == self.node.node_id: # skip self
                continue
            addr = p.get("grpc_addr")
            if not addr:
                continue
            tasks.append(self.node._send(addr, lambda s, r: s.StateUpdate(r), ps_target))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return game_pb2.AttackResult(hit=hit, damage_dealt=dmg, target_hp_remaining=remaining, status_effect="dead" if died else "", target_died=died)
    


    async def resolve_ability(self, request, context):
        state = self.state
        node = self.node

        # lookup users to extract player obj
        def user_lookup(username):
            if username == state.local.username:
                return state.local, True
            return state.others.get(username), False

        attacker, attackerIsHost = user_lookup(request.attacker)
        if attacker is None:
            self.state.log_event(f"[Host] Habilidade de desconhecido '{request.attacker}' — rejeitada.")
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=0, target_died=False)

        # Validate ability 
        approved, reason = node.lobby.validate_attack(
            attacker_class=request.attacker_class,
            damage=request.base_damage_roll,
            weapon=(getattr(request, 'ability_name', '') or request.weapon),
            is_spell=request.is_spell,
        )
        if not approved:
            self.state.log_event(
                f"[Host] ⚠ Habilidade rejeitada de {request.attacker} ({request.attacker_class}): {reason}"
            )
            
            # return current hp
            if getattr(request, 'target', '') == state.local.username:
                cur = state.local.hp
            else:
                t = state.others.get(getattr(request, 'target', ''))
                cur = t.hp if t else 0
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=cur, target_died=False)

        # Build affected set: explicit targets > AOE > single target
        affected = set()
        if getattr(request, 'targets', None):
            affected.update(request.targets)
        elif getattr(request, 'aoe_radius', 0) and getattr(request, 'aoe_radius', 0) > 0:
            cx = getattr(request, 'aoe_center_x', request.attacker_x)
            cy = getattr(request, 'aoe_center_y', request.attacker_y)
            r = request.aoe_radius
            # include local
            if _chebyshev(cx, cy, state.local.x, state.local.y) <= r:
                affected.add(state.local.username)
            for p in state.others.values():
                try:
                    if _chebyshev(cx, cy, p.x, p.y) <= r:
                        affected.add(p.username)
                except Exception:
                    continue
        else:
            if getattr(request, 'target', ''):
                affected.add(request.target)

        if not affected:
            return game_pb2.AttackResult(hit=False, damage_dealt=0, target_hp_remaining=0, target_died=False)

        ps_updates = []
        total_damage = 0
        any_hit = False
        last_remaining = 0
        any_died = False

        for uname in list(affected):
            tgt, tgtIsHost = user_lookup(uname)
            if tgt is None:
                self.state.log_event(f"[Host] Habilidade alvo desconhecido '{uname}' — ignorado.")
                continue

            # heal vs damage
            heal_amt = getattr(request, 'heal_amount', 0) or 0
            dmg = 0
            died = False

            if heal_amt > 0:
                tgt.hp = min(tgt.max_hp, getattr(tgt, 'hp', 0) + heal_amt)
                last_remaining = tgt.hp
            else:
                # determine hit (allow auto-hit roll 999)
                target_ac = getattr(tgt, 'armor_class', 0)
                hit = (getattr(request, 'attack_roll', 0) == 999) or (getattr(request, 'attack_roll', 0) >= target_ac)
                if not hit:
                    # miss: continue but include zero-damage update for visual consistency
                    last_remaining = tgt.hp
                    continue
                dmg = max(1, getattr(request, 'base_damage_roll', 0))
                total_damage += dmg
                any_hit = True

                if tgtIsHost:
                    last_remaining = state.local.take_damage(dmg)
                    died = not state.local.alive
                    if died:
                        asyncio.create_task(self._respawn_after(3))
                else:
                    tgt.hp = max(0, tgt.hp - dmg)
                    last_remaining = tgt.hp
                    if tgt.hp == 0 and tgt.alive:
                        tgt.alive = False
                        tgt.deaths += 1
                        died = True
                        try:
                            asyncio.create_task(self._respawn_remote(tgt.username, 3))
                        except Exception:
                            pass

                # assign kill credit
                if died:
                    any_died = True
                    if attackerIsHost:
                        state.local.kills += 1
                    else:
                        attacker.kills += 1

            # apply status effect if present
            if getattr(request, 'status_effect', ''):
                try:
                    se = request.status_effect
                    if hasattr(tgt, 'status_effects'):
                        if se not in tgt.status_effects:
                            tgt.status_effects.append(se)
                except Exception:
                    pass

            # build PlayerState for this target
            ps = game_pb2.PlayerState(
                username=tgt.username,
                hp=tgt.hp,
                max_hp=tgt.max_hp,
                x=tgt.x,
                y=tgt.y,
                alive=getattr(tgt, 'alive', True),
                class_name=getattr(tgt, 'class_name', state.local.dnd_class.name),
                status_effects=getattr(tgt, 'status_effects', []),
                armor_class=getattr(tgt, 'armor_class', 0),
                kills=getattr(tgt, 'kills', 0),
                deaths=getattr(tgt, 'deaths', 0),
                match_id=state.match_id,
            )
            ps_updates.append(ps)

        # Broadcast all updates (one StateUpdate per affected player)
        tasks = []
        seen = set()
        for ps in ps_updates:
            for nid, p in node.peers.items():
                if nid == node.node_id:
                    continue
                addr = p.get('grpc_addr')
                if not addr:
                    continue
                tasks.append(node._send(addr, lambda s, r: s.StateUpdate(r), ps))
                seen.add(addr)

            for _, o in state.others.items():
                addr = getattr(o, 'grpc_addr', '')
                if addr and addr not in seen:
                    tasks.append(node._send(addr, lambda s, r: s.StateUpdate(r), ps))
                    seen.add(addr)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Summary result: if single target return its values, otherwise aggregate
        return game_pb2.AttackResult(
            hit=any_hit,
            damage_dealt=total_damage,
            target_hp_remaining=last_remaining,
            status_effect=('' if not getattr(request, 'status_effect', '') else request.status_effect),
            target_died=any_died,
        )