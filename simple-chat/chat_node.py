"""
chat_node.py
ChatNode — orquestra todos os componentes do nó P2P de chat.
Herda PeerManagerMixin para ter handshake / ping / broadcast / discover.
Delega lobby ao LobbyManager e CLI ao cli.py.
"""
 
import asyncio
import time
 
import grpc
from kademlia.network import Server as KademliaNode
 
import chat_pb2_grpc
from chat_servicer import ChatServicer
from peer_manager import PeerManagerMixin
from lobby_manager import LobbyManager
from dht_keys import addr_key, username_key
from cli import run_cli
 
 
class ChatNode(PeerManagerMixin):
    """
    Nó P2P que:
      1. Anuncia a sua presença na DHT (Kademlia)
      2. Descobre outros peers via DHT
      3. Envia mensagens a todos os peers via gRPC
      4. Recebe mensagens através do seu servidor gRPC
    """
 
    def __init__(self, username: str, host: str, grpc_port: int, kad_port: int, bootstrap=None):
        self.username = username
        self.host = host
        self.grpc_port = grpc_port
        self.kad_port = kad_port
        self.bootstrap = bootstrap   # (host, kad_port) ou None
 
        self.kad = KademliaNode(ksize=20, alpha=3)
        self.grpc_server = None
        self.peers: dict = {} # node_id_hex → {username, grpc_addr, last_seen}
 
        # LobbyManager criado depois de saber o node_id (disponível após start_kademlia)
        self.lobby: LobbyManager | None = None
 
    # ── KADEMLIA ──────────────────────────────────────────────────
 
    async def start_kademlia(self):
        await self.kad.listen(self.kad_port)
 
        if self.bootstrap:
            print(f"[DHT] A ligar ao bootstrap {self.bootstrap}…")
            await self.kad.bootstrap([self.bootstrap])
            await asyncio.sleep(1)
            print("[DHT] Ligado à rede P2P!")
        else:
            print("[DHT] Sou o primeiro node (bootstrap).")
 
        # Agora temos um node_id definitivo — criar o LobbyManager
        self.lobby = LobbyManager(
            kad=self.kad,
            node_id=self.node_id,
            username=self.username,
            peers=self.peers,
        )
 
    async def register_self(self):
        """Regista a nossa presença na DHT."""
        addr = f"{self.host}:{self.grpc_port}"
        await self.kad.set(addr_key(self.node_id), addr)
        await self.kad.set(username_key(self.username), self.node_id)
        self.peers[self.node_id] = {
            "username": self.username,
            "grpc_addr": addr,
            "last_seen": str(time.time()),
        }
        print(f"[DHT] Registado como '{self.node_id[:8]}…' → {addr}")
 
    async def unregister_from_dht(self):
        """Remove a nossa entrada do dict local (DHT não tem delete real)."""
        self.peers.pop(self.node_id, None)
        print("[DHT] Saída da rede registada.")
 
    # ── gRPC SERVER ───────────────────────────────────────────────
 
    async def start_grpc_server(self):
        self.grpc_server = grpc.aio.server()
        chat_pb2_grpc.add_ChatServiceServicer_to_server(
            ChatServicer(self),
            self.grpc_server,
        )
        self.grpc_server.add_insecure_port(f"0.0.0.0:{self.grpc_port}")
        await self.grpc_server.start()
        print(f"[gRPC] Servidor a escutar na porta {self.grpc_port}")
 
    # ── REFRESH LOOP ──────────────────────────────────────────────
 
    async def _refresh_loop(self):
        """Re-anuncia a presença na DHT e descobre peers a cada 10s."""
        while True:
            try:
                await self.register_self()
                await self.discover_peers()
            except Exception:
                pass
            await asyncio.sleep(10)
 
    # ── ARRANQUE / PARAGEM ────────────────────────────────────────
 
    async def run(self):
        await self.start_kademlia()
        await self.start_grpc_server()
        await self.register_self()
        asyncio.create_task(self._refresh_loop())
        await run_cli(self)
 
    async def shutdown(self):
        await self.unregister_from_dht()
        if self.grpc_server:
            await self.grpc_server.stop(grace=2)
