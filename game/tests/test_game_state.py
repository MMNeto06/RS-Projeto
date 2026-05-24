"""
test_game_state.py
Testes unitários ao GameState e LocalPlayer - lógica pura, sem rede.
 
Como correr
 - usar os scripts bash providenciados. com pwd da pasta game, correr
    - ./tests/scripts/run_unit_test.sh
 - setup manual
    - não precisa de nenhum servidor activo — corre completamente offline,
      sem gRPC nem Kademlia
    - a partir de game/, correr:
        uv run pytest tests/test_game_state.py
"""

import pytest
import time
from game_state import LocalPlayer, GameState
from dnd_classes import CLASSES


@pytest.fixture
def fighter():
    return LocalPlayer(username="alice", node_id="", dnd_class=CLASSES["fighter"])

def test_take_damage_clamps_to_zero(fighter):
    fighter.take_damage(9999)
    assert fighter.hp == 0
    assert fighter.alive is False
    assert fighter.deaths == 1

def test_heal_does_not_exceed_max(fighter):
    fighter.hp = 10
    fighter.heal(9999)
    assert fighter.hp == fighter.max_hp

def test_heal_does_nothing_when_dead(fighter):
    fighter.take_damage(9999)
    fighter.heal(50)
    assert fighter.hp == 0

def test_tick_effects_removes_expired_label(fighter):
    fighter.set_effect("mage_armor", duration_secs=-1, label="Mage Armor")
    assert "Mage Armor" in fighter.status_effects
    fighter.tick_effects()
    assert "Mage Armor" not in fighter.status_effects

def test_active_effect_not_removed_before_expiry(fighter):
    fighter.set_effect("mage_armor", duration_secs=60, label="Mage Armor")
    fighter.tick_effects()
    assert "Mage Armor" in fighter.status_effects

def test_cooldown_blocks_ability(fighter):
    fighter.use_ability("Second Wind", cooldown_secs=60)
    assert not fighter.is_ability_ready("Second Wind")

def test_cooldown_ready_after_expiry(fighter):
    fighter.use_ability("Second Wind", cooldown_secs=-1)
    assert fighter.is_ability_ready("Second Wind")

def test_reset_for_new_match_clears_everything(fighter):
    fighter.take_damage(20)
    fighter.kills = 5
    fighter.deaths = 3
    fighter.use_ability("Second Wind", cooldown_secs=60)
    fighter.set_effect("mage_armor", duration_secs=60, label="Mage Armor")
    fighter.reset_for_new_match(3, 4)

    assert fighter.hp == fighter.max_hp
    assert fighter.alive is True
    assert fighter.kills == 0
    assert fighter.deaths == 0
    assert fighter.x == 3 and fighter.y == 4
    assert not fighter.ability_cooldowns
    assert not fighter.active_effects
    assert not fighter.status_effects


def test_scoreboard_sorted_by_kills():
    local = LocalPlayer(username="alice", node_id="", dnd_class=CLASSES["fighter"])
    state = GameState(local)
    local.kills = 1

    from game_state import RemotePlayer
    state.others["bob"] = RemotePlayer(
        username="bob", node_id="", grpc_addr="",
        hp=60, max_hp=60, x=0, y=0, alive=True,
        class_name="fighter", armor_class=16, kills=5, deaths=1,
    )

    board = state.scoreboard()
    assert board[0][0] == "bob"
    assert board[1][0] == "alice"


def test_chebyshev_diagonal():
    from utils import chebyshev
    assert chebyshev(0, 0, 3, 4) == 4
    assert chebyshev(0, 0, 0, 0) == 0
    assert chebyshev(1, 1, 1, 1) == 0
    assert chebyshev(0, 0, 5, 0) == 5