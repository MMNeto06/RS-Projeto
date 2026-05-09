"""
peer_manager.py
Mixin com toda a lógica de gestão de peers:
  - handshake (troca de info com um peer novo)
  - ping (verificar se um peer está vivo)
  - broadcast (enviar mensagem a todos)
  - discover (encontrar novos peers via DHT/Kademlia)

Usado como mixin pelo ChatNode:
    class ChatNode(PeerManagerMixin): ...
"""

import asyncio
import time

import grpc
from kademlia.utils import digest
import kademlia.node as kad_node

import chat_pb2
import chat_pb2_grpc
from dht_keys import addr_key, username_key


class PeerManagerMixin:
    """
    Assume que a classe que herda tem:
      self.peers     : dict  { node_id_hex → {username, grpc_addr, last_seen} }
      self.kad       : KademliaNode
      self.username  : str
      self.host      : str
      self.grpc_port : int
    """

    @property
    def node_id(self) -> str:
        return self.kad.node.id.hex()

    # ── HANDSHAKE ──────────────────────────────────────────────────

    async def handshake_peer(self, addr: str):
        """
        Contacta um peer pelo seu endereço gRPC, pede o seu PeerInfo
        e guarda-o localmente.
        Devolve o PeerInfoResponse ou None se falhar.
        """
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                resp = await asyncio.wait_for(
                    stub.GetPeerInfo(
                        chat_pb2.PeerInfoRequest(from_node_id=self.node_id)
                    ),
                    timeout=2.0,
                )
                self.peers[resp.node_id] = {
                    "username":  resp.username,
                    "grpc_addr": resp.grpc_addr,
                    "last_seen": time.time(),
                }
                return resp
        except Exception as e:
            print(f"[!] Handshake falhou com {addr}: {e}")
            return None

    # ── PING ───────────────────────────────────────────────────────

    async def ping_peer(self, node_id: str) -> bool:
        """Verifica se um peer (identificado pelo node_id) está online."""
        peer = self.peers.get(node_id)
        if peer:
            addr = peer["grpc_addr"]
        else:
            addr = await self.kad.get(addr_key(node_id))
            if not addr:
                print(f"[!] '{node_id}' não está na DHT nem guardado localmente.")
                return False
            info = await self.handshake_peer(addr)
            if not info:
                return False

        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                resp = await asyncio.wait_for(
                    stub.Ping(chat_pb2.PingRequest(from_user=self.node_id)),
                    timeout=2.0,
                )
                if resp.alive:
                    self.peers[node_id]["last_seen"] = time.time()
                return resp.alive
        except Exception:
            return False

    # ── BROADCAST ─────────────────────────────────────────────────

    async def broadcast(self, content: str):
        """Envia uma mensagem de chat a todos os peers conhecidos."""
        if not self.peers:
            print("[!] Não há mais ninguém na rede.")
            return

        msg = chat_pb2.ChatMessage(
            sender=self.username,
            content=content,
            timestamp=int(time.time()),
            channel="general",
            sender_node_id=self.node_id,
            sender_addr=f"{self.host}:{self.grpc_port}",
        )

        tasks = [
            self._send_to_peer(peer["username"], peer["grpc_addr"], msg)
            for nid, peer in self.peers.items()
            if nid != self.node_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_to_peer(self, username: str, addr: str, msg):
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                await asyncio.wait_for(stub.SendMessage(msg), timeout=3.0)
        except asyncio.TimeoutError:
            print(f"\r\033[K[!] {username} não respondeu (timeout)")
        except grpc.aio.AioRpcError as e:
            print(f"\r\033[K[!] Erro ao contactar {username}: {e.details()}")

    # ── DESCOBERTA ────────────────────────────────────────────────

    async def discover_peers(self):
        """
        Usa o routing table do Kademlia para encontrar nodes vizinhos
        e faz handshake com os que ainda não conhecemos.
        """
        # Self-lookup para popular a routing table
        await self.kad.get("lobby:discover")
        self_node = kad_node.Node(self.kad.node.id, self.host, self.kad_port)
        await self.kad.get(self_node.id)

        target = kad_node.Node(digest("lobby:closest"))
        neighbors = self.kad.protocol.router.find_neighbors(target)

        for n in neighbors:
            if (n.ip, n.port) == (self.host, self.kad_port):
                continue
            if n.id.hex() in self.peers:
                continue  # já conhecemos este peer
            addr = await self.kad.get(addr_key(n.id.hex()))
            if addr:
                await self.handshake_peer(addr)

    async def get_members(self) -> dict:
        return dict(self.peers)