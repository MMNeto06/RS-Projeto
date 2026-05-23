"""
security_test.py
Testes de segurança ao servidor gRPC — valida que o host rejeita
inputs maliciosos ou fora dos limites esperados.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMO CORRER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

é um pouco weird e não sei se está muito bom, mas é assim
 - 2 terminais, ambos a correr o jogo, um com username alice e outro com username hacker
 - num 3º terminal correr o coamndo
    uv run python tests/load_test.py <host-addr:gRPC-port>

não sei o quão funcional está

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
O QUE CADA TESTE VALIDA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Dano absurdo          → attack_roll=999, base_damage=999
                          O host deve limitar/rejeitar — damage_dealt não
                          deve ser 999.

  Fora de alcance       → attacker em (0,0), effective_range=1
                          Se alice não estiver em (0,0) ou adjacente,
                          o host deve rejeitar (hit=False).

  Target inexistente    → target="utilizador_que_nao_existe"
                          O servidor não deve crashar.

  Coordenadas negativas → attacker_x=-5, attacker_y=-5
                          O servidor não deve crashar nem aceitar posições
                          fora do mapa.

  Move out-of-bounds    → x=-1 e x=999
                          O host deve clampar ou rejeitar (applied=False).

  Move de outro user    → username="hacker" tenta mover "alice"
                          O host não deve aplicar o movimento.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTERPRETAR OS RESULTADOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [AT] → resultado de HostAttack
  [MV] → resultado de HostMove

  Resultados suspeitos:
    - damage_dealt=999 no teste de dano absurdo  → sem validação de dano
    - hit=True no teste fora de alcance          → sem validação de range
    - applied=True nos testes de move inválido   → sem validação de coords

"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grpc
import game_pb2
import game_pb2_grpc

addr = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:50051"

ch   = grpc.insecure_channel(addr)
stub = game_pb2_grpc.GameServiceStub(ch)

print(f"\nA testar servidor em {addr}\n{'─'*50}")

# ── ATAQUES ────────────────────────────────────────────────────────

ATTACK_CASES = [
    ("Dano absurdo",          dict(attacker="hacker", target="alice", attack_roll=999,  base_damage_roll=999, attacker_x=0,  attacker_y=0,  effective_range=10)),
    ("Fora de alcance",       dict(attacker="hacker", target="alice", attack_roll=20,   base_damage_roll=8,   attacker_x=0,  attacker_y=0,  effective_range=1)),
    ("Target inexistente",    dict(attacker="hacker", target="utilizador_fantasma",     attack_roll=20,   base_damage_roll=8,   attacker_x=0,  attacker_y=0,  effective_range=10)),
    ("Coords negativas",      dict(attacker="hacker", target="alice", attack_roll=20,   base_damage_roll=8,   attacker_x=-5, attacker_y=-5, effective_range=10)),
]

for name, kwargs in ATTACK_CASES:
    req = game_pb2.AttackRequest(**kwargs)
    try:
        resp   = stub.HostAttack(req, timeout=3)
        result = f"hit={resp.hit}  dmg={resp.damage_dealt}  hp_restante={resp.target_hp_remaining}  morreu={resp.target_died}"
    except grpc.RpcError as e:
        result = f"RpcError: {e.code()}"
    except Exception as e:
        result = f"Erro: {e}"
    print(f"[AT] {name:<25} → {result}")

print()

# ── MOVIMENTOS ─────────────────────────────────────────────────────

MOVE_CASES = [
    ("Coords negativas",   dict(username="hacker", x=-1,  y=-1)),
    ("Coords excessivas",  dict(username="hacker", x=999, y=999)),
    ("Mover outro user",   dict(username="alice",  x=5,   y=5)),
]

for name, kwargs in MOVE_CASES:
    req = game_pb2.MoveRequest(**kwargs)
    try:
        resp   = stub.HostMove(req, timeout=3)
        result = f"applied={resp.applied}"
    except grpc.RpcError as e:
        result = f"RpcError: {e.code()}"
    except Exception as e:
        result = f"Erro: {e}"
    print(f"[MV] {name:<25} → {result}")

print()