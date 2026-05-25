"""
test_combat_engine.py
Testes ao CombatEngine — lógica pura de combate, sem rede.
Cobre o guard contra uso de abilidades fora do índice da classe,
incluindo o cenário de um Fighter a tentar usar uma abilidade que não é sua.
Como correr:
    uv run pytest tests/test_combat_engine.py -v
ou via script:
    ./tests/scripts/run_unit_test.sh   (cobre ambos os ficheiros de testes unitários)
"""

import pytest
from unittest.mock import AsyncMock

from game_state import LocalPlayer, GameState, RemotePlayer
from dnd_classes import CLASSES
from combat_engine import CombatEngine

def _make_engine(cls_key: str, peers: dict | None = None):
    local = LocalPlayer(username="alice", node_id="", dnd_class=CLASSES[cls_key])
    state = GameState(local)
    if peers:
        state.others.update(peers)
    route_fn = AsyncMock(return_value=None)
    engine = CombatEngine(state=state, peers=peers or {}, route_attack_fn=route_fn)
    return engine, state, route_fn

def _make_peer(username: str) -> tuple[dict, RemotePlayer]:
    peer_info = {"username": username, "grpc_addr": "127.0.0.1:9999"}
    remote = RemotePlayer(
        username=username, node_id="x", grpc_addr="127.0.0.1:9999",
        hp=50, max_hp=50, x=0, y=0, alive=True,
        class_name="wizard", armor_class=12, kills=0, deaths=0,
    )
    return peer_info, remote

@pytest.mark.asyncio
async def test_out_of_bounds_positive_index_returns_false():
    engine, _, _ = _make_engine("fighter")
    result = await engine.use_ability(99)
    assert result is False


@pytest.mark.asyncio
async def test_negative_index_returns_false():
    """Índice negativo → False (sem crash)."""
    engine, _, _ = _make_engine("fighter")
    result = await engine.use_ability(-1)
    assert result is False


@pytest.mark.asyncio
async def test_fighter_max_valid_index_is_two():
    """Fighter tem 3 abilidades (0, 1, 2). Índice 3 está fora."""
    engine, _, _ = _make_engine("fighter")
    assert await engine.use_ability(3) is False
    assert await engine.use_ability(0) is True

@pytest.mark.asyncio
async def test_fighter_cannot_use_wizard_magic_missile():
    engine, state, _ = _make_engine("fighter")
    fighter_ab_names = [ab.name for ab in state.local.dnd_class.abilities]
    assert "Magic Missile" not in fighter_ab_names
    assert fighter_ab_names[0] == "Second Wind"


@pytest.mark.asyncio
async def test_fighter_cannot_use_wizard_fireball():
    engine, state, _ = _make_engine("fighter")
    fighter_ab_names = [ab.name for ab in state.local.dnd_class.abilities]
    assert "Fireball" not in fighter_ab_names
    for bad_idx in [3, 4, 5, 10, 100]:
        assert await engine.use_ability(bad_idx) is False


@pytest.mark.asyncio
async def test_wizard_cannot_use_fighter_second_wind():
    peer_info, remote = _make_peer("bob")
    engine, state, _ = _make_engine("wizard", peers={"bob_id": peer_info})
    state.others["bob"] = remote
    wizard_ab_names = [ab.name for ab in state.local.dnd_class.abilities]
    assert "Second Wind" not in wizard_ab_names
    assert wizard_ab_names[0] == "Magic Missile"

@pytest.mark.asyncio
async def test_valid_self_heal_ability_executes():
    engine, state, _ = _make_engine("fighter")
    state.local.hp = 10
    result = await engine.use_ability(0)
    assert result is True
    assert state.local.hp > 10


@pytest.mark.asyncio
async def test_cooldown_blocks_after_use():
    engine, state, _ = _make_engine("fighter")
    state.local.hp = 10
    first = await engine.use_ability(0)
    assert first is True
    second = await engine.use_ability(0)
    assert second is False