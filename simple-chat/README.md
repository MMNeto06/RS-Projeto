# P2P Chat — Canal estilo Discord
 
Sistema de mensagens P2P para aprender as ferramentas do Tema 5.
 
## Stack
| Tecnologia | Função aqui | No jogo DnD |
|---|---|---|
| **Kademlia DHT** | Diretório de peers (username → IP:porta) | Descobrir onde está cada jogador |
| **gRPC** | Enviar/receber mensagens | Enviar ataques, movimentos, ações |
| **Protocol Buffers** | Serializar as mensagens | Serializar `AttackAction`, `MoveAction`, etc. |
| **aioconsole** | Input sem bloquear o event loop | Escrever comandos enquanto recebes ataques |
| **asyncio** | Correr tudo em simultâneo | Gerir estado, concorrência, timers |
 
---
 
## Setup
 
 1. já deve estar, mas caso não esteja, metam tudo de requiremnts na lista dependencies em pyproject.toml ou façam uv add de cada dependência

 2. Gerar código Python a partir do .proto (só precisas de fazer isto uma vez, ou quando mudares o .proto) 
    -> já deve estar feito -Miguel
```bash
uv run python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. chat.proto
```
 
---
 
## Como usar (3 terminais)
 
**Terminal 1 — Alice (bootstrap node, primeiro a entrar):**
```bash
uv run node.py --username Alice --grpc-port 50051 --kad-port 8468
```
 
**Terminal 2 — Bob:**
```bash
uv run node.py --username Bob --grpc-port 50052 --kad-port 8469 \
               --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468
```
 
**Terminal 3 — Carlos:**
```bash
uv run node.py --username Carlos --grpc-port 50053 --kad-port 8470 \
               --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468
```
 
Agora qualquer mensagem escrita num terminal aparece nos outros dois.
 
---
 
## Comandos no chat
 
| Comando | Descrição |
|---|---|
| `/members` | Lista todos os membros no canal |
| `/ping <user>` | Verifica se um user está online |
| `/help` | Mostra ajuda |
| `/quit` | Sair |
 
---
 
## Estrutura do projeto
 
```
p2p_chat/
├── chat.proto          ← definição gRPC (edita aqui para o DnD)
├── chat_pb2.py         ← gerado automaticamente pelo protoc
├── chat_pb2_grpc.py    ← gerado automaticamente pelo protoc
├── node.py             ← lógica principal do node P2P
└── requirements.txt
```
 
---
 
## O que mudar para o jogo DnD
 
### 1. Adicionar ações no `.proto`
```proto
service GameService {
  rpc Attack  (AttackAction) returns (ActionResult);
  rpc Move    (MoveAction)   returns (ActionResult);
  rpc GetState(Empty)        returns (PlayerState);
}
 
message AttackAction {
  string attacker = 1;
  string target   = 2;
  string weapon   = 3;
  int32  damage   = 4;
}
 
message PlayerState {
  string username = 1;
  int32  hp       = 2;
  int32  x        = 3;
  int32  y        = 4;
  bool   alive    = 5;
}
```
 
### 2. Guardar estado do jogador na DHT
Em vez de guardar só `"host:port"`, guardas um JSON com HP, posição, etc.
 
### 3. Trocar broadcast por unicast
No DnD atacas um jogador específico, não todos — usa `_send_to_peer()` diretamente.
