#GameNode — integra Kademlia, servidor gRPC do jogo e loop de input.

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
from game.lobby.lobby_manager import LobbyManager
from game.network.host_router import Router
from game.network import network_handler
from proto_helpers      import local_player_to_proto
from combat_engine      import CombatEngine
from command_dispatcher import CommandDispatcher
from lobby.lobby_enums import LobbyStatus

# Segundos de exibição do ecrã de fim antes de reiniciar
_MATCH_END_PAUSE = 8


class GameNode(Router):
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
            send_fn=self.send_rpc,
            host=self.host,
            kad_port=self.kad_port,
        )

        self._reader = InputReader(prompt="> ")
        self._combat = CombatEngine(
            state=self.state,
            peers=self.peers,
            route_attack_fn=self.route_attack,
        )
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._stubs: dict[str, game_pb2_grpc.GameServiceStub] = {}
        self._last_register = 0.0
        self.lobby_started_event = asyncio.Event()

    @property
    def node_id(self) -> str:
        return self.kad.node.id.hex()

    #---- KADEMLIA -------------------------------------------------

    async def start_kademlia(self):
        await network_handler.init_kad(self.kad, self.kad_port, bootstrap=self.bootstrap)
        self.state.local.node_id = self.node_id


    async def register_self(self, force: bool = False, min_interval: float = 10.0):
        now = time.time()
        if not force and (now - self._last_register) < min_interval:
            return
        await network_handler.register_self(
            self.kad,
            self.node_id,
            self.host,
            self.grpc_port,
            self.username,
            self.peers,
            self.state.local.dnd_class.name,
        )
        self._last_register = now
        

    async def discover_peers(self):
        candidates = await network_handler.find_neighbor_addrs(self.kad, self.host, self.kad_port)
        for nid, addr in candidates:
            if nid in self.peers:
                continue
            await self._handshake(addr, remote_node_id=nid)

    async def _handshake(self, addr: str, remote_node_id: str = ""):
        stub = await self.get_or_create_grpc_stub(addr)
        await network_handler.handshake(addr, self.node_id, self.peers, remote_node_id, stub)

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
        try:
            stub = await self.get_or_create_grpc_stub(addr)
            if not stub:
                return None
            return await asyncio.wait_for(rpc_fn(stub, request), timeout=timeout)
        except Exception:
            # on error, drop cached channel/stub so future attempts recreate
            ch = self._channels.pop(addr, None)
            if ch:
                try:
                    res = ch.close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass
            self._stubs.pop(addr, None)
            return None
        
    async def send_rpc(self, addr: str, rpc, request, timeout: float = 3.0):
        #so the lambda isnt repeated everywhere
        if isinstance(rpc, str):
            rpc_name = rpc
            rpc_fn = lambda s, r: getattr(s, rpc_name)(r)
        else:
            rpc_fn = rpc

        return await self._send(addr, rpc_fn, request, timeout=timeout)
    
    
    async def get_or_create_grpc_channel(self, addr: str = "") -> grpc.aio.Channel:
        if not addr: return None
            

        channel = self._channels.get(addr)
        if channel: return channel
            

        channel = grpc.aio.insecure_channel(addr)
        self._channels[addr] = channel
        return channel

    async def get_or_create_grpc_stub(self, addr: str = "") -> game_pb2_grpc.GameServiceStub:
        if not addr: return None

        stub = self._stubs.get(addr)
        if stub: return stub
            
        ch = await self.get_or_create_grpc_channel(addr)
        if not ch: return None
            
        stub = game_pb2_grpc.GameServiceStub(ch)
        self._stubs[addr] = stub
        return stub
        



    #--- BROADCAST ----------------------------------------------

    async def broadcast_state(self):
        lp = self.state.local
        lp.tick_effects()   # limpa efeitos expirados antes de enviar
        pb = local_player_to_proto(lp, self.state.match_id)
        peers_snapshot = list(self.peers.items()) #to avoid race conditions
        tasks = [
            self.send_rpc(p["grpc_addr"], 'StateUpdate', pb)
            for nid, p in peers_snapshot
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
            x=self.state.local.x, 
            y=self.state.local.y
        )
        peers_snapshot = list(self.peers.items())
        tasks = [
            self.send_rpc(p["grpc_addr"], 'Join', req)
            for nid, p in peers_snapshot
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    #---- MOVIMENTO -----------------------------------------------

    async def move(self, dx: int, dy: int):
        lp    = self.state.local
        new_x  = max(0, min(MAP_W - 1, lp.x + dx))
        new_y  = max(0, min(MAP_H - 1, lp.y + dy))
        await self.route_move(game_pb2.MoveRequest(username = self.username,x = new_x,y = new_y))


    #---- RENDER LOOP ----------------------------------------------

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
                        timeout=1/30,
                    )
                    self._reader.input_event.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(1)

    #---- INPUT LOOP --------------------------------------------------

    async def _lobby_sync_loop(self):
        """
        Corre em background e monitoriza o estado do lobby na DHT.
        Quando o host muda para 'started', activa a fase ACTIVE.
        """
        while True:
            await asyncio.sleep(3)
            if not self.lobby.session:
                continue
            lobby = await self.lobby.repo.get_lobby(self.lobby.session.code)
            if lobby: 
                if lobby.status == LobbyStatus.STARTED:
                    if self.state.phase != MatchPhase.ACTIVE:
                        self.state.phase = MatchPhase.ACTIVE
                        self.state.log_event("🎮 O host iniciou o jogo!")
                elif lobby.status == LobbyStatus.ABANDONED:
                    self.lobby.session.reset()
                    self.state.log_event("[Lobby] O host saiu — lobby encerrado.")

    async def input_loop(self):
        """Loop de input durante a partida."""
        print("\n[Jogo] A entrar na partida…")

        x, y = self.state.random_spawn()
        self.state.local.x, self.state.local.y = x, y

        await self.announce_join()

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


    #---- MATCH END / RESET --------------------------------------------

    async def _broadcast_event(self, event_type, payload: str = ""):
        event = game_pb2.GameEvent(
            type=event_type,
            payload=payload,
            timestamp=int(time.time()),
            match_id=self.state.match_id,
            event_author=self.username,
        )
        peers_snapshot = list(self.peers.items())
        tasks = [
            self.send_rpc(p["grpc_addr"], 'BroadcastEvent', event)
            for nid, p in peers_snapshot
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

    #--- REFRESH LOOP --------------------------------------------

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
            
            

    #---- ARRANQUE -------------------------------------------

    async def run(self):
        logging.getLogger("kademlia").setLevel(logging.ERROR)

        await self.start_kademlia()
        await self.start_grpc_server()
        await self.register_self(force=True)
        asyncio.create_task(self._refresh_loop())
        await self.input_loop()

        leave_req = game_pb2.LeaveNotification(username=self.username)
        peers_snapshot = list(self.peers.items())
        tasks = [
            self.send_rpc(p["grpc_addr"], 'Leave', leave_req)
            for nid, p in peers_snapshot
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.grpc_server:
            await self.grpc_server.stop(grace=2)
        # close pooled channels
        for ch in list(self._channels.values()):
            try:
                res = ch.close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        self._channels.clear()
        self._stubs.clear()

        print("\n[Jogo] Saíste. Até à próxima!")