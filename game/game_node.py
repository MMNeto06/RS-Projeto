"""
game/game_node.py
GameNode — integra DHT (Kademlia), servidor gRPC do jogo e loop de input.

Reutiliza a infra-estrutura de descoberta de peers do chat mas
serve o GameService em vez do ChatService.
"""

import asyncio
import time
import random
import sys
import os

import grpc
import aioconsole
from kademlia.network import Server as KademliaNode
import kademlia.node as kad_node
from kademlia.utils import digest

import game_pb2
import game_pb2_grpc

# Adicionar o diretório pai ao path para importar dht_keys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dht_keys import addr_key, username_key

from dnd_classes import DnDClass, get_class
from game_state  import GameState, LocalPlayer, MatchPhase, MAP_W, MAP_H
from game_servicer import GameServicer
from renderer    import render_frame


class GameNode:
    """
    Nó P2P do jogo. Responsabilidades:
      1. DHT Kademlia: descoberta de peers
      2. Servidor gRPC: recebe ataques, state updates, eventos
      3. Loop de jogo: input do utilizador, render do ecrã
    """

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
        self.peers: dict = {}   # node_id → {username, grpc_addr, last_seen, class_name}

        # Estado local do jogo
        local = LocalPlayer(
            username=username,
            node_id="",          # preenchido após start_kademlia
            dnd_class=dnd_class,
            match_id=match_id,
        )
        self.state = GameState(local)

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
                # Guardar peer com username
                if resp.username not in self.peers:
                    self.peers[resp.username] = {
                        "username":   resp.username,
                        "grpc_addr":  addr,
                        "last_seen":  time.time(),
                        "class_name": "",
                    }
        except Exception:
            pass

    # ── gRPC SERVER ───────────────────────────────────────────────

    async def start_grpc_server(self):
        self.grpc_server = grpc.aio.server()
        game_pb2_grpc.add_GameServiceServicer_to_server(
            GameServicer(self.state, self),
            self.grpc_server,
        )
        self.grpc_server.add_insecure_port(f"0.0.0.0:{self.grpc_port}")
        await self.grpc_server.start()
        print(f"[gRPC] Servidor do jogo na porta {self.grpc_port}")

    # ── ENVIO ─────────────────────────────────────────────────────

    async def _send(self, addr: str, rpc_fn, request, timeout: float = 3.0):
        """Helper genérico para chamadas gRPC com timeout."""
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as ch:
                stub = game_pb2_grpc.GameServiceStub(ch)
                return await asyncio.wait_for(rpc_fn(stub, request), timeout=timeout)
        except Exception:
            return None

    async def broadcast_state(self):
        """Envia o nosso PlayerState a todos os peers."""
        lp = self.state.local
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
        """Anuncia a nossa entrada na partida a todos os peers."""
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

    async def send_attack(self, target_username: str) -> game_pb2.AttackResult | None:
        """
        Ataca um alvo pelo username.
        Rola d20+mod vs AC do alvo (calculado localmente para feedback imediato).
        O alvo calcula o dano real e devolve AttackResult.
        """
        peer = self.peers.get(target_username)
        if not peer:
            self.state.log_event(f"[!] Jogador '{target_username}' não encontrado.")
            return None

        lp = self.state.local
        attack_roll  = lp.dnd_class.roll_attack()
        damage_roll  = lp.dnd_class.roll_damage()

        req = game_pb2.AttackRequest(
            attacker=self.username,
            weapon=lp.dnd_class.weapon,
            attack_roll=attack_roll,
            base_damage_roll=damage_roll,
            damage_type=lp.dnd_class.damage_type,
            target=target_username,
            is_spell=False,
        )
        result = await self._send(
            peer["grpc_addr"],
            lambda s, r: s.Attack(r),
            req,
        )
        if result:
            if result.hit:
                self.state.local.kills += (1 if result.target_died else 0)
                self.state.log_event(
                    f"Atacaste {target_username} com {lp.dnd_class.weapon} "
                    f"→ {result.damage_dealt} dano"
                    + (" 💀 kill!" if result.target_died else "")
                )
            else:
                self.state.log_event(f"Atacaste {target_username} — \033[33mfalhado!\033[0m")
        return result

    async def move(self, dx: int, dy: int):
        """Move o jogador local e faz broadcast do novo estado."""
        lp = self.state.local
        lp.x = max(0, min(MAP_W - 1, lp.x + dx))
        lp.y = max(0, min(MAP_H - 1, lp.y + dy))
        await self.broadcast_state()

    # ── INPUT LOOP ────────────────────────────────────────────────

    async def _render_loop(self):
        """Re-renderiza o ecrã a cada segundo."""
        while True:
            render_frame(self.state, clear=True)
            await asyncio.sleep(1)

    async def input_loop(self):
        """
        Loop de input assíncrono (aioconsole).
        
        Comandos:
          a <username>     → atacar
          m w/a/s/d        → mover (wasd)
          1, 2, …          → usar abilidade N
          s                → mostrar scoreboard
          q                → sair
        """
        print("\n[Jogo] A entrar na partida…")
        await self.announce_join()

        # Spawn aleatório
        x, y = self.state.random_spawn()
        self.state.local.x, self.state.local.y = x, y
        await self.broadcast_state()

        # Iniciar render loop em background
        render_task = asyncio.create_task(self._render_loop())

        while True:
            try:
                text = await aioconsole.ainput("")
                text = text.strip().lower()

                if not text:
                    continue

                parts = text.split()
                cmd   = parts[0]

                if cmd == "q":
                    break

                elif cmd == "a" and len(parts) > 1:
                    target = parts[1]
                    await self.send_attack(target)
                    await self.broadcast_state()

                elif cmd == "m" and len(parts) > 1:
                    direction = parts[1]
                    moves = {"w": (0,-1), "s": (0,1), "a": (-1,0), "d": (1,0)}
                    if direction in moves:
                        dx, dy = moves[direction]
                        await self.move(dx, dy)
                    else:
                        self.state.log_event("[!] Direção inválida. Usa w/a/s/d")

                elif cmd.isdigit():
                    idx = int(cmd) - 1
                    abilities = self.state.local.dnd_class.abilities
                    if 0 <= idx < len(abilities):
                        ab = abilities[idx]
                        if self.state.local.is_ability_ready(ab.name):
                            self.state.local.use_ability(ab.name, ab.cooldown_turns)
                            self.state.log_event(f"Usaste \033[1m{ab.name}\033[0m!")
                            # TODO: implementar efeito real da abilidade
                        else:
                            self.state.log_event(f"{ab.name} ainda em cooldown.")
                    else:
                        self.state.log_event("[!] Abilidade inválida.")

                elif cmd == "s":
                    rows = self.state.scoreboard()
                    self.state.log_event("── Scoreboard ──")
                    for uname, k, d in rows:
                        self.state.log_event(f"  {uname}: {k}K / {d}D")

                else:
                    self.state.log_event(
                        "[!] Comandos: a <user> | m w/a/s/d | 1/2 abilidade | q sair"
                    )

            except (KeyboardInterrupt, EOFError):
                break

        render_task.cancel()

    # ── ARRANQUE ──────────────────────────────────────────────────

    async def _refresh_loop(self):
        while True:
            try:
                await self.register_self()
                await self.discover_peers()
                await self.broadcast_state()
                if self.state.check_match_end():
                    await self._handle_match_end()
            except Exception:
                pass
            await asyncio.sleep(10)

    async def _handle_match_end(self):
        """Calcula vencedor e anuncia o fim da partida."""
        sb = self.state.scoreboard()
        winner = sb[0][0] if sb else "ninguém"
        self.state.phase = MatchPhase.ENDED
        self.state.log_event(f"🏆 Jogo terminado! Vencedor: {winner}")

        event = game_pb2.GameEvent(
            type=game_pb2.GameEvent.MATCH_ENDED,
            payload=winner,
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

    async def run(self):
        import logging
        logging.getLogger("kademlia").setLevel(logging.WARNING)

        await self.start_kademlia()
        await self.start_grpc_server()
        await self.register_self()
        asyncio.create_task(self._refresh_loop())
        await self.input_loop()

        # Saída limpa
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