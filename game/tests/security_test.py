"""
security_test.py
Testes de segurança ao servidor gRPC — valida que o host rejeita inputs maliciosos ou fora dos limites esperados.
 
Como correr
 - usar os scripts bash providenciados. com pwd da pasta game, correr
    - ./tests/scripts/run_security_test.sh
 - setup manual
    - dois terminais, um com username alice (host do lobby) e outro com username hacker
    - anotar o endereço e port gRPC do alice
    - num 3º terminal correr:
        uv run python tests/security_test.py <aliceAddress:gRPCport>
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grpc
import game_pb2
import game_pb2_grpc

addr = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:50051"

ch = grpc.insecure_channel(addr)
stub = game_pb2_grpc.GameServiceStub(ch)

print(f"\nA testar servidor em {addr}\n{'─'*50}")

# ataques
ATTACK_CASES = [
    ("Dano absurdo", dict(attacker="hacker", target="alice", attack_roll=999, base_damage_roll=999, attacker_x=0, attacker_y=0, effective_range=10)),
    ("Fora de alcance", dict(attacker="hacker", target="alice", attack_roll=20, base_damage_roll=8, attacker_x=0, attacker_y=0, effective_range=1)),
    ("Target inexistente", dict(attacker="hacker", target="utilizador_fantasma", attack_roll=20, base_damage_roll=8, attacker_x=0, attacker_y=0, effective_range=10)),
]

all_passed = True

for name, kwargs in ATTACK_CASES:
    req = game_pb2.AttackRequest(**kwargs)
    try:
        resp = stub.HostAttack(req, timeout=3)
        result = f"hit={resp.hit}  dmg={resp.damage_dealt}  hp_restante={resp.target_hp_remaining}  morreu={resp.target_died}"
        if resp.hit or resp.damage_dealt > 0:
            result += " \033[31m[FALHOU: Ataque não bloqueado!]\033[0m"
            all_passed = False
    except grpc.RpcError as e:
        result = f"RpcError: {e.code()}"
    except Exception as e:
        result = f"Erro: {e}"
    print(f"[AT] {name:<25} → {result}")

print()

# movimentos 
MOVE_CASES = [
    ("Coords negativas", dict(username="hacker", x=-1, y=-1)),
    ("Coords excessivas", dict(username="hacker", x=999, y=999))]

for name, kwargs in MOVE_CASES:
    req = game_pb2.MoveRequest(**kwargs)
    try:
        resp = stub.HostMove(req, timeout=3)
        result = f"applied={resp.applied}"
        if resp.applied:
            result += " \033[31m[FALHOU: Mov. não bloqueado!]\033[0m"
            all_passed = False
    except grpc.RpcError as e:
        result = f"RpcError: {e.code()}"
    except Exception as e:
        result = f"Erro: {e}"
    print(f"[MV] {name:<25} → {result}")

print()

if not all_passed:
    print("\033[31m[!] Pelos menos um teste falhou.\033[0m")
    sys.exit(1)
else:
    print("\033[32m[+] Todos os testes passaram e foram bloqueados corretamente.\033[0m")