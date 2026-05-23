import sys
import os
import argparse
import grpc
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import game_pb2
import game_pb2_grpc


def host_move(addr: str, username: str, x: int, y: int, timeout: float = 3.0):
    ch = grpc.insecure_channel(addr)
    stub = game_pb2_grpc.GameServiceStub(ch)
    req = game_pb2.MoveRequest(username=username, x=int(x), y=int(y))
    try:
        resp = stub.HostMove(req, timeout=timeout)
        print(f"Move -> username={username}, x={x}, y={y}, applied={resp.applied}")
        return resp
    except Exception as e:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("addr")
    p.add_argument("--username", dest="username")
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    args = p.parse_args()
    host_move(args.addr, args.username, args.x, args.y)


if __name__ == '__main__':
    main()
