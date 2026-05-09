"""
chat_servicer.py
Implementação dos métodos gRPC definidos no chat.proto.
Recebe chamadas de outros peers e delega ao ChatNode.
"""

import time
import chat_pb2
import chat_pb2_grpc


class ChatServicer(chat_pb2_grpc.ChatServiceServicer):
    """
    Cada instância está ligada ao ChatNode que a criou.
    O node é passado no construtor para que o servicer possa:
      - registar peers novos que nos contactam
      - saber o nosso próprio username / node_id
    """

    def __init__(self, node):
        self.node = node

    # ── MENSAGENS ──────────────────────────────────────────────────

    async def SendMessage(self, request, context):
        """Recebe uma mensagem de chat de outro peer."""
        ts = time.strftime("%H:%M:%S", time.localtime(request.timestamp))

        # Aprender o peer se ainda não o conhecemos
        if request.sender_node_id not in self.node.peers:
            self.node.peers[request.sender_node_id] = {
                "username":  request.sender,
                "grpc_addr": request.sender_addr,
                "last_seen": str(time.time()),
            }

        # Limpar a linha do terminal e mostrar a mensagem recebida
        print(f"\r\033[K[{ts}] \033[1m{request.sender}\033[0m: {request.content}")
        print("> ", end="", flush=True)

        return chat_pb2.Ack(received=True, by=self.node.username)

    # ── PING ───────────────────────────────────────────────────────

    async def Ping(self, request, context):
        """Responde a pings — confirma que o node está vivo."""
        return chat_pb2.PingResponse(username=self.node.username, alive=True)

    # ── INFO ───────────────────────────────────────────────────────

    async def GetPeerInfo(self, request, context):
        """Devolve informação sobre este node (usado no handshake inicial)."""
        return chat_pb2.PeerInfoResponse(
            node_id=self.node.node_id,
            username=self.node.username,
            grpc_addr=f"{self.node.host}:{self.node.grpc_port}",
            alive=True,
        )