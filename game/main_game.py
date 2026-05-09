"""
game/main_game.py  —  entry point do jogo multiplayer

Uso:
  # Primeiro node (bootstrap):
  uv run main_game.py --username Alice --class fighter \
                      --grpc-port 50051 --kad-port 8468

  # Nodes seguintes:
  uv run main_game.py --username Bob   --class wizard  \
                      --grpc-port 50052 --kad-port 8469 \
                      --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468

Classes disponíveis: fighter, wizard, rogue, cleric, ranger
"""

import asyncio
import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dnd_classes import get_class, list_classes
from game_node   import GameNode


def parse_args():
    p = argparse.ArgumentParser(description="DnD Deathmatch — P2P gRPC + Kademlia")
    p.add_argument("--username",           required=True,           help="O teu username")
    p.add_argument("--class",              dest="dnd_class",
                   required=True,          help="Classe DnD (fighter/wizard/rogue/cleric/ranger)")
    p.add_argument("--host",               default="127.0.0.1")
    p.add_argument("--grpc-port",          type=int, required=True)
    p.add_argument("--kad-port",           type=int, required=True)
    p.add_argument("--bootstrap-host",     default=None)
    p.add_argument("--bootstrap-kad-port", type=int, default=None)
    p.add_argument("--match-id",           default="match1")
    return p.parse_args()


async def main():
    args = parse_args()

    dnd_class = get_class(args.dnd_class)
    if not dnd_class:
        print(f"[!] Classe desconhecida: '{args.dnd_class}'")
        print(list_classes())
        return

    bootstrap = None
    if args.bootstrap_host and args.bootstrap_kad_port:
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)

    print(f"\n{list_classes()}")
    print(f"A jogar como {args.username} — {dnd_class.name}")
    print(f"  HP: {dnd_class.hp_base}  AC: {dnd_class.armor_class_base}  Arma: {dnd_class.weapon}\n")

    node = GameNode(
        username=args.username,
        dnd_class=dnd_class,
        host=args.host,
        grpc_port=args.grpc_port,
        kad_port=args.kad_port,
        bootstrap=bootstrap,
        match_id=args.match_id,
    )
    await node.run()


if __name__ == "__main__":
    asyncio.run(main())