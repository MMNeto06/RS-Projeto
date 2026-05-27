# DND Death
Projeto de Redes e Serviços. 

Desenvolvido por:
- João Farias (124964)
- Miguel Neto (125200)
- Diogo Silva (125240)

Universidade de Aveiro 25/26

## Tema
O DND DEATH é um jogo competitivo de Deathmatch que transpõe mecânicas clássicas de Dungeons and Dragons para um ambiente distribuído. 

O foco reside na exploração de redes descentralizadas P2P para descoberta de jogadores e sessões.

Utilizamos uma arquitetura híbrida para garantir escalabilidade na descoberta (Kademlia DHT) e baixa latência na jogabilidade (gRPC sobre TCP), priorizando a integridade das ações de jogo.

## Instruções de Utilização

### Modo automático (recomendado para jogar na mesma rede)
- Primeiro a arrancar (torna-se bootstrap, anuncia presença na LAN):
  uv run main_game.py --username Alice

- Todos os outros (descobrem o bootstrap via broadcast UDP):
  uv run main_game.py --username Bob

- Sem flags — pede o username interactivamente:
  uv run main_game.py

O IP local é detectado automaticamente. As portas gRPC e Kademlia são
escolhidas pelo SO (livres). O bootstrap é descoberto via beacon UDP na LAN.

### Modo manual (para testes / forwarding / WAN) 

- Bootstrap:

  uv run main_game.py --username Alice --host 192.168.1.10 \
                      --grpc-port 50051 --kad-port 8468

- Outros nodes:

  uv run main_game.py --username Bob --host 192.168.1.20 \
                      --grpc-port 50052 --kad-port 8469 \
                      --bootstrap-host 192.168.1.10 --bootstrap-kad-port 8468

- Flags úteis: 
  - **--first**          Salta a descoberta de bootstrap e assume logo o papel
                   de primeiro node (útil se o timeout de 4s for lento).
  - **--discovery-timeout N**   Segundos à espera de beacon (default: 4).

#### Como funciona o --host 

O servidor gRPC escuta em 0.0.0.0 (todas as interfaces), mas o endereço
que fica registado na DHT é "{host}:{grpc_port}". Os outros peers lêem
esse endereço da DHT para te contactar. Se --host for 127.0.0.1, cada peer
tenta ligar ao seu próprio loopback — e falha. Na LAN tens de anunciar o
teu IP 192.168.1.xxx. Este script detecta-o automaticamente.


