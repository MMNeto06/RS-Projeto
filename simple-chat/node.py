"""
╔══════════════════════════════════════════════════════════════╗
║           P2P Chat - Canal estilo Discord                    ║
║                                                              ║
║  Tecnologias usadas:                                         ║
║   • Kademlia DHT  → descoberta de peers                      ║
║   • gRPC          → envio de mensagens entre nodes           ║
║   • Protocol Buffers → serialização das mensagens            ║
║   • aioconsole    → input assíncrono no terminal             ║
║   • asyncio       → tudo a correr em simultâneo              ║
╚══════════════════════════════════════════════════════════════╝

Como usar:
  # Node 1 (bootstrap - o primeiro a entrar):
  python node.py --username Alice --grpc-port 50051 --kad-port 8468

  # Node 2 (entra via bootstrap):
  python node.py --username Bob --grpc-port 50052 --kad-port 8469 --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468

  # Node 3:
  python node.py --username Carlos --grpc-port 50053 --kad-port 8470 --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468
"""

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

# Reduzir o ruído de logs do kademlia
logging.getLogger("kademlia").setLevel(logging.WARNING)

# Chave usada na DHT para guardar o registo de membros do canal
CHANNEL_KEY = "general_members"


# ================================================================
# gRPC SERVICE IMPLEMENTATION
# ================================================================
# Esta classe implementa o "servidor" de cada node.
# Quando outro node nos enviar uma mensagem via gRPC,
# é este código que é executado.
# ================================================================

class ChatServicer(chat_pb2_grpc.ChatServiceServicer):
    """Implementação dos métodos definidos no chat.proto"""

    def __init__(self, username: str):
        self.username = username

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

        return chat_pb2.Ack(received=True, by=self.username)

    async def Ping(self, request, context):
        """Responde a pings para confirmar que o node está vivo."""
        return chat_pb2.PingResponse(username=self.username, alive=True)


# ================================================================
# MAIN CHAT NODE
# ================================================================

class ChatNode:
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

    async def register_in_dht(self):
        """
        Guarda a nossa informação na DHT para que outros nos encontrem.

        Usamos uma chave especial (CHANNEL_KEY) que guarda um dicionário
        JSON com todos os membros do canal:
          { "Alice": "127.0.0.1:50051", "Bob": "127.0.0.1:50052", ... }
        """
        
        for attempt in range(3):
            # Ler a lista atual (pode já ter outros membros)
            existing = await self.kad.get(CHANNEL_KEY)
            members = json.loads(existing) if existing else {}

            # Adicionar-nos
            members[self.username] = f"{self.host}:{self.grpc_port}"

            # Guardar de volta na DHT
            await self.kad.set(CHANNEL_KEY, json.dumps(members))

            #verificar se guardou bem
            verify = await self.kad.get(CHANNEL_KEY)
            if verify and self.username in json.loads(verify):
                break
            await asyncio.sleep(0.5)

        print(f"[DHT] Registado como '{self.username}' → {self.host}:{self.grpc_port}")

    async def get_members(self) -> dict:
        """
        Lê o dicionário de membros da DHT.
        Retorna {username: "host:port"} para todos os peers conhecidos.
        """
        data = await self.kad.get(CHANNEL_KEY)
        return json.loads(data) if data else {}

    async def unregister_from_dht(self):
        """Remove-nos da DHT quando saímos."""
        members = await self.get_members()
        if self.username in members:
            del members[self.username]
            await self.kad.set(CHANNEL_KEY, json.dumps(members))
            print("[DHT] Removido do canal.")

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
            ChatServicer(self.username),
            self.grpc_server
        )
        self.grpc_server.add_insecure_port(f"0.0.0.0:{self.grpc_port}")
        await self.grpc_server.start()
        print(f"[gRPC] Servidor a escutar na porta {self.grpc_port}")

    # ── ENVIO DE MENSAGENS ────────────────────────────────────────

    async def broadcast(self, content: str):
        """
        Envia uma mensagem a TODOS os peers conhecidos via gRPC.

        Equivalente a escrever num canal do Discord:
        a mensagem vai para toda a gente ao mesmo tempo.

        No teu jogo DnD, em vez de broadcast, vais fazer
        unicast (um jogador → outro jogador específico).
        """
        members = await self.get_members()

        if not members:
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
        for username, addr in members.items():
            if username == self.username:
                continue  # não nos enviamos a nós próprios
            tasks.append(self._send_to_peer(username, addr, msg))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_to_peer(self, username: str, addr: str, msg):
        """
        Abre uma ligação gRPC a um peer específico e envia a mensagem.

        Note o 'async with' — a ligação é aberta, usada, e fechada
        automaticamente. Em produção, mantínhas a ligação aberta
        para melhor performance.
        """
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

    async def ping_peer(self, username: str) -> bool:
        """Verifica se um peer está online."""
        members = await self.get_members()
        if username not in members:
            print(f"[!] '{username}' não está registado na DHT.")
            return False

        addr = members[username]
        host, port = addr.split(":")
        try:
            async with grpc.aio.insecure_channel(f"{host}:{port}") as channel:
                stub = chat_pb2_grpc.ChatServiceStub(channel)
                resp = await asyncio.wait_for(
                    stub.Ping(chat_pb2.PingRequest(from_user=self.username)),
                    timeout=2.0
                )
                return resp.alive
        except Exception:
            return False

    # ── COMANDOS DO TERMINAL ──────────────────────────────────────

    async def handle_command(self, text: str) -> bool:
        """
        Processa comandos especiais (começam com /).
        Retorna True se foi um comando, False se é mensagem normal.
        """
        if not text.startswith("/"):
            return False

        parts = text.split()
        cmd   = parts[0].lower()

        if cmd == "/members":
            members = await self.get_members()
            print("\n[Canal #general]")
            for u, addr in members.items():
                marker = " ← (tu)" if u == self.username else ""
                print(f"  • {u} ({addr}){marker}")
            print()

        elif cmd == "/ping" and len(parts) > 1:
            target = parts[1]
            print(f"[~] A fazer ping a {target}...")
            online = await self.ping_peer(target)
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
        """
        Loop principal de input usando aioconsole.

        aioconsole.ainput() é como o input() normal, MAS não bloqueia
        o event loop — o asyncio pode continuar a processar mensagens
        recebidas enquanto esperamos que o utilizador escreva.

        Sem aioconsole, o input() bloquearia tudo e não receberíamos
        mensagens de outros peers enquanto estivéssemos a escrever.
        """
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

    # ── ARRANQUE ──────────────────────────────────────────────────


    async def run(self):
        """Arranca todos os componentes e inicia o loop."""
        await self.start_kademlia()
        await self.start_grpc_server()
        await self.register_in_dht()
        asyncio.create_task(self._refresh_loop())
        await self.input_loop()

    async def _refresh_loop(self):
        """Re-anuncia a nossa presença na DHT a cada 30s."""
        while True:
            await asyncio.sleep(30)
            try:
                await self.register_in_dht()
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
