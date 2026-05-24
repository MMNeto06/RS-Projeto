"""
load_test.py
Teste de carga ao servidor gRPC — mede throughput e taxa de erro sob N clientes simultâneos a atacar.

Como correr
 - usar os scripts bash providenciados. com pwd da pasta game, correr (susbstituir N e M por números se quiserem especificar essas flags)
    - ./tests/scripts/run_load_test.sh [--clients N] [--requests M]
 - setup manual
    - dois terminais, um com username alice (host do lobby) e outro com username loadbot
    - anotar o endereço e port gRPC do alice
    - num 3º terminal correr:
        uv run python tests/load_test.py <aliceAddress:gRPCport>
        uv run python tests/load_test.py <aliceAddress:gRPCport>
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import argparse
import time
import grpc
import game_pb2
import game_pb2_grpc

async def spam_attacks(addr: str, n_rounds: int, results: list):
    ch = grpc.aio.insecure_channel(addr)
    stub = game_pb2_grpc.GameServiceStub(ch)
    req = game_pb2.AttackRequest(
        attacker="loadbot",
        target="alice",
        attack_roll=10,
        base_damage_roll=5,
        effective_range=10,
    )
    ok = err = 0
    for _ in range(n_rounds):
        try:
            await asyncio.wait_for(stub.HostAttack(req), timeout=2)
            ok += 1
        except Exception:
            err += 1
    results.append((ok, err))
    await ch.close()


async def main(addr: str, n_clients: int, n_rounds: int):
    results: list = []
    print(f"A enviar {n_clients} × {n_rounds} = {n_clients * n_rounds} requests para {addr}…\n")

    t0 = time.perf_counter()
    await asyncio.gather(*[
        spam_attacks(addr, n_rounds, results)
        for _ in range(n_clients)
    ])
    elapsed = time.perf_counter() - t0

    total_ok = sum(r[0] for r in results)
    total_err = sum(r[1] for r in results)
    total = total_ok + total_err
    rps = total / elapsed if elapsed > 0 else 0

    print(f"{'─'*40}")
    print(f"Clientes : {n_clients}")
    print(f"Rounds   : {n_rounds}")
    print(f"Total    : {total}")
    print(f"OK       : {total_ok}")
    print(f"Erros    : {total_err}  {'AVISO: há problemas de estabilidade' if total_err > 0 else '✓'}")
    print(f"Tempo    : {elapsed:.2f}s")
    print(f"RPS      : {rps:.1f}")
    print(f"{'─'*40}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("addr", nargs="?", default="127.0.0.1:50051")
    p.add_argument("--clients", type=int, default=20)
    p.add_argument("--rounds",  type=int, default=10)
    args = p.parse_args()
    asyncio.run(main(args.addr, args.clients, args.rounds))