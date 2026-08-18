# -*- coding: utf-8 -*-
"""デジタル水槽で観察する名前付きNPCのライフサイクル。

集落人口の出生・背景死亡は ``institutions.population`` が整数会計する。
ここで扱うNPCは、その人口から顔なじみとして抽出された「名前付き観察対象」
であり、死亡しても集落人口をもう一度減らさない。これにより人口の二重計上を
避けつつ、関係相手にも誕生時点・年齢・自然死を持たせる。

寿命割当はworld seed・NPC identity・登場turnから局所的に決定し、ゲーム本体の
RNGを消費しない。同じ人物は保存・再開やプロセスをまたいでも同じ予定を持つ。
"""
from __future__ import annotations

import hashlib


NPC_INTRODUCTION_AGE_RANGE = (18, 70)
NPC_LIFESPAN_RANGE = (65, 95)
MONTHS_PER_YEAR = 12


def _stable_int(seed: int, identity: str, label: str) -> int:
    payload = f"{int(seed)}\0{identity}\0{label}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _bounded(stable_value: int, bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    if lo > hi:
        raise ValueError("range lower bound must not exceed upper bound")
    return lo + stable_value % (hi - lo + 1)


def npc_life_schedule(world_seed: int, identity: str,
                      introduced_turn: int) -> dict:
    """NPCの年齢と自然死予定を決定論的に割り当てる。

    ``birth_turn`` は世界開始より前なら負になりうる。``death_turn`` は必ず
    登場turnより後になる。年単位だけで全員が同じ月に死亡しないよう、誕生月と
    死亡月にも決定論的な0〜11か月のずれを持たせる。
    """
    identity = str(identity)
    introduced_turn = int(introduced_turn)
    age_years = _bounded(
        _stable_int(world_seed, identity, "introduction_age"),
        NPC_INTRODUCTION_AGE_RANGE)
    birth_offset = _stable_int(world_seed, identity, "birth_month") % MONTHS_PER_YEAR
    sampled_lifespan = _bounded(
        _stable_int(world_seed, identity, "lifespan"), NPC_LIFESPAN_RANGE)
    death_age_years = max(age_years + 1, sampled_lifespan)
    death_offset = _stable_int(world_seed, identity, "death_month") % MONTHS_PER_YEAR

    birth_turn = introduced_turn - age_years * MONTHS_PER_YEAR - birth_offset
    death_turn = birth_turn + death_age_years * MONTHS_PER_YEAR + death_offset
    death_turn = max(introduced_turn + 1, death_turn)
    return {
        "introduced_turn": introduced_turn,
        "birth_turn": birth_turn,
        "introduction_age_years": round(
            (introduced_turn - birth_turn) / MONTHS_PER_YEAR, 6),
        "death_age_years": round(
            (death_turn - birth_turn) / MONTHS_PER_YEAR, 6),
        "death_turn": death_turn,
        "died_turn": None,
        "alive": True,
    }


def npc_due_to_die(npc: dict, turn: int) -> bool:
    """生存中NPCの自然死予定が到来したか。入力は変更しない。"""
    death_turn = npc.get("death_turn")
    return bool(
        npc.get("alive", True)
        and death_turn is not None
        and int(turn) >= int(death_turn))


def mark_npc_died(npc: dict, turn: int, cause: str = "natural") -> dict:
    """NPC死亡後の新しいdictを返す。集落人口や契約には触れない。"""
    after = dict(npc)
    after.update({"alive": False, "died_turn": int(turn), "death_cause": cause})
    return after

