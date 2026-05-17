"""
proto_helpers.py
Helpers para construir mensagens protobuf a partir do estado local do jogo.

Centralizar aqui evita duplicação da construção de PlayerState espalhada por
game_node.py, game_servicer.py e qualquer outro módulo que precise de serializar
estado de jogadores para gRPC.
"""

import game_pb2
from game_state import LocalPlayer, RemotePlayer


def local_player_to_proto(lp: LocalPlayer, match_id: str) -> game_pb2.PlayerState:
    """Serializa um LocalPlayer para PlayerState protobuf."""
    return game_pb2.PlayerState(
        username=lp.username,
        hp=lp.hp,
        max_hp=lp.max_hp,
        x=lp.x,
        y=lp.y,
        alive=lp.alive,
        class_name=lp.dnd_class.name,
        status_effects=lp.status_effects,
        armor_class=lp.armor_class,
        kills=lp.kills,
        deaths=lp.deaths,
        match_id=match_id,
    )


def remote_player_to_proto(p: RemotePlayer, match_id: str) -> game_pb2.PlayerState:
    """Serializa um RemotePlayer para PlayerState protobuf."""
    return game_pb2.PlayerState(
        username=p.username,
        hp=p.hp,
        max_hp=p.max_hp,
        x=p.x,
        y=p.y,
        alive=p.alive,
        class_name=p.class_name,
        status_effects=p.status_effects,
        armor_class=p.armor_class,
        kills=p.kills,
        deaths=p.deaths,
        match_id=match_id,
    )