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

    def _dbg(self, msg: str):
        role = "host" if (self.lobby and self.lobby.is_host) else "client"
        print(f"[DBG][Node:{self.username}][{role}] {msg}")

    @property
    def node_id(self) -> str:
        return self.kad.node.id.hex()

    #---- KADEMLIA -------------------------------------------------

    async def start_kademlia(self):
        self._dbg(f"start_kademlia listen={self.kad_port} bootstrap={self.bootstrap}")
        # install a loop exception handler to silence noisy InvalidStateError
        # coming from third-party RPC/UDP tasks (rpcudp) which otherwise
        # surface as "Task exception was never retrieved" in logs.
        try:
            loop = asyncio.get_running_loop()
            def _kad_exc_handler(loop, context):
                exc = context.get("exception")
                if isinstance(exc, asyncio.InvalidStateError):
                    # noisy double-set_result from rpcudp — warn and ignore
                    self._dbg(f"ignored loop InvalidStateError: {exc}")
                    return
                # delegate other exceptions to default handler
                if hasattr(loop, 'default_exception_handler'):
                    loop.default_exception_handler(context)
                else:
                    # fallback: print context
                    self._dbg(f"unhandled loop exception: {context}")
            loop.set_exception_handler(_kad_exc_handler)
        except Exception:
            # if handler installation fails, continue — this is non-fatal
            pass

        await network_handler.init_kad(self.kad, self.kad_port, bootstrap=self.bootstrap)
        self.state.local.node_id = self.node_id
        self._dbg(f"kademlia ready node_id={self.node_id[:8]}")


    async def register_self(self, force: bool = False, min_interval: float = 10.0):
        now = time.time()
        if not force and (now - self._last_register) < min_interval:
            return
        self._dbg(f"register_self force={force} host={self.host} grpc={self.grpc_port}")
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
        self._dbg(f"register_self done peers={len(self.peers)}")
        

    async def discover_peers(self):
        candidates = await network_handler.find_neighbor_addrs(self.kad, self.host, self.kad_port)
        self._dbg(f"discover_peers candidates={len(candidates)}")
        for nid, addr in candidates:
            if nid in self.peers:
                continue
            self._dbg(f"discover_peers new nid={nid[:8]} addr={addr}")
            await self._handshake(addr, remote_node_id=nid)
            await self._announce_join_to(addr)

    async def _handshake(self, addr: str, remote_node_id: str = ""):
        self._dbg(f"handshake start nid={remote_node_id[:8]} addr={addr}")
        stub = await self.get_or_create_grpc_stub(addr)
        await network_handler.handshake(addr, self.node_id, self.peers, remote_node_id, stub)
        peer = self.peers.get(remote_node_id)
        self._dbg(f"handshake done nid={remote_node_id[:8]} known={bool(peer)}")

    async def _announce_join_to(self, addr: str):
        self._dbg(f"announce_join_to START addr={addr}")
        req = game_pb2.JoinRequest(
            username=self.username,
            class_name=self.state.local.dnd_class.name,
            grpc_address=self.host,
            port=self.grpc_port,
        )
        result = await self.send_rpc(addr, 'Join', req)
        self._dbg(f"announce_join_to END addr={addr} result={result}")

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
                self._dbg(f"send no stub addr={addr}")
                return None
            return await asyncio.wait_for(rpc_fn(stub, request), timeout=timeout)
        except Exception as e:
            self._dbg(f"send failed addr={addr} err={type(e).__name__}: {e}")
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
        # on repeated send failures, refresh peer grpc_addr from DHT and retry once
        for attempt in range(1, 2 + 1):
            try:
                return await asyncio.wait_for(rpc_fn(stub, request), timeout=timeout)
            except Exception as e:
                last_exc = e
                err_name = type(e).__name__
                # log attempt
                self._dbg(f"send failed addr={addr} attempt={attempt}/{2} err={err_name}: {e}")
                # if final attempt, drop cached channel/stub so future attempts recreate
                if attempt == 2:
                    # Try to refresh grpc_addr from DHT if we can match this addr to a known peer
                    try:
                        # find node id for this addr
                        nid = None
                        for k, p in list(self.peers.items()):
                            if p.get("grpc_addr") == addr:
                                nid = k
                                break
                        if nid and hasattr(self, 'kad') and self.kad:
                            try:
                                from dht_keys import addr_key as _addr_key
                                new_addr = await asyncio.wait_for(self.kad.get(_addr_key(nid)), timeout=2.0)
                                if new_addr and new_addr != addr:
                                    self._dbg(f"_send: refreshed addr from DHT nid={nid[:8]} old={addr} new={new_addr}")
                                    # update peers and retry once with new addr
                                    self.peers[nid]["grpc_addr"] = new_addr
                                    # drop old cached channel/stub for old addr
                                    ch = self._channels.pop(addr, None)
                                    if ch:
                                        try:
                                            res = ch.close()
                                            if asyncio.iscoroutine(res):
                                                await res
                                        except Exception:
                                            pass
                                    self._stubs.pop(addr, None)
                                    # try sending once to new addr
                                    stub = await self.get_or_create_grpc_stub(new_addr)
                                    if stub:
                                        try:
                                            return await asyncio.wait_for(rpc_fn(stub, request), timeout=timeout)
                                        except Exception as e2:
                                            self._dbg(f"_send retry to refreshed addr failed err={type(e2).__name__}: {e2}")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # final cleanup: drop cached channel/stub so future attempts recreate
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
        self._dbg(f"new grpc channel addr={addr}")
        return channel

    async def get_or_create_grpc_stub(self, addr: str = "") -> game_pb2_grpc.GameServiceStub:
        if not addr: return None

        stub = self._stubs.get(addr)
        if stub: return stub
            
        ch = await self.get_or_create_grpc_channel(addr)
        if not ch: return None
            
        stub = game_pb2_grpc.GameServiceStub(ch)
        self._stubs[addr] = stub
        self._dbg(f"new grpc stub addr={addr}")
        return stub
        



    #--- BROADCAST ----------------------------------------------

    async def broadcast_state(self):
        lp = self.state.local
        lp.tick_effects()   # limpa efeitos expirados antes de enviar
        pb = local_player_to_proto(lp, self.state.match_id)
        peers_snapshot = list(self.peers.items()) #to avoid race conditions
        host_addr = self.lobby.host_addr if self.lobby and not self.lobby.is_host else ""
        tasks = [
            self.send_rpc(p["grpc_addr"], 'StateUpdate', pb)
            for nid, p in peers_snapshot
            if nid != self.node_id
        ]
        if host_addr and all(p.get("grpc_addr") != host_addr for _, p in peers_snapshot):
            tasks.append(self.send_rpc(host_addr, 'StateUpdate', pb))
        self._dbg(
            f"broadcast_state targets={len(tasks)} peers={len(peers_snapshot)} "
            f"host_fallback={bool(host_addr)} pos=({lp.x},{lp.y})"
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def announce_join(self):
        self._dbg(f"announce_join START")
        peers_snapshot = list(self.peers.items())
        host_addr = self.lobby.host_addr if self.lobby and not self.lobby.is_host else ""
        self._dbg(f"announce_join peer_snapshot={len(peers_snapshot)} host_addr={host_addr or '-'}")
        tasks = [
            self._announce_join_to(p["grpc_addr"])
            for nid, p in peers_snapshot
            if nid != self.node_id
        ]
        self._dbg(f"announce_join tasks_from_peers={len(tasks)}")
        if host_addr and all(p.get("grpc_addr") != host_addr for _, p in peers_snapshot):
            self._dbg(f"announce_join host_fallback={host_addr}")
            tasks.append(self._announce_join_to(host_addr))
        self._dbg(
            f"announce_join END targets={len(tasks)} peers={len(peers_snapshot)} "
            f"host_fallback={bool(host_addr)}"
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    #---- MOVIMENTO -----------------------------------------------

    async def move(self, dx: int, dy: int):
        lp    = self.state.local
        new_x  = max(0, min(MAP_W - 1, lp.x + dx))
        new_y  = max(0, min(MAP_H - 1, lp.y + dy))
        self._dbg(f"move request from=({lp.x},{lp.y}) to=({new_x},{new_y})")
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
        self._dbg(f"input_loop START peers={len(self.peers)} is_host={self.lobby.is_host if self.lobby else '?'}")
        await self.announce_join()

        x, y = self.state.random_spawn()
        self.state.local.x, self.state.local.y = x, y

        if self.state.phase != MatchPhase.ACTIVE:
            self.state.phase      = MatchPhase.ACTIVE
            self.state.start_time = time.time()
            self.state.match_id   = self.state.local.match_id
            await self._broadcast_event(game_pb2.GameEvent.MATCH_STARTED)

        await self.broadcast_state()
        self._dbg("input_loop initial state broadcast done")

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

            except Exception as e:
                self._dbg(f"refresh_loop error {type(e).__name__}: {e}")
            await asyncio.sleep(0.5)
            
            

    #---- ARRANQUE -------------------------------------------

    async def run(self):
        logging.getLogger("kademlia").setLevel(logging.ERROR)
        self._dbg("run start")

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
        self._dbg("run stop")

        print("\n[Jogo] Saíste. Até à próxima!")