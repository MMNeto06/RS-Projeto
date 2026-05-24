"""
test_cross_lobby.py
Confirma que jogadores em lobbies diferentes não se podem atacar.

Como correr
 - usar os scripts bash providenciados. com pwd da pasta game, correr
    - ./tests/scripts/run_cross_lobby_test.py
    - ./tests/scripts/run_cross_lobby_test.py --scenrario b
 - setup manual
    - cenário A
        - dois terminais, primeira pessoa tem username alice, é host do lobby, lembrar/especificar address e port gRPC
        - segundo jogador, username bob
        - correr uv run python tests/test_cross_lobby.py <aliceAddress:gRPCport>
    - cenário B
        - tudo do cenário A, mas criar mais dois jogadores, charlie e dave, ou seja mais dois terminais
        - guardar address e port gRPC do charlie
        - correr uv run python tests/test_cross_lobby.py <aliceAddress:gRPCport> --attacker charlie --attacker-addr <charlieAddress:gRPCport>
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
        attack_roll=999,
        base_damage_roll=50,
        attacker_x=0,
        attacker_y=0,
        effective_range=99,
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
    p.add_argument("host_addr", nargs="?", default="127.0.0.1:50051", help="Endereço gRPC do host do lobby A (alvo do ataque)")
    p.add_argument("--target", default="alice", help="Username do jogador a atacar (deve estar no lobby A)")
    p.add_argument("--attacker", default="charlie", help="Username do atacante (deve ser de fora do lobby A)")
    p.add_argument("--attacker-addr", default="", help="(Opcional) Endereço gRPC do lobby B, apenas para documentar o cenário")
    args = p.parse_args()

    run_test(
        host_addr=args.host_addr,
        attacker=args.attacker,
        target=args.target,
        attacker_addr=args.attacker_addr,
    )


if __name__ == "__main__":
    main()