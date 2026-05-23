"""
test_cross_lobby.py
Confirma que jogadores em lobbies diferentes não se podem atacar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PORQUÊ ESTE TESTE FUNCIONA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

O host de um lobby só conhece os jogadores do seu próprio lobby
(via state.others). Quando recebe um HostAttack de um atacante
desconhecido, o resolve_attack rejeita-o imediatamente:

    attacker, _ = self._lookup_player(request.attacker)
    if attacker is None:
        → hit=False, damage=0   # atacante não é do lobby

Este teste tem dois cenários:

  Cenário A (simples, 1 lobby):
    Envia um ataque de "charlie" (nome desconhecido pelo host de alice).
    O host deve rejeitar porque charlie não está no lobby de alice.

  Cenário B (completo, 2 lobbies):
    Envia um ataque do host do lobby B para o host do lobby A.
    Requer dois jogos a correr em simultâneo — ver instruções abaixo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMO CORRER — CENÁRIO A (recomendado para começar)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TERMINAL 1 — host do lobby A (alice):

    uv run python main_game.py --username alice --grpc-port 50051 --kad-port 50052 --first

    No menu:
        > class fighter
        > lobby create lobbyA 4
        > ready

TERMINAL 2 — segundo jogador do lobby A (bob):

    uv run python main_game.py --username bob

    No menu:
        > class fighter
        > lobby join <CÓDIGO>
        > ready

    De volta ao terminal 1:
        > lobby start

TERMINAL 3 — correr o teste:

    uv run python tests/test_cross_lobby.py 127.0.0.1:50051

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMO CORRER — CENÁRIO B (dois lobbies reais)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TERMINAL 1 — host do lobby A (alice):

    uv run python main_game.py --username alice --grpc-port 50051 --kad-port 50052 --first

    No menu:
        > class fighter
        > lobby create lobbyA 4
        > ready

TERMINAL 2 — segundo jogador do lobby A (bob):

    uv run python main_game.py --username bob

    No menu:
        > class fighter
        > lobby join <CÓDIGO_A>
        > ready

    De volta ao terminal 1 → lobby start

TERMINAL 3 — host do lobby B (charlie), com portas diferentes:

    uv run python main_game.py --username charlie --grpc-port 50061 --kad-port 50062

    No menu:
        > class fighter
        > lobby create lobbyB 4
        > ready

TERMINAL 4 — segundo jogador do lobby B (dave):

    uv run python main_game.py --username dave

    No menu:
        > class fighter
        > lobby join <CÓDIGO_B>
        > ready

    De volta ao terminal 3 → lobby start

TERMINAL 5 — correr o teste com ambos os endereços:

    uv run python tests/test_cross_lobby.py 127.0.0.1:50051 --attacker charlie --attacker-addr 127.0.0.1:50061

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTERPRETAR OS RESULTADOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  PASS → hit=False e damage_dealt=0  (ataque rejeitado pelo host)
  FAIL → hit=True  ou damage_dealt>0  (isolamento de lobby quebrado)

"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import argparse
import grpc
import game_pb2
import game_pb2_grpc


def run_test(host_addr: str, attacker: str, target: str, attacker_addr: str = ""):
    ch   = grpc.insecure_channel(host_addr)
    stub = game_pb2_grpc.GameServiceStub(ch)

    req = game_pb2.AttackRequest(
        attacker=attacker,
        target=target,
        weapon="Longsword",
        attack_roll=999,          # auto-hit para garantir que, se passar, faz dano
        base_damage_roll=50,      # dano alto para ser óbvio se passar
        attacker_x=0,
        attacker_y=0,
        effective_range=99,       # alcance máximo para não ser rejeitado por range
        attacker_class="fighter",
        is_spell=False,
    )

    print(f"\n{'─'*55}")
    print(f"Host alvo    : {host_addr}")
    print(f"Atacante     : {attacker}" + (f" ({attacker_addr})" if attacker_addr else " [nome desconhecido]"))
    print(f"Alvo         : {target}")
    print(f"{'─'*55}")

    try:
        resp = stub.HostAttack(req, timeout=3)
    except grpc.RpcError as e:
        print(f"RpcError: {e.code()} — {e.details()}")
        return

    hit    = resp.hit
    damage = resp.damage_dealt

    blocked = (not hit) and (damage == 0)

    status = "\033[32mPASS\033[0m" if blocked else "\033[31mFAIL\033[0m"
    print(f"Resultado    : hit={hit}  damage_dealt={damage}  hp_restante={resp.target_hp_remaining}")
    print(f"Isolamento   : {status}")

    if not blocked:
        print("\033[31m[!] O host processou um ataque de fora do lobby — isolamento quebrado!\033[0m")
    else:
        print("O host rejeitou o ataque — atacante não reconhecido no lobby.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("host_addr",      nargs="?", default="127.0.0.1:50051",
                   help="Endereço gRPC do host do lobby A (alvo do ataque)")
    p.add_argument("--target",       default="alice",
                   help="Username do jogador a atacar (deve estar no lobby A)")
    p.add_argument("--attacker",     default="charlie",
                   help="Username do atacante (deve ser de fora do lobby A)")
    p.add_argument("--attacker-addr", default="",
                   help="(Opcional) Endereço gRPC do lobby B, apenas para documentar o cenário")
    args = p.parse_args()

    run_test(
        host_addr=args.host_addr,
        attacker=args.attacker,
        target=args.target,
        attacker_addr=args.attacker_addr,
    )


if __name__ == "__main__":
    main()