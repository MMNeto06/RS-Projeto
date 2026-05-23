#!/usr/bin/env python3

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import game_pb2
import game_pb2_grpc
import sys
import os
import argparse
import grpc

def host_attack(addr: str, attacker: str, target: str, weapon: str = "", attack_roll: int = 999, base_damage: int = 999, attacker_class: str = "", attacker_x: int = 0, attacker_y: int = 0, effective_range: int = 10, is_spell: bool = False, timeout: float = 5.0):
    ch = grpc.insecure_channel(addr)
    stub = game_pb2_grpc.GameServiceStub(ch)
    req = game_pb2.AttackRequest(
        attacker=attacker,
        weapon=weapon,
        attack_roll=attack_roll,
        base_damage_roll=base_damage,
        target=target,
        is_spell=is_spell,
        attacker_class=attacker_class,
        attacker_x=attacker_x,
        attacker_y=attacker_y,
        effective_range=effective_range,
    )
    try:
        resp = stub.HostAttack(req, timeout=timeout)
        print(f"attack -> hit={resp.hit} damage_dealt={resp.damage_dealt} target_hp_remaining={resp.target_hp_remaining} target_died={resp.target_died}")
        return resp
    except Exception as e:
        return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("addr")
    p.add_argument("--attacker", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--weapon", default="")
    p.add_argument("--attack-roll", type=int)
    p.add_argument("--base-damage", type=int)
    p.add_argument("--attacker-class")
    p.add_argument("--attacker-x", type=int)
    p.add_argument("--attacker-y", type=int)
    p.add_argument("--range", type=int)
    p.add_argument("--is-spell", action="store_true")
    args = p.parse_args()
    host_attack(
        args.addr,
        attacker=args.attacker,
        target=args.target,
        weapon=args.weapon,
        attack_roll=args.attack_roll,
        base_damage=args.base_damage,
        attacker_class=args.attacker_class,
        attacker_x=args.attacker_x,
        attacker_y=args.attacker_y,
        effective_range=args.range,
        is_spell=args.is_spell,
    )

if __name__ == '__main__':
    main()
