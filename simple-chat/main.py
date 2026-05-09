"""
main.py  —  entry point do chat P2P

Uso:
  uv run main.py --username Alice --grpc-port 50051 --kad-port 8468
  uv run main.py --username Bob --grpc-port 50052 --kad-port 8469 \
                 --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468
"""

import asyncio
import argparse
import logging

from chat_node import ChatNode

# Reduzir ruído do Kademlia
logging.getLogger("kademlia").setLevel(logging.ERROR)

def parse_args():
    p = argparse.ArgumentParser(description="P2P Chat — gRPC + Kademlia")
    p.add_argument("--username", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--grpc-port", type=int, required=True)
    p.add_argument("--kad-port", type=int, required=True)
    p.add_argument("--bootstrap-host", default=None)
    p.add_argument("--bootstrap-kad-port", type=int, default=None)
    return p.parse_args()


async def main():
    args = parse_args()

    bootstrap = None
    if args.bootstrap_host and args.bootstrap_kad_port:
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)

    node = ChatNode(
        username=args.username,
        host=args.host,
        grpc_port=args.grpc_port,
        kad_port=args.kad_port,
        bootstrap=bootstrap,
    )
    await node.run()


if __name__ == "__main__":
    asyncio.run(main())