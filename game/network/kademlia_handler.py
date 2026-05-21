"""Helpers for Kademlia-based discovery (non-mutating helpers)."""
import asyncio
from typing import Iterable, List, Tuple, Dict, Callable, Optional


from kademlia.utils import digest
import kademlia.node as kad_node

from dht_keys import addr_key
from dht_keys import username_key
import time
import grpc
import game_pb2
import game_pb2_grpc
import os



async def init_kad(kad, kad_port: int, bootstrap: Optional[str] = None) -> None:
    #start listening 
    await kad.listen(kad_port)

    #if there is another peer, then bootstrap to it
    if bootstrap:
        print(f"[DHT] A ligar ao bootstrap {bootstrap}…")
        await kad.bootstrap([bootstrap])
        await asyncio.sleep(1)
        print("[DHT] Ligado!")
    else: #if not, then just dont do anything, the network starts with the listen
        print("[DHT] Primeiro node (bootstrap).")


async def find_neighbor_addrs(kad, host: str, kad_port: int, ksize: int = 20) -> List[Tuple[str, str]]:
    #returns a list of (node_id, grpc_addr) tuples, for each node found in the dht;
    await kad.get("game:discover") #makes sure the routing table isnt stale
    target = kad_node.Node(digest(os.urandom(16))) #random target for more diverse results
    neighbors = kad.protocol.router.find_neighbors(target)[:ksize] #get nodes closest to target

    results: List[Tuple[str, str]] = []
    for n in neighbors:
        if (n.ip, n.port) == (host, kad_port): #skip self
            continue
        nid = n.id.hex()
        addr = await kad.get(addr_key(nid)) #gets the grpc address from the DHT
        if addr:
            results.append((nid, addr))
    return results


async def probe_addrs(kad, node_ids: Iterable[str]) -> Dict[str, str]:
    #look up all node ids given and return their grpc addresses in a dict
    out: Dict[str, str] = {}
    for nid in node_ids:
        addr = await kad.get(addr_key(nid))
        if addr:
            out[nid] = addr
    return out


async def register_self(kad, node_id: str, host: str, grpc_port: int, username: str, peers: dict, class_name: str) -> None:
    #registers the local node in the DHT and populates the peers map.

    addr = f"{host}:{grpc_port}" #get address
    await kad.set(addr_key(node_id), addr) #set the address in the DHT
    await kad.set(username_key(username), node_id) #set username in the DHT
    peers[node_id] = { #register own data in the peers list
        "node_id":   node_id,
        "username":  username,
        "grpc_addr": addr,
        "last_seen": time.time(),
        "class_name": class_name,
    }


async def handshake(addr: str, node_id_prop: str, peers: dict, remote_node_id: str = "") -> None:
    #do a ping handshake and populate peers 
    host, port = addr.split(":")  #get host and port
    try:
        async with grpc.aio.insecure_channel(f"{host}:{port}") as ch:
            stub = game_pb2_grpc.GameServiceStub(ch)
            resp = await asyncio.wait_for(
                stub.Ping(game_pb2.PingRequest(from_user=node_id_prop)),
                timeout=2.0,
            )
            key = remote_node_id if remote_node_id else resp.username
            if key not in peers:
                peers[key] = {
                    "node_id":    remote_node_id,
                    "username":   resp.username,
                    "grpc_addr":  addr,
                    "last_seen":  time.time(),
                    "class_name": "",
                }
    except Exception:
        pass
