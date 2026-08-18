# -*- coding: utf-8 -*-
"""デジタル水槽の名前付き住民・世帯台帳。

``institutions.population`` の整数人口を正本とし、通常規模ではその一人ずつに
永続ID・名前・誕生月・所属集落・世帯を割り当てる。大人口では名前付き住民を
上限付きの観察標本とし、残りを集落別の匿名cohortとして保持する。出生、死亡、
移住の人数は外から渡され、この層自身は人口を増減させない。名前と初期年齢は
world seedと通し番号から決定論的に作り、ゲーム本体のRNGを消費しない。

住民台帳はオフライン水槽だけの状態であり、銀行・通貨・信用・プレイヤーの行動選択の
計算には使わない。したがって観察粒度を変えてもpolicy-checkの数値世界や乱数列は
変わらない。名前付き人数と匿名cohortの合計は常に集落人口と一致させ、代表世帯と
住民に毎月の主な生業を割り当てる。
"""
from __future__ import annotations

import copy
import hashlib


RESIDENT_REGISTRY_VERSION = 4
NAMED_RESIDENT_LIMIT = 4096
MONTHS_PER_YEAR = 12
FOCUS_ENTRY_AGE_YEARS = 13
REPRODUCTIVE_AGE_RANGE = (18, 49)
INITIAL_HOUSEHOLD_TARGET_SIZE = 4
ACTIVITY_AGE_RANGE = (15, 74)
HOUSEHOLD_LIVELIHOODS = ("food", "medicine", "shelter", "tools")


def _empty_activity_counts() -> dict[str, int]:
    return {good: 0 for good in HOUSEHOLD_LIVELIHOODS}


def _upgrade_activity_account(row: dict, *, legacy: bool) -> None:
    """累計活動を財別内訳+未分類へ上げ、合計不変条件を検証する。"""
    raw = row.get("activity_counts_by_good") or {}
    if not isinstance(raw, dict):
        raise TypeError("activity_counts_by_good must be a dict")
    counts = {
        good: max(0, int(raw.get(good, 0)))
        for good in HOUSEHOLD_LIVELIHOODS}
    classified = sum(counts.values())
    total = max(0, int(row.get("activity_count", 0)))
    # version番号だけを旧値へ戻したcheckpointや、段階的deployの途中で新しい
    # 内訳だけが先に保存された台帳も捨てない。旧versionでは総数と内訳のどちらが
    # 新しいか断定できないため、観測済み内訳の合計を総数の下限にする。
    if legacy:
        recorded_unclassified = max(
            0, int(row.get("unclassified_activity_count", 0)))
        total = max(total, classified + recorded_unclassified)
    if classified > total:
        raise ValueError("per-good activity counts exceed activity total")
    if legacy or "unclassified_activity_count" not in row:
        unclassified = total - classified
    else:
        unclassified = max(0, int(row["unclassified_activity_count"]))
        if classified + unclassified != total:
            raise ValueError("activity count breakdown does not match total")
    row["activity_count"] = total
    row["activity_counts_by_good"] = counts
    row["unclassified_activity_count"] = unclassified


def record_activity_count(row: dict, good: str) -> None:
    """住民または世帯の累計活動を1回増やす。

    呼び出し側が作成中のcopyを直接更新する低水準関数。財別内訳と総数を同じ
    箇所で増やし、旧履歴の``unclassified``はそのまま残す。
    """
    if good not in HOUSEHOLD_LIVELIHOODS:
        raise ValueError(f"unsupported household activity: {good}")
    _upgrade_activity_account(row, legacy=False)
    row["activity_count"] += 1
    row["activity_counts_by_good"][good] += 1

FAMILY_NAMES = (
    "青木", "秋山", "浅野", "石井", "伊藤", "上田", "遠藤", "大野",
    "岡田", "小川", "加藤", "川口", "木村", "久保", "小林", "斎藤",
    "坂本", "佐々木", "佐藤", "清水", "鈴木", "高木", "高橋", "田中",
    "中村", "西村", "橋本", "林", "藤井", "前田", "松本", "森",
    "山口", "山田", "吉田", "渡辺",
)

GIVEN_NAMES = (
    "葵", "明", "碧", "歩", "泉", "樹", "海", "楓", "薫", "奏",
    "恵", "蛍", "咲", "司", "忍", "純", "澄", "空", "環", "翼",
    "直", "凪", "望", "光", "響", "文", "真琴", "岬", "瑞希", "実",
    "結", "優", "悠", "陽", "蓮", "和", "千尋", "春", "冬", "涼",
)


def _stable_int(seed: int, *parts: object) -> int:
    payload = "\0".join([str(int(seed)), *(str(part) for part in parts)])
    return int.from_bytes(
        hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")


def resident_age_years(resident: dict, turn: int) -> float:
    """指定月の年齢。誕生前は0歳として表示する。"""
    return round(max(0, int(turn) - int(resident["birth_turn"]))
                 / MONTHS_PER_YEAR, 6)


def _livelihood_for_household(world_seed: int, household_id: str) -> str:
    index = _stable_int(
        world_seed, household_id, "livelihood") % len(HOUSEHOLD_LIVELIHOODS)
    return HOUSEHOLD_LIVELIHOODS[index]


def _new_registry(world_seed: int, registered_turn: int,
                  legacy_snapshot: bool) -> dict:
    return {
        "version": RESIDENT_REGISTRY_VERSION,
        "world_seed": int(world_seed),
        "registered_turn": int(registered_turn),
        "legacy_snapshot": bool(legacy_snapshot),
        "residents": {},
        "households": {},
        "resident_sequence": 0,
        "household_sequence": 0,
        "births_total": 0,
        "deaths_total": 0,
        "migrations_total": 0,
        "archived_resident_count": 0,
        "archived_household_count": 0,
        "cohort_mode": False,
        "named_resident_limit": NAMED_RESIDENT_LIMIT,
        "anonymous_population_by_settlement": {},
    }


def _bounded_population_sample(populations: dict[str, int],
                               limit: int) -> dict[str, int]:
    """各有人集落を最低1人で表現しつつ、世界全体の名前付き上限へ配賦する。"""
    positive = {
        str(key): max(0, int(value))
        for key, value in populations.items() if int(value) > 0}
    total = sum(positive.values())
    if total <= limit:
        return positive
    keys = sorted(positive)
    sample_total = min(total, max(int(limit), len(keys)))
    allocations = {key: 1 for key in keys}
    remaining = sample_total - len(keys)
    capacities = {key: positive[key] - 1 for key in keys}
    capacity_total = sum(capacities.values())
    exact = {
        key: (capacities[key] * remaining / capacity_total
              if capacity_total else 0.0)
        for key in keys}
    for key in keys:
        add = min(capacities[key], int(exact[key]))
        allocations[key] += add
        remaining -= add
    for key in sorted(keys, key=lambda item: (
            -(exact[item] - int(exact[item])), item)):
        if not remaining:
            break
        if allocations[key] < positive[key]:
            allocations[key] += 1
            remaining -= 1
    if remaining or sum(allocations.values()) != sample_total:
        raise RuntimeError("named resident sample allocation failed")
    return allocations


def _next_household(registry: dict, settlement_id: str, turn: int, *,
                    family_name: str | None = None,
                    origin: str = "initial") -> dict:
    registry["household_sequence"] += 1
    sequence = registry["household_sequence"]
    household_id = f"h{sequence:06d}"
    if family_name is None:
        index = _stable_int(
            registry["world_seed"], settlement_id, household_id,
            "family_name") % len(FAMILY_NAMES)
        family_name = FAMILY_NAMES[index]
    household = {
        "id": household_id,
        "name": f"{family_name}家",
        "family_name": family_name,
        "settlement_id": str(settlement_id),
        "founded_turn": int(turn),
        "closed_turn": None,
        "active": True,
        "origin": origin,
        "next_given_index": 0,
        "livelihood": _livelihood_for_household(
            registry["world_seed"], household_id),
        "activity_count": 0,
        "activity_counts_by_good": _empty_activity_counts(),
        "unclassified_activity_count": 0,
        "last_activity_turn": None,
        "last_activity": None,
        "last_actor_id": None,
    }
    registry["households"][household_id] = household
    return household


def _next_resident(registry: dict, settlement_id: str, household: dict,
                   birth_turn: int, turn: int, *, origin: str,
                   parent_ids: list[str] | None = None,
                   generation: int = 0) -> dict:
    registry["resident_sequence"] += 1
    sequence = registry["resident_sequence"]
    resident_id = f"r{sequence:06d}"
    ordinal = int(household.get("next_given_index", 0))
    household["next_given_index"] = ordinal + 1
    base = _stable_int(
        registry["world_seed"], household["id"], "given_name")
    given_name = GIVEN_NAMES[(base + ordinal) % len(GIVEN_NAMES)]
    if ordinal >= len(GIVEN_NAMES):
        given_name = f"{given_name}{ordinal // len(GIVEN_NAMES) + 1}"
    resident = {
        "id": resident_id,
        "name": f"{household['family_name']} {given_name}",
        "family_name": household["family_name"],
        "given_name": given_name,
        "settlement_id": str(settlement_id),
        "household_id": household["id"],
        "birth_turn": int(birth_turn),
        "registered_turn": int(turn),
        "alive": True,
        "died_turn": None,
        "death_cause": None,
        "origin": origin,
        "parent_ids": list(parent_ids or ()),
        "generation": int(generation),
        "focus_count": 0,
        "focus_since_turn": None,
        "focus_generation": None,
        "talent": None,
        "activity_count": 0,
        "activity_counts_by_good": _empty_activity_counts(),
        "unclassified_activity_count": 0,
        "last_activity_turn": None,
        "last_activity": None,
    }
    registry["residents"][resident_id] = resident
    return resident


def initial_resident_registry(settlements: dict, world_seed: int,
                              turn: int = 1, *,
                              legacy_snapshot: bool = False,
                              named_resident_limit: int =
                              NAMED_RESIDENT_LIMIT) -> dict:
    """現在の集落人口を、名前付き標本と匿名cohortへ決定論的に展開する。

    新規世界では ``turn=1``。旧checkpoint移行時は現在月を渡し、その時点で匿名
    人口へ名前を付与したことを ``legacy_snapshot`` に記録する。上限以下なら従来
    どおり全員を名前付きにする。いずれも集落dictを変更せず、名前付き人数と匿名
    cohortの合計は各 ``settlement.population`` と正確に一致する。
    """
    if int(named_resident_limit) <= 0:
        raise ValueError("named_resident_limit must be positive")
    registry = _new_registry(world_seed, turn, legacy_snapshot)
    registry["named_resident_limit"] = int(named_resident_limit)
    registry["births_total"] = sum(
        int(row.get("births_total", 0)) for row in settlements.values())
    registry["deaths_total"] = sum(
        int(row.get("deaths_total", 0)) for row in settlements.values())

    populations = {
        str(settlement_id): max(0, int(row.get("population", 0)))
        for settlement_id, row in settlements.items()}
    named_targets = _bounded_population_sample(
        populations, int(named_resident_limit))
    registry["cohort_mode"] = sum(populations.values()) > sum(
        named_targets.values())

    for settlement_id, settlement in settlements.items():
        settlement_id = str(settlement_id)
        population = max(0, int(settlement.get("population", 0)))
        reproductive = max(0, min(
            population, int(settlement.get("reproductive_population", 0))))
        if population == 0:
            continue
        named_population = named_targets[settlement_id]
        if registry["cohort_mode"]:
            represented_reproductive = min(
                named_population,
                reproductive * named_population // population)
            # 依存人口がいる集落では、焦点人物候補となる13歳を1人残す。
            if population > reproductive and named_population > 0:
                represented_reproductive = min(
                    represented_reproductive, named_population - 1)
            if reproductive > 0 and represented_reproductive == 0 \
                    and named_population > 1:
                represented_reproductive = 1
        else:
            represented_reproductive = reproductive
        household_count = max(
            1, (named_population + INITIAL_HOUSEHOLD_TARGET_SIZE - 1)
            // INITIAL_HOUSEHOLD_TARGET_SIZE)
        households = [
            _next_household(
                registry, settlement_id, turn,
                origin="legacy_snapshot" if legacy_snapshot else "initial")
            for _ in range(household_count)
        ]
        dependent_index = 0
        for index in range(named_population):
            household = households[index % household_count]
            if index < represented_reproductive:
                age_years = 18 + _stable_int(
                    world_seed, settlement_id, index,
                    "initial_reproductive_age") % 28
                age_month = _stable_int(
                    world_seed, settlement_id, index,
                    "initial_birth_month") % MONTHS_PER_YEAR
            else:
                # 各集落に必ず13歳を一人置き、既存の焦点人物開始年齢と台帳を
                # 一致させる。残りは子どもと高齢者を交互に配置する。
                if dependent_index == 0:
                    age_years, age_month = FOCUS_ENTRY_AGE_YEARS, 0
                elif dependent_index % 2:
                    age_years = _stable_int(
                        world_seed, settlement_id, index,
                        "initial_child_age") % 18
                    age_month = _stable_int(
                        world_seed, settlement_id, index,
                        "initial_child_month") % MONTHS_PER_YEAR
                else:
                    age_years = 50 + _stable_int(
                        world_seed, settlement_id, index,
                        "initial_elder_age") % 36
                    age_month = _stable_int(
                        world_seed, settlement_id, index,
                        "initial_elder_month") % MONTHS_PER_YEAR
                dependent_index += 1
            _next_resident(
                registry, settlement_id, household,
                int(turn) - age_years * MONTHS_PER_YEAR - age_month,
                turn,
                origin="legacy_snapshot" if legacy_snapshot else "initial")
        anonymous = population - named_population
        if anonymous:
            registry["anonymous_population_by_settlement"][
                settlement_id] = anonymous
    return registry


def upgrade_resident_registry(registry: dict) -> dict:
    """version 1の台帳へ生業・行動欄を追加する。

    住民ID・名前・世帯・集落・累計値は変更しない。既存の水槽は同じ人々の
    まま次の月から生業記録を開始できる。
    """
    if not isinstance(registry, dict):
        raise TypeError("resident registry must be a dict")
    version = int(registry.get("version", 0))
    if version < 1:
        raise ValueError("resident registry cannot be upgraded from version 0")
    if version > RESIDENT_REGISTRY_VERSION:
        raise ValueError("resident registry version is newer than this runtime")
    after = copy.deepcopy(registry)
    legacy_activity_breakdown = version < RESIDENT_REGISTRY_VERSION
    world_seed = int(after.get("world_seed", 0))
    for household_id, household in after.get("households", {}).items():
        household.setdefault(
            "livelihood", _livelihood_for_household(world_seed, household_id))
        household.setdefault("activity_count", 0)
        _upgrade_activity_account(
            household, legacy=legacy_activity_breakdown)
        household.setdefault("last_activity_turn", None)
        household.setdefault("last_activity", None)
        household.setdefault("last_actor_id", None)
    for resident in after.get("residents", {}).values():
        resident.setdefault("activity_count", 0)
        _upgrade_activity_account(
            resident, legacy=legacy_activity_breakdown)
        resident.setdefault("last_activity_turn", None)
        resident.setdefault("last_activity", None)
    after.setdefault("cohort_mode", False)
    after.setdefault("named_resident_limit", NAMED_RESIDENT_LIMIT)
    after.setdefault("anonymous_population_by_settlement", {})
    after["anonymous_population_by_settlement"] = {
        str(key): max(0, int(value))
        for key, value in after["anonymous_population_by_settlement"].items()
        if int(value) > 0}
    after["version"] = RESIDENT_REGISTRY_VERSION
    return after


def living_residents(registry: dict, settlement_id: str | None = None) -> list:
    residents = [
        resident for resident in registry.get("residents", {}).values()
        if resident.get("alive", True)
        and (settlement_id is None
             or resident.get("settlement_id") == settlement_id)
    ]
    return sorted(residents, key=lambda resident: resident["id"])


def named_living_counts_by_settlement(registry: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for resident in living_residents(registry):
        settlement_id = str(resident["settlement_id"])
        counts[settlement_id] = counts.get(settlement_id, 0) + 1
    return counts


def anonymous_population_by_settlement(registry: dict) -> dict[str, int]:
    return {
        str(key): max(0, int(value))
        for key, value in registry.get(
            "anonymous_population_by_settlement", {}).items()
        if int(value) > 0}


def anonymous_population_count(registry: dict,
                               settlement_id: str | None = None) -> int:
    rows = anonymous_population_by_settlement(registry)
    if settlement_id is not None:
        return rows.get(str(settlement_id), 0)
    return sum(rows.values())


def living_counts_by_settlement(registry: dict) -> dict:
    counts = anonymous_population_by_settlement(registry)
    for settlement_id, count in named_living_counts_by_settlement(
            registry).items():
        counts[settlement_id] = counts.get(settlement_id, 0) + count
    return counts


def living_population_count(registry: dict,
                            settlement_id: str | None = None) -> int:
    counts = living_counts_by_settlement(registry)
    if settlement_id is not None:
        return counts.get(str(settlement_id), 0)
    return sum(counts.values())


def _change_anonymous_population(registry: dict, settlement_id: str,
                                 delta: int) -> None:
    settlement_id = str(settlement_id)
    rows = registry.setdefault("anonymous_population_by_settlement", {})
    after = int(rows.get(settlement_id, 0)) + int(delta)
    if after < 0:
        raise ValueError("anonymous population cannot become negative")
    if after:
        rows[settlement_id] = after
    else:
        rows.pop(settlement_id, None)


def active_household_count(registry: dict) -> int:
    if registry.get("cohort_mode", False):
        populations = living_counts_by_settlement(registry)
        return sum(
            1 for household in registry.get("households", {}).values()
            if household.get("active", True)
            and populations.get(str(household.get("settlement_id")), 0) > 0)
    living_households = {
        resident["household_id"] for resident in living_residents(registry)}
    return len(living_households)


def _apply_household_activity(after: dict, settlement_id: str, turn: int,
                              preferred_good: str | None) -> dict | None:
    settlement_id = str(settlement_id)
    living = living_residents(after, settlement_id)
    if not living:
        return None
    members_by_household = {}
    for resident in living:
        members_by_household.setdefault(
            resident["household_id"], []).append(resident)
    households = [
        after["households"][household_id]
        for household_id in members_by_household
        if household_id in after.get("households", {})]
    if not households:
        return None

    preferred = (preferred_good if preferred_good in HOUSEHOLD_LIVELIHOODS
                 else None)
    specialist = [
        household for household in households
        if household.get("livelihood") == preferred]
    candidates = specialist or households
    household = min(candidates, key=lambda row: (
        int(row.get("activity_count", 0)),
        row.get("last_activity_turn") is not None,
        int(row.get("last_activity_turn") or -1), row["id"]))
    action = preferred or household["livelihood"]
    lo, hi = ACTIVITY_AGE_RANGE
    possible_actors = members_by_household[household["id"]]
    working_age = [
        resident for resident in possible_actors
        if lo <= resident_age_years(resident, turn) <= hi]
    actor = min(working_age or possible_actors, key=lambda row: (
        int(row.get("activity_count", 0)), row["id"]))

    record_activity_count(household, action)
    household["last_activity_turn"] = int(turn)
    household["last_activity"] = action
    household["last_actor_id"] = actor["id"]
    record_activity_count(actor, action)
    actor["last_activity_turn"] = int(turn)
    actor["last_activity"] = action
    return {
        "turn": int(turn), "kind": "household_activity",
        "settlement_id": settlement_id,
        "household_id": household["id"],
        "household_name": household["name"],
        "livelihood": household["livelihood"],
        "activity": action,
        "resident_id": actor["id"], "resident_name": actor["name"],
        "resident_age": resident_age_years(actor, turn),
        "responding_to_shortage": preferred is not None,
    }


def plan_household_activity(registry: dict, settlement_id: str, turn: int,
                            preferred_good: str | None = None) -> dict:
    """集落の今月の主な生業を1世帯・1住民へ割り当てる。

    財の増減自体は既存の背景生産会計が正本で、ここで追加生産しない。
    ``preferred_good``に応じた専門世帯を優先し、同条件では行動回数の
    少ない世帯・住民へ順に回す。乱数は使わない。
    """
    after = upgrade_resident_registry(registry)
    return {
        "registry": after,
        "event": _apply_household_activity(
            after, settlement_id, turn, preferred_good),
    }


def plan_household_activities(registry: dict, turn: int,
                              preferred_goods: dict) -> dict:
    """複数集落の生業を1回のcopyで計画する。"""
    after = upgrade_resident_registry(registry)
    events = []
    for settlement_id, preferred_good in preferred_goods.items():
        event = _apply_household_activity(
            after, settlement_id, turn, preferred_good)
        if event is not None:
            events.append(event)
    return {"registry": after, "events": events}


def select_focus_resident(registry: dict, settlement_id: str, turn: int,
                          exclude_id: str | None = None) -> str | None:
    """焦点人物を選ぶ。13歳に最も近い生存者、同差ならID順。"""
    candidates = [
        resident for resident in living_residents(registry, settlement_id)
        if resident["id"] != exclude_id]
    if not candidates:
        return None
    selected = min(candidates, key=lambda resident: (
        abs(resident_age_years(resident, turn) - FOCUS_ENTRY_AGE_YEARS),
        resident["id"]))
    return selected["id"]


def materialize_cohort_resident(registry: dict, settlement_id: str,
                                turn: int) -> dict:
    """匿名cohortの既存1人へIDを与え、焦点選択可能にする。

    人口は増やさない。出生月は13歳相当だが、観察上の登場境界は
    ``registered_turn``で別に保持する。
    """
    settlement_id = str(settlement_id)
    if anonymous_population_count(registry, settlement_id) <= 0:
        raise ValueError("no anonymous resident is available to materialize")
    after = copy.deepcopy(registry)
    households = sorted((
        row for row in after.get("households", {}).values()
        if row.get("active", True)
        and str(row.get("settlement_id")) == settlement_id),
        key=lambda row: (int(row.get("activity_count", 0)), row["id"]))
    household = (households[0] if households else _next_household(
        after, settlement_id, turn, origin="cohort_materialized"))
    _change_anonymous_population(after, settlement_id, -1)
    resident = _next_resident(
        after, settlement_id, household,
        int(turn) - FOCUS_ENTRY_AGE_YEARS * MONTHS_PER_YEAR,
        int(turn), origin="cohort_materialized")
    return {"registry": after, "resident_id": resident["id"]}


def assign_focus(registry: dict, resident_id: str, turn: int,
                 generation: int, talent: str) -> dict:
    """既存住民を焦点人物として記録した新しいregistryを返す。"""
    after = copy.deepcopy(registry)
    resident = after["residents"].get(resident_id)
    if resident is None or not resident.get("alive", True):
        raise ValueError("focus resident must exist and be alive")
    resident["focus_count"] = int(resident.get("focus_count", 0)) + 1
    resident["focus_since_turn"] = int(turn)
    resident["focus_generation"] = int(generation)
    resident["talent"] = talent
    return after


def _living_in_household(registry: dict, household_id: str) -> list:
    return [
        resident for resident in living_residents(registry)
        if resident["household_id"] == household_id]


def _close_empty_households(registry: dict, turn: int) -> list[dict]:
    living_ids = {
        resident["household_id"] for resident in living_residents(registry)}
    events = []
    for household in registry.get("households", {}).values():
        represented_cohort = (
            registry.get("cohort_mode", False)
            and anonymous_population_count(
                registry, household.get("settlement_id")) > 0)
        if (household["id"] in living_ids or represented_cohort
                or not household.get("active", True)):
            continue
        household["active"] = False
        household["closed_turn"] = int(turn)
        events.append({
            "turn": int(turn), "kind": "household_closed",
            "household_id": household["id"], "name": household["name"],
            "settlement_id": household["settlement_id"],
        })
    return events


def _birth_household(registry: dict, settlement_id: str, turn: int) -> tuple[dict, list]:
    candidates = []
    lo, hi = REPRODUCTIVE_AGE_RANGE
    for household in registry.get("households", {}).values():
        if (not household.get("active", True)
                or household.get("settlement_id") != settlement_id):
            continue
        members = _living_in_household(registry, household["id"])
        adults = [
            resident for resident in members
            if lo <= resident_age_years(resident, turn) <= hi]
        if adults:
            candidates.append((len(members), household["id"], household, adults))
    if candidates:
        _, _, household, adults = min(candidates)
        parents = [resident["id"] for resident in adults[:2]]
        return household, parents
    if registry.get("cohort_mode", False):
        representatives = sorted((
            household for household in registry.get("households", {}).values()
            if household.get("active", True)
            and household.get("settlement_id") == settlement_id),
            key=lambda row: (int(row.get("activity_count", 0)), row["id"]))
        if representatives:
            return representatives[0], []
    household = _next_household(
        registry, settlement_id, turn, origin="birth_without_household")
    return household, []


def apply_population_change(registry: dict, settlement_id: str,
                            births: int, deaths: int, turn: int, *,
                            protected_resident_ids: tuple[str, ...] = (),
                            death_cause: str = "background") -> dict:
    """集計済み出生・死亡を個人台帳へ反映する。入力registryは変更しない。"""
    births, deaths = int(births), int(deaths)
    if births < 0 or deaths < 0:
        raise ValueError("births and deaths must be non-negative")
    after = copy.deepcopy(registry)
    events = []

    if after.get("cohort_mode", False):
        named_slots = max(
            0, int(after.get("named_resident_limit", NAMED_RESIDENT_LIMIT))
            - len(living_residents(after)))
        named_births = min(births, named_slots)
    else:
        named_births = births
    for _ in range(named_births):
        household, parent_ids = _birth_household(
            after, str(settlement_id), int(turn))
        parent_generations = [
            after["residents"][resident_id].get("generation", 0)
            for resident_id in parent_ids]
        resident = _next_resident(
            after, str(settlement_id), household, int(turn), int(turn),
            origin="birth", parent_ids=parent_ids,
            generation=(max(parent_generations) + 1
                        if parent_generations else 0))
        events.append({
            "turn": int(turn), "kind": "resident_born",
            "resident_id": resident["id"], "name": resident["name"],
            "settlement_id": str(settlement_id),
            "household_id": household["id"],
            "household_name": household["name"],
            "parent_ids": list(parent_ids),
        })
    anonymous_births = births - named_births
    if anonymous_births:
        _change_anonymous_population(
            after, str(settlement_id), anonymous_births)
    after["births_total"] = int(after.get("births_total", 0)) + births

    protected = set(protected_resident_ids)
    anonymous_deaths = (min(
        deaths, anonymous_population_count(after, str(settlement_id)))
        if after.get("cohort_mode", False) else 0)
    if anonymous_deaths:
        _change_anonymous_population(
            after, str(settlement_id), -anonymous_deaths)
    named_deaths = deaths - anonymous_deaths
    candidates = living_residents(after, str(settlement_id))
    candidates.sort(key=lambda resident: (
        resident["id"] in protected,
        -resident_age_years(resident, turn), resident["id"]))
    if named_deaths > len(candidates):
        raise ValueError("resident deaths exceed living settlement population")
    for resident in candidates[:named_deaths]:
        resident["alive"] = False
        resident["died_turn"] = int(turn)
        resident["death_cause"] = death_cause
        events.append({
            "turn": int(turn), "kind": "resident_died",
            "resident_id": resident["id"], "name": resident["name"],
            "settlement_id": str(settlement_id),
            "household_id": resident["household_id"],
            "age": resident_age_years(resident, turn),
            "cause": death_cause,
        })
    after["deaths_total"] = int(after.get("deaths_total", 0)) + deaths
    events.extend(_close_empty_households(after, int(turn)))
    return {"registry": after, "events": events}


def mark_resident_died(registry: dict, resident_id: str, turn: int,
                       cause: str) -> dict:
    """指定した生存住民一人を死亡させる。人口集計の減算は呼び出し側の責務。"""
    resident = registry.get("residents", {}).get(resident_id)
    if resident is None or not resident.get("alive", True):
        return {"registry": copy.deepcopy(registry), "events": []}
    after = copy.deepcopy(registry)
    target = after["residents"][resident_id]
    target["alive"] = False
    target["died_turn"] = int(turn)
    target["death_cause"] = cause
    after["deaths_total"] = int(after.get("deaths_total", 0)) + 1
    events = [{
        "turn": int(turn), "kind": "resident_died",
        "resident_id": target["id"], "name": target["name"],
        "settlement_id": target["settlement_id"],
        "household_id": target["household_id"],
        "age": resident_age_years(target, turn), "cause": cause,
    }]
    events.extend(_close_empty_households(after, int(turn)))
    return {"registry": after, "events": events}


def apply_migration(registry: dict, migration_event: dict, turn: int, *,
                    protected_resident_ids: tuple[str, ...] = ()) -> dict:
    """人口移住イベントを同じ人数の住民移動へ展開する。"""
    after = copy.deepcopy(registry)
    source_id = str(migration_event["from_settlement"])
    destination_id = str(migration_event["to_settlement"])
    migrants = int(migration_event["migrants"])
    reproductive = max(0, min(
        migrants, int(migration_event.get("reproductive_migrants", 0))))
    if migrants < 0:
        raise ValueError("migrants must be non-negative")
    anonymous_migrants = (min(
        migrants, anonymous_population_count(after, source_id))
        if after.get("cohort_mode", False) else 0)
    if anonymous_migrants:
        _change_anonymous_population(
            after, source_id, -anonymous_migrants)
        _change_anonymous_population(
            after, destination_id, anonymous_migrants)
    named_migrants = migrants - anonymous_migrants
    named_reproductive = max(
        0, min(named_migrants, reproductive - anonymous_migrants))
    protected = set(protected_resident_ids)
    candidates = living_residents(after, source_id)
    lo, hi = REPRODUCTIVE_AGE_RANGE
    reproductive_candidates = [
        resident for resident in candidates
        if lo <= resident_age_years(resident, turn) <= hi]
    other_candidates = [
        resident for resident in candidates
        if resident not in reproductive_candidates]
    sort_key = lambda resident: (
        resident["household_id"], resident["id"])
    reproductive_candidates.sort(key=sort_key)
    other_candidates.sort(key=sort_key)
    safe_reproductive = [
        resident for resident in reproductive_candidates
        if resident["id"] not in protected]
    safe_other = [
        resident for resident in other_candidates
        if resident["id"] not in protected]
    protected_candidates = [
        resident for resident in reproductive_candidates + other_candidates
        if resident["id"] in protected]
    selected = safe_reproductive[:named_reproductive]
    selected_ids = {resident["id"] for resident in selected}
    remaining = [
        resident for resident in safe_reproductive + safe_other
        if resident["id"] not in selected_ids]
    remaining.sort(key=sort_key)
    selected.extend(remaining[:max(0, named_migrants - len(selected))])
    if len(selected) < named_migrants:
        protected_candidates.sort(key=sort_key)
        selected.extend(
            protected_candidates[:named_migrants - len(selected)])
    if len(selected) != named_migrants:
        raise ValueError("migrants exceed living source population")

    by_household = {}
    for resident in selected:
        by_household.setdefault(resident["household_id"], []).append(resident)
    moved_rows = []
    household_events = []
    for household_id, members in sorted(by_household.items()):
        household = after["households"][household_id]
        living_before = _living_in_household(after, household_id)
        selected_member_ids = {resident["id"] for resident in members}
        if selected_member_ids == {resident["id"] for resident in living_before}:
            destination_household = household
            household["settlement_id"] = destination_id
        else:
            destination_household = _next_household(
                after, destination_id, turn,
                family_name=household["family_name"], origin="migration_split")
            household_events.append({
                "turn": int(turn), "kind": "household_split",
                "from_household_id": household_id,
                "household_id": destination_household["id"],
                "name": destination_household["name"],
                "from_settlement": source_id,
                "settlement_id": destination_id,
            })
        for resident in members:
            resident["settlement_id"] = destination_id
            resident["household_id"] = destination_household["id"]
            moved_rows.append({
                "resident_id": resident["id"], "name": resident["name"],
                "household_id": destination_household["id"],
            })
    after["migrations_total"] = int(after.get("migrations_total", 0)) + migrants
    events = household_events
    if migrants:
        events.append({
            "turn": int(turn), "kind": "residents_migrated",
            "from_settlement": source_id, "to_settlement": destination_id,
            "migrants": migrants, "residents": moved_rows,
        })
    return {"registry": after, "events": events}


def compact_registry(registry: dict, cutoff_turn: int) -> dict:
    """将来計算に不要な古い死者・閉鎖世帯を観察窓から落とす。

    生存者、通し番号、累積値は保持する。出生・移住・焦点選択は生存者だけを参照
    するため、圧縮前後で将来の個人台帳は一致する。
    """
    after = copy.deepcopy(registry)
    cutoff_turn = int(cutoff_turn)
    before_residents = len(after.get("residents", {}))
    after["residents"] = {
        resident_id: resident
        for resident_id, resident in after.get("residents", {}).items()
        if resident.get("alive", True)
        or (resident.get("died_turn") is not None
            and int(resident["died_turn"]) >= cutoff_turn)
    }
    referenced_households = {
        resident["household_id"] for resident in after["residents"].values()}
    before_households = len(after.get("households", {}))
    after["households"] = {
        household_id: household
        for household_id, household in after.get("households", {}).items()
        if household_id in referenced_households
        or household.get("active", True)
    }
    after["archived_resident_count"] = (
        int(after.get("archived_resident_count", 0))
        + before_residents - len(after["residents"]))
    after["archived_household_count"] = (
        int(after.get("archived_household_count", 0))
        + before_households - len(after["households"]))
    return after


def registry_matches_settlements(registry: dict, settlements: dict) -> bool:
    """生存住民数と集落人口が全IDで一致するか。診断・テスト用。"""
    counts = living_counts_by_settlement(registry)
    return all(
        counts.get(settlement_id, 0) == int(row.get("population", 0))
        for settlement_id, row in settlements.items()) and all(
            settlement_id in settlements for settlement_id in counts)
