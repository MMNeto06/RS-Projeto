import asyncio
import argparse
import json
import time
import logging

import aioconsole          # input assíncrono (não bloqueia o event loop)
import grpc                # framework RPC
from kademlia.network import Server as KademliaNode   # DHT P2P

import chat_pb2            # gerado pelo protoc a partir do chat.proto
import chat_pb2_grpc       # gerado pelo protoc a partir do chat.proto

import kademlia.node as kad_node
from kademlia.utils import digest

# Reduzir o ruído de logs do kademlia
logging.getLogger("kademlia").setLevel(logging.WARNING)

class ChatServicer(chat_pb2_grpc.ChatServiceServicer):
    """Implementação dos métodos definidos no chat.proto"""

    def __init__(self, node):
        self.node = node

    async def SendMessage(self, request, context):
        """
        Chamado quando recebemos uma mensagem de outro peer.
        'request' é um objeto ChatMessage (definido no .proto).
        """
        ts = time.strftime("%H:%M:%S", time.localtime(request.timestamp))
        # Limpar a linha atual do terminal e imprimir a mensagem
        print(f"\r\033[K[{ts}] \033[1m{request.sender}\033[0m: {request.content}")
        # Reimprimir o prompt
        print("> ", end="", flush=True)
        
        return chat_pb2.Ack(received=True, by=self.node.username)

    async def Ping(self, request, context):
        """Responde a pings para confirmar que o node está vivo."""
        return chat_pb2.PingResponse(username=self.node.username, alive=True)
    
    async def GetPeerInfo(self, request, context):
        return chat_pb2.PeerInfoResponse(
            node_id=self.node.kad.node.id.hex(),
            username=self.node.username,
            grpc_addr=f"{self.node.host}:{self.node.grpc_port}",
            alive=True,
        )



def addr_key(key): return f"peer:{key}:addr"
def username_key(username): return f"username:{username}"


# ================================================================
# MAIN CHAT NODE
# ================================================================

class ChatNode:
    
    #Im changing the central channel key to distributed peer records.
    
    """
    Um node P2P que:
    1. Anuncia a sua presença na DHT (Kademlia)
    2. Descobre outros peers também via DHT
    3. Envia mensagens a todos os peers via gRPC
    4. Recebe mensagens através do seu servidor gRPC
    """

    def __init__(self, username, host, grpc_port, kad_port, bootstrap=None):
        self.username   = username
        self.host       = host
        self.grpc_port  = grpc_port
        self.kad_port   = kad_port
        self.bootstrap  = bootstrap  # (host, kad_port) do node bootstrap, ou None
        self.kad        = KademliaNode(ksize=20, alpha=3) # IMPORTANTE!!! this is a quick fix, fiz real está nos outros importantes
        self.grpc_server = None
        self.peers = {}

    # ── KADEMLIA ──────────────────────────────────────────────────

    async def start_kademlia(self):
        """
        Inicia o node Kademlia.

        O Kademlia é uma DHT (Distributed Hash Table): uma tabela
        chave→valor distribuída por todos os peers.
        Usamos para guardar: username → "host:grpc_port"

        É o equivalente ao "servidor de nomes" no teu jogo DnD:
        quando queres atacar o João, primeiro perguntas à DHT
        onde é que o node do João está.
        """
        await self.kad.listen(self.kad_port)

        if self.bootstrap:
            print(f"[DHT] A ligar ao bootstrap {self.bootstrap}...")
            await self.kad.bootstrap([self.bootstrap])
            await asyncio.sleep(1)  # ← deixar a DHT estabilizar
            print("[DHT] Ligado à rede P2P!")
        else:
            print("[DHT] Sou o primeiro node (bootstrap).")

    async def register_self(self): #Again, now the register will store a record per peer, not a whole json
        addr = f"{self.host}:{self.grpc_port}"
        node_id_hex = self.kad.node.id.hex()
        await self.kad.set(addr_key(node_id_hex), addr)
        await self.kad.set(username_key(self.username), node_id_hex)
        self.peers[node_id_hex] = {"username": self.username, "grpc_addr": addr, "last_seen": str(time.time())}
        print(f"[DHT] Registado como '{node_id_hex}' → {self.host}:{self.grpc_port}")

    async def get_members(self) -> dict:
        return dict(self.peers)
    
    async def unregister_from_dht(self): #Leaving the network is app only, will be dealt with by gRCP
        self.peers.pop(self.kad.node.id.hex(), None)
        print("[DHT] Left the network.")
        

    # ── gRPC SERVER ───────────────────────────────────────────────

    async def start_grpc_server(self):
        """
        Inicia o servidor gRPC que recebe mensagens de outros peers.

        Cada node tem o seu próprio servidor gRPC a correr numa porta.
        Quando o Bob quiser enviar uma mensagem à Alice, ele abre uma
        ligação gRPC para a porta da Alice e chama SendMessage().
        """
        self.grpc_server = grpc.aio.server()
        chat_pb2_grpc.add_ChatServiceServicer_to_server(
            ChatServicer(self),
            self.grpc_server
        )
        self.grpc_server.add_insecure_port(f"0.0.0.0:{self.grpc_port}")
        await self.grpc_server.start()
        print(f"[gRPC] Servidor a escutar na porta {self.grpc_port}")

    # ── ENVIO DE MENSAGENS ────────────────────────────────────────

    async def broadcast(self, content: str):
        peers = self.peers

        if not peers:
            print("[!] Não há mais ninguém no canal.")
            return

        # Criar a mensagem (objeto definido no .proto)
        msg = chat_pb2.ChatMessage(
            sender    = self.username,
            content   = content,
            timestamp = int(time.time()),
            channel   = "general"
        )

        # Enviar a cada peer em paralelo
        tasks = []
        for node_id, peer in self.peers.items():
            if node_id == self.kad.node.id.hex():
                continue  # não nos enviamos a nós próprios
            tasks.append(self._send_to_peer(peer["username"], peer["grpc_addr"], msg))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_to_peer(self, username: str, addr: str, msg):
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                await asyncio.wait_for(
                    stub.SendMessage(msg),
                    timeout=3.0   # desistir após 3 segundos
                )
        except asyncio.TimeoutError:
            print(f"\r\033[K[!] {username} não respondeu (timeout)")
        except grpc.aio.AioRpcError as e:
            print(f"\r\033[K[!] Erro ao contactar {username}: {e.details()}")

    async def ping_peer(self, node_id: str) -> bool:
        """Verifica se um peer está online."""
        peers = self.peers
        if node_id in peers:
            addr = peers[node_id]["grpc_addr"]
        else: #Does a dht lookup if username was not found
            addr = await self.kad.get(addr_key(node_id))
            if not addr:
                print(f"[!] '{node_id}' não está registado na DHT nem guardado localmente.")
                return False
            info = await self.handshake_peer(addr)
            if not info:
                return False
            self.peers[node_id]["last_seen"] = time.time()
        
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                resp = await asyncio.wait_for(
                    stub.Ping(chat_pb2.PingRequest(from_user=self.kad.node.id.hex())),
                    timeout=2.0
                )
                return resp.alive
        except Exception:
            return False

    # ── COMANDOS DO TERMINAL ──────────────────────────────────────

    async def handle_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False

        parts = text.split()
        cmd   = parts[0].lower()

        if cmd == "/members":
            members = await self.get_members()
            print("\n[Canal #general]")
            for id, peer in members.items():
                marker = " ← (tu)" if id == self.kad.node.id.hex() else ""
                print(f"  • {peer["username"]} ({peer["grpc_addr"]}){marker}")
            print()

        elif cmd == "/ping" and len(parts) > 1:
            target = parts[1]
            print(f"[~] A fazer ping a {target}...")
            addr = await self.kad.get(username_key(target))
            online = await self.ping_peer(addr)
            status = "\033[32mONLINE\033[0m" if online else "\033[31mOFFLINE\033[0m"
            print(f"[~] {target} está {status}")

        elif cmd == "/help":
            print("""
Comandos disponíveis:
  /members        → lista todos os membros do canal
  /ping <user>    → verifica se um utilizador está online
  /help           → mostra esta ajuda
  /quit ou /exit  → sair
""")
        elif cmd in ("/quit", "/exit"):
            return None  # sinal para sair

        else:
            print(f"[!] Comando desconhecido: {cmd}. Usa /help para ver os comandos.")

        return True

    # ── LOOP PRINCIPAL ────────────────────────────────────────────

    async def input_loop(self):
        print(f"\n\033[1mBem-vindo ao canal #general, {self.username}!\033[0m")
        print("Escreve uma mensagem e prime Enter para enviar.")
        print("Usa /help para ver comandos.\n")

        while True:
            try:
                text = await aioconsole.ainput("> ")
                text = text.strip()

                if not text:
                    continue

                result = await self.handle_command(text)

                if result is None:
                    # /quit ou /exit
                    break
                elif result is False:
                    # É uma mensagem normal, fazer broadcast
                    await self.broadcast(text)

            except (KeyboardInterrupt, EOFError):
                break

        # Saída limpa
        print("\n[*] A sair do canal...")
        await self.unregister_from_dht()
        if self.grpc_server:
            await self.grpc_server.stop(grace=2)
            
            
    async def discover_peers(self):
        await self.kad.get("lobby:discover")

        node_id = self.kad.node.id
        self_id = kad_node.Node(node_id, self.host, self.kad_port)
        await self.kad.get(self_id.id)  # self‑lookup key
        
        target = kad_node.Node(digest("lobby:closest"))
        neighbors = self.kad.protocol.router.find_neighbors(target)

        for n in neighbors:
            if (n.ip, n.port) == (self.host, self.kad_port):
                continue

            addr = await self.kad.get(addr_key(n.id.hex()))
            if addr:
                await self.handshake_peer(addr)
                
    async def handshake_peer(self, addr: str):
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                resp = await asyncio.wait_for(
                    stub.GetPeerInfo(
                        chat_pb2.PeerInfoRequest(from_node_id=self.kad.node.id.hex())
                    ),
                    timeout=2.0
                )

                self.peers[resp.node_id] = {
                    "username": resp.username,
                    "grpc_addr": resp.grpc_addr,
                    "last_seen": time.time(),
                }
                return resp
        except Exception as e:
            print(f"handshake failed with {addr}: {e}")
            return None

    # ── ARRANQUE ──────────────────────────────────────────────────


    async def run(self):
        """Arranca todos os componentes e inicia o loop."""
        await self.start_kademlia()
        await self.start_grpc_server()
        asyncio.create_task(self._refresh_loop())
        await self.input_loop()

    async def _refresh_loop(self):
        """Re-anuncia a nossa presença na DHT a cada 30s."""
        while True:
            try:
                await self.register_self()
                await self.discover_peers()
                await asyncio.sleep(10)
            except Exception:
                pass


# ================================================================
# ENTRY POINT
# ================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="P2P Chat - canal estilo Discord usando gRPC + Kademlia"
    )
    p.add_argument("--username",           required=True,           help="O teu username")
    p.add_argument("--host",               default="127.0.0.1",     help="O teu IP (default: 127.0.0.1)")
    p.add_argument("--grpc-port",          type=int, required=True, help="Porta do teu servidor gRPC")
    p.add_argument("--kad-port",           type=int, required=True, help="Porta do teu node Kademlia")
    p.add_argument("--bootstrap-host",     default=None,            help="IP do node bootstrap")
    p.add_argument("--bootstrap-kad-port", type=int, default=None,  help="Porta Kademlia do node bootstrap")
    return p.parse_args()


async def main():
    args = parse_args()

    bootstrap = None
    if args.bootstrap_host and args.bootstrap_kad_port:
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)

    node = ChatNode(
        username   = args.username,
        host       = args.host,
        grpc_port  = args.grpc_port,
        kad_port   = args.kad_port,
        bootstrap  = bootstrap,
    )

    await node.run()


if __name__ == "__main__":
    asyncio.run(main())
