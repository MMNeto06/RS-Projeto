"""
game/game_servicer.py
Implementação dos métodos gRPC definidos no game.proto.
"""

import time
import asyncio

import game_pb2
import game_pb2_grpc
from game_state import GameState, MatchPhase, RemotePlayer


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