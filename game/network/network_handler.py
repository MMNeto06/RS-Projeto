#kad helpers
import asyncio
from typing import Iterable, List, Tuple, Dict, Callable, Optional


from kademlia.utils import digest
import kademlia.node as kad_node

from dht_keys import addr_key
from dht_keys import username_key
import time
import game_pb2
import game_pb2_grpc
import os



async def init_kad(kad, kad_port: int, bootstrap: Optional[str] = None) -> None:
    await kad.listen(kad_port)

    if bootstrap:
        print(f"[DHT] A ligar ao bootstrap {bootstrap}…")
        await kad.bootstrap([bootstrap])
        await asyncio.sleep(1)
        print("[DHT] Ligado!")
    else: 
        print("[DHT] Primeiro node (bootstrap).")


async def find_neighbor_addrs(kad, host: str, kad_port: int, ksize: int = 20) -> List[Tuple[str, str]]:
    await kad.get("game:discover") 
    target = kad_node.Node(digest(os.urandom(16))) 
    neighbors = kad.protocol.router.find_neighbors(target)[:ksize] 

    candidate_ids: List[str] = []
    for n in neighbors:
        if (n.ip, n.port) == (host, kad_port): 
            continue
        nid = n.id.hex()
        if nid in candidate_ids: 
            continue
        candidate_ids.append(nid)

    addr_map = await probe_addrs(kad, candidate_ids, 10, 0.5)

    results: List[Tuple[str, str]] = [] 
    for nid in candidate_ids:
        addr = addr_map.get(nid)
        if addr:
            results.append((nid, addr))
    return results


async def probe_addrs(kad, node_ids: Iterable[str], concurrency: int, timeout: float) -> Dict[str, str]:
    sem = asyncio.Semaphore(concurrency) 

    async def get_with_sem(nid: str):
        async with sem:
            try:
                addr = await asyncio.wait_for(kad.get(addr_key(nid)), timeout=timeout)
                return nid, addr
            except Exception:
                return nid, None

    out: Dict[str,str] = {}
    
    for result in asyncio.as_completed((get_with_sem(nid) for nid in node_ids)):
        nid, addr = await result
        if addr:
            out[nid] = addr
    return out


async def register_self(kad, node_id: str, host: str, grpc_port: int, username: str, peers: dict, class_name: str) -> None:
    addr = f"{host}:{grpc_port}"
    await kad.set(addr_key(node_id), addr)
    await kad.set(username_key(username), node_id)
    peers[node_id] = {
        "node_id":   node_id,
        "username":  username,
        "grpc_addr": addr,
        "last_seen": time.time(),
        "class_name": class_name,
    }


async def handshake(addr: str, node_id_prop: str, peers: dict, remote_node_id: str = "", stub: game_pb2_grpc.GameServiceStub = None) -> None:
    try:
        #do a ping handshake and populate peers 
        resp = await asyncio.wait_for(
            stub.Ping(game_pb2.PingRequest(from_user=node_id_prop)),
            timeout=2.0,
        )
        key = remote_node_id
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