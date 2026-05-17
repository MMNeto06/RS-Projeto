"""
game/game_node.py
GameNode — integra DHT (Kademlia), servidor gRPC do jogo e loop de input.
"""

import asyncio
import time
import sys
import os
import json
import logging

import grpc
from kademlia.network import Server as KademliaNode
import kademlia.node as kad_node
from kademlia.utils import digest

import game_pb2
import game_pb2_grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dht_keys import addr_key, username_key, lobby_meta_key

from dnd_classes import DnDClass
from game_state   import GameState, LocalPlayer, MatchPhase, MAP_W, MAP_H
from game_servicer import GameServicer
from renderer      import render_frame
from input_reader  import InputReader
from lobby_manager import LobbyManager
from proto_helpers      import local_player_to_proto
from combat_engine      import CombatEngine
from command_dispatcher import CommandDispatcher

# Segundos de exibição do ecrã de fim antes de reiniciar
_MATCH_END_PAUSE = 8


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
        self._combat = CombatEngine(
            state=self.state,
            peers=self.peers,
            route_attack_fn=self._route_attack_request,
        )

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


    # ── BROADCAST ─────────────────────────────────────────────────

    async def broadcast_state(self):
        lp = self.state.local
        lp.tick_effects()   # limpa efeitos expirados antes de enviar
        pb = local_player_to_proto(lp, self.state.match_id)
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
                    lobby_meta_key(self.lobby.current_lobby)
                )
                if not raw:
                    continue
                meta = json.loads(raw)
                if meta.get("status") == "started":
                    if self.state.phase != MatchPhase.ACTIVE:
                        self.state.phase = MatchPhase.ACTIVE
                        self.state.log_event("🎮 O host iniciou o jogo!")
                elif meta.get("status") == "abandoned":
                    self.lobby.current_lobby = None
                    self.state.log_event("[Lobby] O host saiu — lobby encerrado.")
            except Exception:
                pass

    async def input_loop(self):
        """Loop de input durante a partida."""
        print("\n[Jogo] A entrar na partida…")
        await self.announce_join()

        x, y = self.state.random_spawn()
        self.state.local.x, self.state.local.y = x, y

        if self.state.phase != MatchPhase.ACTIVE:
            self.state.phase      = MatchPhase.ACTIVE
            self.state.start_time = time.time()
            self.state.match_id   = self.state.local.match_id
            await self._broadcast_event(game_pb2.GameEvent.MATCH_STARTED)

        await self.broadcast_state()

        self._reader.start()
        render_task = asyncio.create_task(self._render_loop())
        sync_task   = asyncio.create_task(self._lobby_sync_loop())
        dispatcher  = CommandDispatcher(self)

        try:
            while True:
                try:
                    text = await self._reader.readline()
                except (KeyboardInterrupt, EOFError):
                    break
                if not await dispatcher.dispatch(text):
                    break
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