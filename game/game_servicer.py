"""
game/game_servicer.py
Implementação dos métodos gRPC definidos no game.proto.

O GameServicer recebe as chamadas dos outros peers (ataques, state updates,
eventos de jogo) e actualiza o GameState local.
"""

import random
import time

import game_pb2
import game_pb2_grpc
from game_state import GameState, MatchPhase


class GameServicer(game_pb2_grpc.GameServiceServicer):
    """
    Cada método corresponde a um RPC do game.proto.
    Recebe pedidos de outros peers e modifica o GameState local.

    Parameters
    ----------
    state   : GameState      — estado local do jogo
    node    : GameNode       — referência ao node (para broadcasts, etc.)
    """

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
        """Um peer anuncia que entrou na partida."""
        self.state.log_event(
            f"\033[32m{request.username}\033[0m ({request.class_name}) entrou no jogo."
        )
        # Guardar o endereço gRPC para podermos contactá-lo
        nid = request.username   # usamos username como ID temporário
        if nid not in self.state.others:
            from game_state import RemotePlayer
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
        self.state.log_event(
            f"\033[31m{request.username}\033[0m saiu do jogo."
        )
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── ATTACK ────────────────────────────────────────────────────

    async def Attack(self, request, context):
        """
        Recebemos um ataque de outro peer.
        Calculamos se acertou (d20 + mod vs nossa AC) e aplicamos o dano.
        """
        local = self.state.local

        # Só processa se o alvo somos nós e estamos vivos
        if request.target != local.username or not local.alive:
            return game_pb2.AttackResult(
                hit=False, damage_dealt=0,
                target_hp_remaining=local.hp, target_died=False,
            )

        # Verificar se o ataque acerta (attack_roll vs AC)
        hit = request.attack_roll >= local.armor_class

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

            # Se morremos, pedir respawn após delay
            if died:
                import asyncio
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
        import asyncio
        await asyncio.sleep(seconds)
        x, y = self.state.random_spawn()
        self.state.local.respawn(x, y)
        self.state.log_event(
            f"\033[32mRenasceste em ({x},{y})!\033[0m"
        )
        await self.node.broadcast_state()

    # ── STATE UPDATE ──────────────────────────────────────────────

    async def StateUpdate(self, request, context):
        """Recebe o estado actualizado de outro jogador."""
        self.state.update_remote(request)
        return game_pb2.Ack(received=True, by=self.state.local.username)

    # ── BROADCAST EVENT ───────────────────────────────────────────

    async def BroadcastEvent(self, request, context):
        """Recebe um evento global da partida (morte, reset, início, fim)."""
        from game_pb2 import GameEvent

        if request.type == GameEvent.PLAYER_DIED:
            self.state.log_event(
                f"💀 \033[31m{request.payload}\033[0m morreu!"
            )

        elif request.type == GameEvent.MATCH_STARTED:
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
            self.state.phase = MatchPhase.LOBBY
            self.state.log_event("🔄 Partida reiniciada.")

        return game_pb2.Ack(received=True, by=self.state.local.username)