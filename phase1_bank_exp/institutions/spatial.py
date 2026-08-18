# -*- coding: utf-8 -*-
"""デジタル水槽の永続空間状態。

座標は画面pixelではなく0..1の正規化された世界座標で保存する。活動場所が先に
存在し、住民は世帯の住まいと活動場所へ結び付く。表示上の集落は活動場所の近接
連結成分から毎回導出し、矩形領域やsettlement IDを空間の原因にはしない。

既存のsettlement IDは当面、財・人口会計との移行用``account_id``としてだけ保持
する。初期配置・活動クラスタの形成には使わない。すべての配置はstable hashで
決まり、randomモジュールもゲーム本体のRNGも消費しない。
"""
from __future__ import annotations

import copy
import hashlib
import math


SPATIAL_STATE_VERSION = 6
CLUSTERED_SPATIAL_STATE_VERSION = 2
PIONEERING_SPATIAL_STATE_VERSION = 3
ACTIVITY_EPISODE_SPATIAL_STATE_VERSION = 4
SITE_ACTIVITY_SPATIAL_STATE_VERSION = 5
SPATIAL_KEYFRAME_VERSION = 7
SPATIAL_KEYFRAME_INTERVAL = 12
SPATIAL_CLUSTER_DISTANCE = 0.075
SITE_ATTACH_DISTANCE_RANGE = (0.018, 0.045)
SITE_MOVE_RATE = 0.18
RESIDENT_MOVE_RATE = 0.45
WORLD_MARGIN = 0.04
LIVELIHOODS = ("food", "medicine", "shelter", "tools")
ACTIVITY_MODE_HOME = "home"
ACTIVITY_MODE_ROUTINE = "routine"
ACTIVITY_MODE_PRIMARY = "primary"

# 活動場所が十分に集まった共同体から、実際にその月の生業を担った世帯が
# 新しい活動地へ移る「開拓」。settlement/account IDを原因にせず、前月までの
# 活動クラスタと住民台帳上の行動だけから決める。全てstable hash由来なので
# ゲーム本体のRNG・方針選択・数値人口を消費しない。
PIONEERING_FIRST_TURN = 24
PIONEERING_COOLDOWN_TURNS = 240
PIONEERING_MIN_CLUSTER_AGE = 12
PIONEERING_MIN_CLUSTER_SITES = 6
PIONEERING_MIN_CLUSTER_POPULATION = 24
PIONEERING_MIN_SITE_POPULATION = 2
PIONEERING_MAX_ACTIVE_CLUSTERS = 16
PIONEERING_COMMUNITY_SITE_THRESHOLD = 3
PIONEERING_COMMUNITY_POPULATION_THRESHOLD = 8
PIONEERING_TARGET_RADII = (0.14, 0.18, 0.22, 0.26)


def _stable_int(*parts) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _unit(*parts) -> float:
    return _stable_int(*parts) / float(2**64)


def _bounded(value: float) -> float:
    return round(max(WORLD_MARGIN, min(1.0 - WORLD_MARGIN, value)), 9)


def _offset(point: tuple[float, float], identity: str,
            distance_range=SITE_ATTACH_DISTANCE_RANGE) -> tuple[float, float]:
    angle = _unit(identity, "angle") * math.tau
    low, high = distance_range
    distance = low + _unit(identity, "distance") * (high - low)
    return (
        _bounded(point[0] + math.cos(angle) * distance),
        _bounded(point[1] + math.sin(angle) * distance),
    )


def _active_households(registry: dict) -> list[dict]:
    living = [
        resident for resident in registry.get("residents", {}).values()
        if resident.get("alive", True)]
    living_households = {resident["household_id"] for resident in living}
    if registry.get("cohort_mode", False):
        account_populations = {
            str(key): max(0, int(value))
            for key, value in registry.get(
                "anonymous_population_by_settlement", {}).items()}
        for resident in living:
            account = str(resident.get("settlement_id"))
            account_populations[account] = account_populations.get(
                account, 0) + 1
        living_households.update(
            household["id"]
            for household in registry.get("households", {}).values()
            if household.get("active", True)
            and account_populations.get(
                str(household.get("settlement_id")), 0) > 0)
    return sorted((
        household for household_id, household
        in registry.get("households", {}).items()
        if household_id in living_households
    ), key=lambda row: (int(row.get("founded_turn", 0)), row["id"]))


def _site_record(household: dict, turn: int, x: float, y: float, *,
                 parent_site_id: str | None, root_site_id: str) -> dict:
    home_x, home_y = _offset((x, y), f"{household['id']}:home", (0.008, 0.022))
    return {
        "id": f"site:{household['id']}",
        "household_id": household["id"],
        "livelihood": household.get("livelihood") or
            LIVELIHOODS[_stable_int(household["id"], "livelihood") % len(LIVELIHOODS)],
        "x": round(x, 9), "y": round(y, 9),
        "home_x": home_x, "home_y": home_y,
        "target_x": round(x, 9), "target_y": round(y, 9),
        "parent_site_id": parent_site_id,
        "root_site_id": root_site_id,
        "account_id": household.get("settlement_id"),
        "founded_turn": int(household.get("founded_turn", turn)),
        "updated_turn": int(turn),
        "named_resident_count": 0, "population_weight": 0,
        "last_activity_turn": None,
        "activity_worker_count": 0,
        "activity_named_worker_count": 0,
        "activity_anonymous_worker_count": 0,
        "activity_worker_count_by_good": {},
        "activity_output_by_good": {},
        "active": True, "closed_turn": None,
    }


def _bootstrap_sites(registry: dict, turn: int) -> dict:
    households = _active_households(registry)
    if not households:
        return {}
    # 人数ではなく活動場所数から核の数を決める。settlement数は参照しない。
    nucleus_count = max(1, min(8, round(math.sqrt(len(households) / 8))))
    sites = {}
    roots = []
    for index, household in enumerate(households):
        site_id = f"site:{household['id']}"
        if index < nucleus_count:
            if nucleus_count == 1:
                x, y = 0.5, 0.5
            else:
                angle = (math.tau * index / nucleus_count
                         + _unit(registry.get("world_seed", 0), "spatial_rotation") * 0.6)
                x = _bounded(0.5 + math.cos(angle) * 0.27)
                y = _bounded(0.5 + math.sin(angle) * 0.27)
            root_site_id = site_id
            roots.append(root_site_id)
            site = _site_record(
                household, turn, x, y, parent_site_id=None,
                root_site_id=root_site_id)
        else:
            root_site_id = roots[
                _stable_int(household["id"], "nucleus") % len(roots)]
            root = sites[root_site_id]
            # 初期の名前付き世帯は人口全体の観察標本であり、標本点を親から
            # 親へ再帰的に延ばすと、標本数だけで活動域が広がって別の核まで
            # 単連結してしまう。初期配置だけは各活動核へ直接付け、標本密度を
            # 地理的な拡大へ変換しない。誕生後の新世帯・移住・開拓による実際
            # の空間発展は_new_site/_start_pioneeringが従来どおり担う。
            x, y = _offset(
                (root["x"], root["y"]), f"{household['id']}:site")
            site = _site_record(
                household, turn, x, y, parent_site_id=root_site_id,
                root_site_id=root_site_id)
        sites[site_id] = site
    return sites


def _new_site(state: dict, registry: dict, household: dict, turn: int) -> dict:
    sites = [row for row in state["sites"].values() if row.get("active", True)]
    resident_site_ids = {
        state["residents"][resident_id]["site_id"]
        for resident_id, resident in registry.get("residents", {}).items()
        if resident.get("alive", True)
        and resident.get("household_id") == household["id"]
        and resident_id in state["residents"]
    }
    parent_candidates = [
        state["sites"][site_id] for site_id in sorted(resident_site_ids)
        if site_id in state["sites"]]
    if not parent_candidates:
        parent_ids = {
            parent_id for resident in registry.get("residents", {}).values()
            if resident.get("alive", True)
            and resident.get("household_id") == household["id"]
            for parent_id in resident.get("parent_ids", ())}
        parent_candidates = [
            state["sites"][state["residents"][parent_id]["site_id"]]
            for parent_id in sorted(parent_ids)
            if parent_id in state["residents"]
            and state["residents"][parent_id]["site_id"] in state["sites"]]
    if not parent_candidates:
        parent_candidates = [
            row for row in sites
            if row.get("livelihood") == household.get("livelihood")]
    if not parent_candidates:
        parent_candidates = sites
    if not parent_candidates:
        x = _bounded(0.12 + _unit(household["id"], "x") * 0.76)
        y = _bounded(0.12 + _unit(household["id"], "y") * 0.76)
        site_id = f"site:{household['id']}"
        return _site_record(
            household, turn, x, y, parent_site_id=None,
            root_site_id=site_id)
    parent = parent_candidates[
        _stable_int(household["id"], "new-parent") % len(parent_candidates)]
    x, y = _offset(
        (parent["x"], parent["y"]), f"{household['id']}:{turn}:new-site")
    return _site_record(
        household, turn, x, y, parent_site_id=parent["id"],
        root_site_id=parent.get("root_site_id", parent["id"]))


def _retarget_changed_account(state: dict, site: dict, household: dict,
                              turn: int) -> None:
    account_id = household.get("settlement_id")
    if site.get("account_id") == account_id:
        return
    candidates = [
        row for row in state["sites"].values()
        if row["id"] != site["id"] and row.get("active", True)
        and row.get("account_id") == account_id]
    if candidates:
        parent = candidates[
            _stable_int(site["id"], account_id, "migration-target") % len(candidates)]
        target_x, target_y = _offset(
            (parent["x"], parent["y"]), f"{site['id']}:{account_id}:migration")
        site["target_x"], site["target_y"] = target_x, target_y
        site["parent_site_id"] = parent["id"]
        site["root_site_id"] = parent.get("root_site_id", parent["id"])
        if state.get("accounting_mode") == "activity_communities":
            # 数値上の移住と空間所属を同じ月末境界で確定する。水槽上の移動は
            # residents_migratedイベントが住民pixel自身を補間して表現する。
            site["x"], site["y"] = target_x, target_y
        # 住居を活動点と一緒に毎月滑らせない。所属変更という明示的な
        # 移住境界で新しい住居座標を一度だけ確定し、以後は固定する。
        site["home_x"], site["home_y"] = _offset(
            (target_x, target_y),
            f"{site['household_id']}:home:{account_id}", (0.008, 0.022))
    site["account_id"] = account_id
    site["updated_turn"] = int(turn)


def _move_sites(state: dict, turn: int) -> None:
    for site in state["sites"].values():
        if not site.get("active", True):
            continue
        site["x"] = round(
            site["x"] + (site.get("target_x", site["x"]) - site["x"])
            * SITE_MOVE_RATE, 9)
        site["y"] = round(
            site["y"] + (site.get("target_y", site["y"]) - site["y"])
            * SITE_MOVE_RATE, 9)
        # homeは固定アンカー。活動点の開拓・微調整で住居まで移動させない。
        # 世帯が実際に所属を変えた時だけ_retarget_changed_accountで更新する。
        site["updated_turn"] = int(turn)


def _resident_activity_episode(resident: dict, household: dict,
                               turn: int) -> dict:
    """当月の住居―活動点間の移動根拠を空間状態として固定する。

    ``primary``は背景生産の実際の担い手、``home``は住居側に留まる住民。
    記録にない「労働年齢なら通勤する」という動きは合成しない。開始・帰宅
    位相はstable hash由来で、画面のwall clockやゲーム本体RNGには依存しない。
    表示側はこのepisodeを補間するだけで、全住民を一律に往復させない。
    """
    turn = int(turn)
    primary = resident.get("last_activity_turn") == turn
    if primary:
        mode = ACTIVITY_MODE_PRIMARY
    else:
        mode = ACTIVITY_MODE_HOME
    activity = (
        resident.get("last_activity") if primary
        else household.get("livelihood")) or "unknown"
    if mode == ACTIVITY_MODE_PRIMARY:
        departure = 0.05 + _unit(
            resident["id"], turn, "primary-departure") * 0.12
        return_phase = 0.78 + _unit(
            resident["id"], turn, "primary-return") * 0.14
    else:
        departure, return_phase = 0.0, 1.0
    return {
        "activity_mode": mode,
        "activity": activity,
        "departure_phase": round(departure, 6),
        "return_phase": round(return_phase, 6),
    }


def _sync_residents(state: dict, registry: dict, turn: int) -> None:
    living = sorted((
        row for row in registry.get("residents", {}).values()
        if row.get("alive", True)), key=lambda row: row["id"])
    members = {}
    for resident in living:
        members.setdefault(resident["household_id"], []).append(resident)
    households = registry.get("households", {})
    before = state.get("residents", {})
    after = {}
    for household_id, rows in members.items():
        site_id = f"site:{household_id}"
        site = state["sites"].get(site_id)
        if site is None:
            continue
        household = households.get(household_id, {})
        for index, resident in enumerate(rows):
            angle = (_unit(resident["id"], "resident-angle") * math.tau
                     + index * 2.399963)
            radius = min(0.018, 0.003 + math.sqrt(index) * 0.0025)
            # 永続座標は住居側の個人アンカー。活動先への月内移動は下の
            # episodeとして別保存し、描画時だけ補間する。これにより担当者が
            # 変わっても住居の点群そのものが活動点へ滑らない。
            base_x = site["home_x"]
            base_y = site["home_y"]
            target_x = _bounded(base_x + math.cos(angle) * radius)
            target_y = _bounded(base_y + math.sin(angle) * radius)
            previous = before.get(resident["id"])
            x = target_x if previous is None else round(
                previous["x"] + (target_x - previous["x"])
                * RESIDENT_MOVE_RATE, 9)
            y = target_y if previous is None else round(
                previous["y"] + (target_y - previous["y"])
                * RESIDENT_MOVE_RATE, 9)
            episode = _resident_activity_episode(
                resident, household, turn)
            after[resident["id"]] = {
                "resident_id": resident["id"],
                "household_id": household_id, "site_id": site_id,
                "x": x, "y": y,
                "target_x": target_x, "target_y": target_y,
                "updated_turn": int(turn),
                **episode,
            }
    state["residents"] = after


def _sync_site_population_weights(state: dict, registry: dict) -> None:
    """匿名cohortを代表siteへ整数配賦し、空間上の総人口を保存する。"""
    named_by_site = {}
    for row in state.get("residents", {}).values():
        named_by_site[row["site_id"]] = named_by_site.get(
            row["site_id"], 0) + 1
    sites_by_account = {}
    for site in state.get("sites", {}).values():
        site["named_resident_count"] = named_by_site.get(site["id"], 0)
        site["population_weight"] = site["named_resident_count"]
        if site.get("active", True):
            sites_by_account.setdefault(
                str(site.get("account_id")), []).append(site)

    anonymous = {
        str(key): max(0, int(value))
        for key, value in registry.get(
            "anonymous_population_by_settlement", {}).items()
        if int(value) > 0}
    for account_id, anonymous_count in anonymous.items():
        sites = sorted(sites_by_account.get(account_id, ()),
                       key=lambda row: row["id"])
        if not sites:
            continue
        weights = [max(1, site["named_resident_count"]) for site in sites]
        denominator = sum(weights)
        allocations = [anonymous_count * weight // denominator
                       for weight in weights]
        remaining = anonymous_count - sum(allocations)
        order = sorted(range(len(sites)), key=lambda index: (
            -(anonymous_count * weights[index] % denominator),
            sites[index]["id"]))
        for index in order[:remaining]:
            allocations[index] += 1
        for site, count in zip(sites, allocations):
            site["population_weight"] += count


def _sync_site_activity(state: dict, activity_economy_state: dict | None,
                        turn: int) -> None:
    """当月の活動経済台帳を活動場所へ写す。

    台帳が無い旧経路では既存の世帯activityだけを使うため、ここでは状態を
    消去しない。台帳がある場合は全siteを0へ戻してから当月配賦を正本として
    適用し、匿名workerも個体化せず人数のまま保持する。
    """
    if not isinstance(activity_economy_state, dict):
        return
    for site in state.get("sites", {}).values():
        site["activity_worker_count"] = 0
        site["activity_named_worker_count"] = 0
        site["activity_anonymous_worker_count"] = 0
        site["activity_worker_count_by_good"] = {}
        site["activity_output_by_good"] = {}
    # 再開直後や月初の同期では、checkpointに残る前月の台帳を当月活動として
    # 再適用しない。updated_turnが当月と一致する台帳だけが空間上の正本。
    if int(activity_economy_state.get("updated_turn", -1)) != int(turn):
        return
    for community in activity_economy_state.get(
            "communities", {}).values():
        for good, activity in community.get("activities", {}).items():
            for allocation in activity.get("site_allocations", ()):
                site = state.get("sites", {}).get(allocation.get("site_id"))
                if site is None:
                    raise ValueError(
                        "activity economy allocation refers to missing site")
                # 生産後の人口更新で最後の構成員が死亡すると、同じ月の末に
                # siteは閉鎖される。それでも当月の生産活動は既に起きている
                # ため、閉鎖siteにも当月分だけ保存する。
                workers = max(0, int(allocation.get("worker_count", 0)))
                named = max(0, int(allocation.get(
                    "named_worker_count", 0)))
                anonymous = max(0, int(allocation.get(
                    "anonymous_worker_count", 0)))
                if named + anonymous != workers:
                    raise ValueError("activity site worker accounting drift")
                site["activity_worker_count"] += workers
                site["activity_named_worker_count"] += named
                site["activity_anonymous_worker_count"] += anonymous
                site["activity_worker_count_by_good"][good] = (
                    site["activity_worker_count_by_good"].get(good, 0)
                    + workers)
                site["activity_output_by_good"][good] = round(
                    float(site["activity_output_by_good"].get(good, 0.0))
                    + float(allocation.get("gross_output", 0.0)), 6)
                if workers > 0 or float(
                        allocation.get("gross_output", 0.0)) > 0.0:
                    site["last_activity_turn"] = int(turn)
    for site in state.get("sites", {}).values():
        if sum(site["activity_worker_count_by_good"].values()) != int(
                site["activity_worker_count"]):
            raise RuntimeError("site per-good activity worker accounting drift")


def derive_activity_clusters(state: dict, distance: float =
                             SPATIAL_CLUSTER_DISTANCE) -> dict:
    """活動場所の近接連結成分を、表示上の集落として導出する。"""
    sites = sorted((
        row for row in state.get("sites", {}).values()
        if row.get("active", True)), key=lambda row: row["id"])
    if not sites:
        return {}
    parents = list(range(len(sites)))

    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def join(left, right):
        a, b = find(left), find(right)
        if a != b:
            parents[b] = a

    cells = {}
    for index, site in enumerate(sites):
        gx, gy = int(site["x"] / distance), int(site["y"] / distance)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in cells.get((gx + dx, gy + dy), ()):
                    prior = sites[other]
                    if math.hypot(
                            site["x"] - prior["x"],
                            site["y"] - prior["y"]) <= distance:
                        join(index, other)
        cells.setdefault((gx, gy), []).append(index)

    groups = {}
    for index, site in enumerate(sites):
        groups.setdefault(find(index), []).append(site)
    resident_counts = {}
    for row in state.get("residents", {}).values():
        resident_counts[row["site_id"]] = (
            resident_counts.get(row["site_id"], 0) + 1)
    clusters = {}
    for group in groups.values():
        group.sort(key=lambda row: row["id"])
        cluster_id = f"cluster:{group[0]['household_id']}"
        livelihoods, accounts = {}, {}
        for site in group:
            livelihoods[site["livelihood"]] = (
                livelihoods.get(site["livelihood"], 0) + 1)
            account = str(site.get("account_id"))
            accounts[account] = accounts.get(account, 0) + 1
        clusters[cluster_id] = {
            "id": cluster_id,
            "site_ids": [row["id"] for row in group],
            "centroid_x": round(sum(row["x"] for row in group) / len(group), 9),
            "centroid_y": round(sum(row["y"] for row in group) / len(group), 9),
            "site_count": len(group),
            "resident_count": sum(
                int(row.get(
                    "population_weight",
                    resident_counts.get(row["id"], 0)))
                for row in group),
            "named_resident_count": sum(
                int(row.get(
                    "named_resident_count",
                    resident_counts.get(row["id"], 0)))
                for row in group),
            "livelihood_counts": livelihoods,
            "account_counts": accounts,
        }
    return clusters


def _next_cluster_id(state: dict) -> str:
    serial = int(state.get("next_cluster_serial", 1))
    state["next_cluster_serial"] = serial + 1
    return f"cluster:{serial:06d}"


def _cluster_event(kind: str, turn: int, cluster: dict, **extra) -> dict:
    """クラスタ変化を、画面pixelに依存しない世界座標で記録する。"""
    return {
        "turn": int(turn), "kind": kind,
        "cluster_id": cluster["id"],
        "x": cluster["centroid_x"], "y": cluster["centroid_y"],
        "site_count": cluster["site_count"],
        "resident_count": cluster["resident_count"],
        **extra,
    }


def _new_lineage(cluster: dict, turn: int, parent_ids: list[str]) -> dict:
    return {
        "id": cluster["id"], "born_turn": int(turn),
        "ended_turn": None, "active": True,
        "parent_ids": list(parent_ids), "merged_into": None,
        "first_centroid_x": cluster["centroid_x"],
        "first_centroid_y": cluster["centroid_y"],
        "last_centroid_x": cluster["centroid_x"],
        "last_centroid_y": cluster["centroid_y"],
        "last_turn": int(turn),
        "peak_site_count": cluster["site_count"],
        "peak_resident_count": cluster["resident_count"],
    }


def _update_lineage(lineage: dict, cluster: dict, turn: int) -> None:
    lineage["active"] = True
    lineage["ended_turn"] = None
    lineage["merged_into"] = None
    lineage["last_centroid_x"] = cluster["centroid_x"]
    lineage["last_centroid_y"] = cluster["centroid_y"]
    lineage["last_turn"] = int(turn)
    lineage["peak_site_count"] = max(
        int(lineage.get("peak_site_count", 0)), cluster["site_count"])
    lineage["peak_resident_count"] = max(
        int(lineage.get("peak_resident_count", 0)),
        cluster["resident_count"])


def _bootstrap_cluster_tracking(state: dict, turn: int, *,
                                reason: str = "initial") -> None:
    """活動密度から最初の集落系譜を作る。legacy accountは参照しない。"""
    raw_clusters = sorted(
        derive_activity_clusters(state).values(),
        key=lambda row: tuple(row["site_ids"]))
    state["clusters"] = {}
    state["cluster_lineages"] = {}
    state["cluster_events"] = []
    state["next_cluster_serial"] = 1
    for raw in raw_clusters:
        cluster_id = _next_cluster_id(state)
        cluster = dict(raw, id=cluster_id, formed_turn=int(turn),
                       parent_ids=[])
        state["clusters"][cluster_id] = cluster
        state["cluster_lineages"][cluster_id] = _new_lineage(
            cluster, turn, [])
        state["cluster_events"].append(_cluster_event(
            "cluster_formed", turn, cluster, reason=reason,
            parent_cluster_ids=[]))


def _tracked_activity_clusters(state: dict, turn: int, *,
                               record_events: bool = True) -> None:
    """近接成分を前月の成分へ重ね、集落の時間を越えた同一性を決める。

    最大の重なりを持つ子成分が旧IDを継ぐ。分裂した残りの子には新IDを与え、
    合流時は最大重なりの旧IDだけを残す。距離計算は従来どおりで、経済会計の
    settlement/account IDは照合に使わない。
    """
    previous = state.get("clusters", {})
    if "cluster_lineages" not in state:
        _bootstrap_cluster_tracking(state, turn, reason="legacy_upgrade")
        return
    raw = sorted(
        derive_activity_clusters(state).values(),
        key=lambda row: tuple(row["site_ids"]))
    previous_ids = sorted(previous)
    previous_sites = {
        cluster_id: set(previous[cluster_id].get("site_ids", ()))
        for cluster_id in previous_ids}
    raw_sites = [set(row.get("site_ids", ())) for row in raw]

    # 一対一の継承を、重なり数→Jaccard→IDの固定順で決める。
    edges = []
    for cluster_id in previous_ids:
        for index, site_ids in enumerate(raw_sites):
            overlap = len(previous_sites[cluster_id] & site_ids)
            if not overlap:
                continue
            union = len(previous_sites[cluster_id] | site_ids)
            edges.append((
                -overlap, -(overlap / max(1, union)), cluster_id,
                tuple(raw[index]["site_ids"]), index))
    matched_previous, matched_raw = {}, {}
    for _, _, cluster_id, _, index in sorted(edges):
        if cluster_id in matched_previous or index in matched_raw:
            continue
        matched_previous[cluster_id] = index
        matched_raw[index] = cluster_id

    sources_by_raw = {
        index: sorted((
            cluster_id for cluster_id in previous_ids
            if previous_sites[cluster_id] & raw_sites[index]),
            key=lambda cluster_id: (
                -len(previous_sites[cluster_id] & raw_sites[index]),
                cluster_id))
        for index in range(len(raw))}
    descendants_by_previous = {
        cluster_id: [
            index for index in range(len(raw))
            if previous_sites[cluster_id] & raw_sites[index]]
        for cluster_id in previous_ids}

    current = {}
    raw_to_cluster_id = {}
    lineages = state.setdefault("cluster_lineages", {})
    events = state.setdefault("cluster_events", [])
    for index, row in enumerate(raw):
        inherited_id = matched_raw.get(index)
        cluster_id = inherited_id or _next_cluster_id(state)
        source_ids = sources_by_raw[index]
        parent_ids = (list(previous[inherited_id].get("parent_ids", ()))
                      if inherited_id else source_ids)
        formed_turn = (previous[inherited_id].get("formed_turn", turn)
                       if inherited_id else int(turn))
        cluster = dict(
            row, id=cluster_id, formed_turn=int(formed_turn),
            parent_ids=list(parent_ids))
        current[cluster_id] = cluster
        raw_to_cluster_id[index] = cluster_id
        if inherited_id:
            lineage = lineages.setdefault(
                cluster_id, _new_lineage(cluster, formed_turn, []))
            _update_lineage(lineage, cluster, turn)
        else:
            lineages[cluster_id] = _new_lineage(
                cluster, turn, parent_ids)
            if record_events and not parent_ids:
                events.append(_cluster_event(
                    "cluster_formed", turn, cluster,
                    reason="activity_formed", parent_cluster_ids=[]))

    # 分裂は「新しい子lineageが生まれた場合」、合流は「古いlineageが
    # 終了した場合」だけを記録する。既存クラスタAから既存クラスタBへ
    # 1世帯が移ると、集合論上はAのsiteが2成分へ分かれ、Bへも重なる。
    # それをsplit+mergeと二重記録すると、単なる境界移動が集落の誕生・
    # 消滅に見えてしまう。住民移動そのものはpopulation_migrated/
    # residents_migratedで既に観察できるため、系譜イベントには数えない。
    if record_events:
        for cluster_id in previous_ids:
            descendants = descendants_by_previous[cluster_id]
            born_descendants = [
                index for index in descendants if index not in matched_raw]
            if len(descendants) > 1 and born_descendants:
                source = previous[cluster_id]
                events.append(_cluster_event(
                    "cluster_split", turn, source,
                    source_cluster_id=cluster_id,
                    cluster_ids=[raw_to_cluster_id[i] for i in descendants]))
        for index, source_ids in sources_by_raw.items():
            ended_sources = [
                cluster_id for cluster_id in source_ids
                if cluster_id not in matched_previous]
            if len(source_ids) > 1 and ended_sources:
                cluster = current[raw_to_cluster_id[index]]
                events.append(_cluster_event(
                    "cluster_merged", turn, cluster,
                    source_cluster_ids=source_ids))

    for cluster_id in previous_ids:
        descendants = descendants_by_previous[cluster_id]
        retained = matched_previous.get(cluster_id)
        if retained is not None:
            continue
        lineage = lineages.get(cluster_id)
        if lineage is not None:
            lineage["active"] = False
            lineage["ended_turn"] = int(turn)
        if descendants:
            destination_id = raw_to_cluster_id[descendants[0]]
            if lineage is not None:
                lineage["merged_into"] = destination_id
        elif record_events:
            events.append(_cluster_event(
                "cluster_dissolved", turn, previous[cluster_id],
                reason="no_active_sites"))
    state["clusters"] = current
    _annotate_accounting_clusters(state)
    if record_events:
        for cluster_id, cluster in current.items():
            before = previous.get(cluster_id, {})
            if (not before.get("provisional", False)
                    or cluster.get("provisional", False)):
                continue
            lineage = lineages.setdefault(
                cluster_id, _new_lineage(
                    cluster, cluster.get("formed_turn", turn),
                    cluster.get("parent_ids", [])))
            if lineage.get("pioneering_community_formed_turn") is not None:
                continue
            lineage["pioneering_community_formed_turn"] = int(turn)
            events.append(_cluster_event(
                "pioneering_community_formed", turn, cluster,
                source_cluster_id=before.get("accounting_parent_id"),
                pioneering_site_count=cluster.get(
                    "pioneering_site_count", 0)))


def _cluster_by_site(state: dict) -> dict[str, str]:
    return {
        site_id: cluster_id
        for cluster_id, cluster in state.get("clusters", {}).items()
        for site_id in cluster.get("site_ids", ())
    }


def _annotate_accounting_clusters(state: dict) -> None:
    """単独の開拓点を、会計主体になる前の前集落として印付けする。

    活動点が三つ以上かつ人口8人以上集まるまでは、空間上は独立した塊として
    見える一方、人口・財の会計は母共同体へ属し続ける。これにより「活動が
    集まる前から集落が存在する」という逆転と、活動点1つごとに固定upkeepが
    増える二重計上を同時に避ける。
    """
    clusters = state.get("clusters", {})
    sites = state.get("sites", {})
    for cluster_id, cluster in clusters.items():
        rows = [sites[site_id] for site_id in cluster.get("site_ids", ())
                if site_id in sites]
        origins = {
            row.get("pioneering_origin_cluster_id") for row in rows
            if row.get("pioneering_origin_cluster_id") is not None}
        all_pioneering = bool(rows) and len(origins) == 1 and all(
            row.get("pioneering_started_turn") is not None for row in rows)
        origin_id = next(iter(origins), None)
        provisional = bool(
            all_pioneering and origin_id in clusters
            and (int(cluster.get("site_count", 0))
                 < PIONEERING_COMMUNITY_SITE_THRESHOLD
                 or int(cluster.get("resident_count", 0))
                 < PIONEERING_COMMUNITY_POPULATION_THRESHOLD))
        cluster["provisional"] = provisional
        cluster["accounting_parent_id"] = (
            origin_id if provisional else cluster_id)
        cluster["pioneering_site_count"] = (
            len(rows) if all_pioneering else 0)


def accounting_activity_cluster_ids(state: dict) -> set[str]:
    """数値会計へ昇格済みの活動クラスタIDを返す。"""
    return {
        cluster_id for cluster_id, cluster
        in state.get("clusters", {}).items()
        if not cluster.get("provisional", False)}


def _pioneering_target(state: dict, site: dict, cluster: dict,
                       turn: int) -> tuple[float, float]:
    """既存の活動密度から最も離れた、決定論的な新活動地を選ぶ。"""
    # 同じ母共同体からすでに前集落が伸びている場合は、その近傍へ次の
    # 活動点を置く。点が三つ集まるまで会計共同体へ昇格しないため、孤立点を
    # 世界中へ量産せず「活動の集まりが先、集落が後」を構造で表せる。
    outposts = sorted((
        row for row in state.get("clusters", {}).values()
        if row.get("provisional", False)
        and row.get("accounting_parent_id") == cluster["id"]),
        key=lambda row: (int(row.get("site_count", 0)), row["id"]))
    if outposts:
        outpost = outposts[0]
        return _offset(
            (outpost["centroid_x"], outpost["centroid_y"]),
            f"{site['id']}:{outpost['id']}:{turn}:pioneering-outpost",
            (0.018, 0.038))
    active_sites = [
        row for row in state.get("sites", {}).values()
        if row.get("active", True) and row["id"] != site["id"]]
    rotation = _unit(
        state.get("world_seed", 0), site["id"], turn,
        "pioneering-rotation") * math.tau
    best = None
    for radius_index, radius in enumerate(PIONEERING_TARGET_RADII):
        for angle_index in range(24):
            angle = rotation + math.tau * angle_index / 24
            x = _bounded(
                cluster["centroid_x"] + math.cos(angle) * radius)
            y = _bounded(
                cluster["centroid_y"] + math.sin(angle) * radius)
            nearest = min((
                math.hypot(x - row["x"], y - row["y"])
                for row in active_sites), default=1.0)
            source_distance = math.hypot(
                x - cluster["centroid_x"], y - cluster["centroid_y"])
            # 同点時も入力順やdict順ではなく、固定した半径・角度順で決める。
            score = (
                round(nearest, 12), round(source_distance, 12),
                -radius_index, -angle_index)
            if best is None or score > best[0]:
                best = (score, x, y)
    if best is None:
        return site["x"], site["y"]
    return best[1], best[2]


def _start_pioneering(state: dict, registry: dict, turn: int) -> None:
    """活動済み世帯を1つだけ選び、新活動地への移動を開始する。"""
    turn = int(turn)
    if turn < PIONEERING_FIRST_TURN:
        return
    last_turn = state.get("last_pioneering_turn")
    if (last_turn is not None
            and turn - int(last_turn) < PIONEERING_COOLDOWN_TURNS):
        return
    clusters = state.get("clusters", {})
    if len(clusters) >= PIONEERING_MAX_ACTIVE_CLUSTERS:
        return
    cluster_by_site = _cluster_by_site(state)
    candidates = []
    for household in registry.get("households", {}).values():
        if not household.get("active", True):
            continue
        site = state.get("sites", {}).get(f"site:{household['id']}")
        if (site is None or not site.get("active", True)
                or site.get("pioneering_started_turn") is not None):
            continue
        if (household.get("last_activity_turn") != turn
                and site.get("last_activity_turn") != turn):
            continue
        # 会計移住等ですでに別の目的地へ向かっている活動点へ、二重の意図を
        # 上書きしない。
        if math.hypot(
                site.get("target_x", site["x"]) - site["x"],
                site.get("target_y", site["y"]) - site["y"]) > 1e-6:
            continue
        cluster_id = cluster_by_site.get(site["id"])
        cluster = clusters.get(cluster_id)
        if cluster is None:
            continue
        if (turn - int(cluster.get("formed_turn", turn))
                < PIONEERING_MIN_CLUSTER_AGE
                or int(cluster.get("site_count", 0))
                < PIONEERING_MIN_CLUSTER_SITES
                or int(cluster.get("resident_count", 0))
                < PIONEERING_MIN_CLUSTER_POPULATION
                or int(site.get("population_weight", 0))
                < PIONEERING_MIN_SITE_POPULATION):
            continue
        candidates.append((
            -int(site.get("activity_worker_count", 0)),
            -int(household.get("activity_count", 0)),
            -int(site.get("population_weight", 0)),
            household["id"], household, site, cluster))
    if not candidates:
        return

    _, _, _, _, household, site, cluster = min(candidates)
    target_x, target_y = _pioneering_target(
        state, site, cluster, turn)
    state["pioneering_sequence"] = int(
        state.get("pioneering_sequence", 0)) + 1
    pioneering_id = f"pioneer:{state['pioneering_sequence']:06d}"
    actor = registry.get("residents", {}).get(
        household.get("last_actor_id"), {})
    site.update({
        "target_x": target_x, "target_y": target_y,
        "pioneering_id": pioneering_id,
        "pioneering_started_turn": turn,
        "pioneering_settled_turn": None,
        "pioneering_origin_cluster_id": cluster["id"],
    })
    state["last_pioneering_turn"] = turn
    state.setdefault("cluster_events", []).append({
        "turn": turn, "kind": "pioneering_started",
        "pioneering_id": pioneering_id,
        "site_id": site["id"], "household_id": household["id"],
        "household_name": household.get("name", household["id"]),
        "resident_id": actor.get("id"),
        "resident_name": actor.get("name"),
        "livelihood": site.get("livelihood"),
        "source_cluster_id": cluster["id"],
        "x": site["x"], "y": site["y"],
        "target_x": target_x, "target_y": target_y,
        "site_count": 1,
        "resident_count": int(site.get("population_weight", 0)),
    })


def _record_pioneering_arrivals(state: dict, turn: int) -> None:
    """移動した活動点が元クラスタを離れた月を、共同体成立として記録する。"""
    cluster_by_site = _cluster_by_site(state)
    for site in sorted(state.get("sites", {}).values(),
                       key=lambda row: row["id"]):
        if (site.get("pioneering_started_turn") is None
                or site.get("pioneering_settled_turn") is not None):
            continue
        cluster_id = cluster_by_site.get(site["id"])
        origin_id = site.get("pioneering_origin_cluster_id")
        if cluster_id is None or cluster_id == origin_id:
            continue
        cluster = state.get("clusters", {}).get(cluster_id, {})
        site["pioneering_settled_turn"] = int(turn)
        state.setdefault("cluster_events", []).append({
            "turn": int(turn), "kind": "pioneering_settled",
            "pioneering_id": site.get("pioneering_id"),
            "site_id": site["id"],
            "household_id": site.get("household_id"),
            "livelihood": site.get("livelihood"),
            "source_cluster_id": origin_id,
            "cluster_id": cluster_id,
            "x": site["x"], "y": site["y"],
            "target_x": site.get("target_x", site["x"]),
            "target_y": site.get("target_y", site["y"]),
            "site_count": int(cluster.get("site_count", 1)),
            "resident_count": int(cluster.get(
                "resident_count", site.get("population_weight", 0))),
            "joined_existing": int(cluster.get("formed_turn", turn))
                < int(turn),
            "provisional": bool(cluster.get("provisional", False)),
        })


def _upgrade_spatial_state(state: dict, turn: int) -> dict:
    version = int(state.get("version", 0))
    if version == SPATIAL_STATE_VERSION:
        return state
    if version not in (
            1, CLUSTERED_SPATIAL_STATE_VERSION,
            PIONEERING_SPATIAL_STATE_VERSION,
            ACTIVITY_EPISODE_SPATIAL_STATE_VERSION,
            SITE_ACTIVITY_SPATIAL_STATE_VERSION):
        raise ValueError(f"unsupported spatial state version: {version}")
    after = copy.deepcopy(state)
    if version == 1:
        _bootstrap_cluster_tracking(after, turn, reason="legacy_upgrade")

    if version <= CLUSTERED_SPATIAL_STATE_VERSION:
        # v2にはcluster lineageまで存在するが、開拓の通し番号・最終実行月・
        # provisional会計境界が無かった。途中版のv2 checkpointに開拓記録が
        # すでに含まれる場合も、IDとeventから最大値を復元して重複採番しない。
        pioneer_ids = [
            str(value) for value in (
                [row.get("pioneering_id")
                 for row in after.get("sites", {}).values()]
                + [row.get("pioneering_id")
                   for row in after.get("cluster_events", [])])
            if value]
        inferred_sequence = max((
            int(value.rsplit(":", 1)[-1])
            for value in pioneer_ids
            if value.rsplit(":", 1)[-1].isdigit()), default=0)
        after["pioneering_sequence"] = max(
            int(after.get("pioneering_sequence", 0)), inferred_sequence)
        started_turns = [
            int(value) for value in (
                [row.get("pioneering_started_turn")
                 for row in after.get("sites", {}).values()]
                + [row.get("turn") for row in after.get("cluster_events", [])
                   if row.get("kind") == "pioneering_started"])
            if value is not None]
        if after.get("last_pioneering_turn") is None:
            after["last_pioneering_turn"] = max(started_turns, default=None)
        _annotate_accounting_clusters(after)
    # v3のresident座標には移動の理由が無かった。registry無しで読むcompact
    # 境界では安全なhome episodeを補い、次の同期時に実データで上書きする。
    for resident in after.get("residents", {}).values():
        resident.setdefault("activity_mode", ACTIVITY_MODE_HOME)
        resident.setdefault("activity", "unknown")
        resident.setdefault("departure_phase", 0.0)
        resident.setdefault("return_phase", 1.0)
    for site in after.get("sites", {}).values():
        site.setdefault("last_activity_turn", None)
        site.setdefault("activity_worker_count", 0)
        site.setdefault("activity_named_worker_count", 0)
        site.setdefault("activity_anonymous_worker_count", 0)
        site.setdefault("activity_worker_count_by_good", {})
        site.setdefault("activity_output_by_good", {})
    after["version"] = SPATIAL_STATE_VERSION
    return after


def initial_spatial_state(registry: dict, turn: int = 1) -> dict:
    """住民台帳から、settlement数に依存しない初期空間を構築する。"""
    if not isinstance(registry, dict):
        raise TypeError("resident registry must be a dict")
    state = {
        "version": SPATIAL_STATE_VERSION,
        "world_seed": int(registry.get("world_seed", 0)),
        "created_turn": int(turn), "updated_turn": int(turn),
        "pioneering_sequence": 0, "last_pioneering_turn": None,
        "sites": _bootstrap_sites(registry, turn),
        "residents": {}, "clusters": {},
    }
    _sync_residents(state, registry, turn)
    _sync_site_population_weights(state, registry)
    _bootstrap_cluster_tracking(state, turn)
    _annotate_accounting_clusters(state)
    if not spatial_state_matches_registry(state, registry):
        raise RuntimeError("initial spatial state does not match resident registry")
    return state


def synchronize_spatial_state(state: dict | None, registry: dict,
                              turn: int, *,
                              activity_economy_state: dict | None = None) -> dict:
    """出生・死亡・世帯分割・移住を永続空間へ同期する。

    既存座標を保存し、新しい活動場所だけを既存点へ付着させる。会計上の所属が
    変わった世帯は移行先の活動点へ複数ターンかけて移動する。
    """
    if state is None:
        return initial_spatial_state(registry, turn)
    upgraded = _upgrade_spatial_state(state, turn)
    after = copy.deepcopy(upgraded)
    households = {row["id"]: row for row in _active_households(registry)}
    for site in after.get("sites", {}).values():
        if site["household_id"] not in households and site.get("active", True):
            site["active"] = False
            site["closed_turn"] = int(turn)
    for household_id, household in households.items():
        site_id = f"site:{household_id}"
        if site_id not in after["sites"]:
            after["sites"][site_id] = _new_site(
                after, registry, household, turn)
        site = after["sites"][site_id]
        site["active"] = True
        site["closed_turn"] = None
        site["livelihood"] = household.get(
            "livelihood", site.get("livelihood"))
        _retarget_changed_account(after, site, household, turn)
    _sync_site_activity(after, activity_economy_state, turn)
    _start_pioneering(after, registry, turn)
    _move_sites(after, turn)
    _sync_residents(after, registry, turn)
    _sync_site_population_weights(after, registry)
    _tracked_activity_clusters(after, turn)
    _record_pioneering_arrivals(after, turn)
    after["updated_turn"] = int(turn)
    if not spatial_state_matches_registry(after, registry):
        raise RuntimeError("spatial state does not match resident registry")
    return after


def compact_spatial_state(state: dict, registry: dict) -> dict:
    """観察窓から落ちた死者・閉鎖世帯の空間レコードを同時に除く。"""
    after = copy.deepcopy(_upgrade_spatial_state(
        state, int(state.get("updated_turn", 0))))
    household_ids = set(registry.get("households", {}))
    resident_ids = set(registry.get("residents", {}))
    after["sites"] = {
        key: row for key, row in after.get("sites", {}).items()
        if row["household_id"] in household_ids}
    after["residents"] = {
        key: row for key, row in after.get("residents", {}).items()
        if key in resident_ids and row["site_id"] in after["sites"]}
    _tracked_activity_clusters(
        after, int(after.get("updated_turn", 0)), record_events=False)
    return after


def relabel_spatial_accounts(state: dict, registry: dict) -> dict:
    """活動共同体へ昇格した住民台帳の会計IDを、座標を動かさず反映する。"""
    after = copy.deepcopy(state)
    households = registry.get("households", {})
    for site in after.get("sites", {}).values():
        household = households.get(site.get("household_id"))
        if household is not None:
            site["account_id"] = household.get("settlement_id")
    after["accounting_mode"] = "activity_communities"
    return after


def build_spatial_keyframe(state: dict, turn: int | None = None) -> dict:
    """履歴再生用の軽量な空間snapshotを作る。

    checkpointの完全な辞書を毎月複製せず、描画・補間に必要な座標と帰属だけを
    固定順の配列へ畳む。数値世界や入力stateは変更しない。
    """
    if int(state.get("version", 0)) not in (
            1, CLUSTERED_SPATIAL_STATE_VERSION,
            PIONEERING_SPATIAL_STATE_VERSION,
            ACTIVITY_EPISODE_SPATIAL_STATE_VERSION,
            SITE_ACTIVITY_SPATIAL_STATE_VERSION,
            SPATIAL_STATE_VERSION):
        raise ValueError("unsupported spatial state version")
    frame_turn = int(state.get("updated_turn", 0) if turn is None else turn)
    sites = sorted((
        row for row in state.get("sites", {}).values()
        if (row.get("active", True)
            or row.get("last_activity_turn") == frame_turn)),
        key=lambda row: row["id"])
    resident_rows = sorted(
        state.get("residents", {}).values(), key=lambda row: row["resident_id"])
    clusters = sorted(
        state.get("clusters", {}).values(), key=lambda row: row["id"])
    return {
        "version": SPATIAL_KEYFRAME_VERSION,
        "turn": frame_turn,
        # [site_id, household_id, livelihood, account_id, x, y,
        #  home_x, home_y, founded_turn, named_resident_count,
        #  population_weight, last_activity_turn, activity_worker_count,
        #  activity_named_worker_count, activity_anonymous_worker_count,
        #  activity_output_by_good, activity_worker_count_by_good]
        "sites": [[
            row["id"], row["household_id"], row.get("livelihood"),
            row.get("account_id"), row["x"], row["y"],
            row["home_x"], row["home_y"], row.get("founded_turn", frame_turn),
            int(row.get("named_resident_count", 0)),
            int(row.get(
                "population_weight", row.get("named_resident_count", 0))),
            row.get("last_activity_turn"),
            int(row.get("activity_worker_count", 0)),
            int(row.get("activity_named_worker_count", 0)),
            int(row.get("activity_anonymous_worker_count", 0)),
            dict(row.get("activity_output_by_good", {})),
            dict(row.get("activity_worker_count_by_good", {})),
        ] for row in sites],
        # [resident_id, site_id, household_id, x, y, activity_mode,
        #  activity, departure_phase, return_phase]
        "residents": [[
            row["resident_id"], row["site_id"], row["household_id"],
            row["x"], row["y"], row.get("activity_mode", ACTIVITY_MODE_HOME),
            row.get("activity", "unknown"),
            row.get("departure_phase", 0.0), row.get("return_phase", 1.0),
        ] for row in resident_rows],
        # [cluster_id, centroid_x, centroid_y, site_count, resident_count,
        #  formed_turn, parent_cluster_ids, site_ids, provisional,
        #  accounting_parent_id]
        # v4で末尾2項目を追加した。旧v1〜v3の8要素行は表示側が
        # provisional=False / accounting_parent_id=cluster_idとして読む。
        "clusters": [[
            row["id"], row["centroid_x"], row["centroid_y"],
            row["site_count"], row["resident_count"],
            row.get("formed_turn", frame_turn), row.get("parent_ids", []),
            row.get("site_ids", []),
            bool(row.get("provisional", False)),
            row.get("accounting_parent_id", row["id"]),
        ] for row in clusters],
    }


def spatial_state_matches_registry(state: dict, registry: dict) -> bool:
    living = {
        resident_id: resident
        for resident_id, resident in registry.get("residents", {}).items()
        if resident.get("alive", True)}
    active_households = {row["id"] for row in _active_households(registry)}
    active_sites = {
        row["household_id"] for row in state.get("sites", {}).values()
        if row.get("active", True)}
    positions = state.get("residents", {})
    named_by_site = {}
    for row in positions.values():
        named_by_site[row["site_id"]] = named_by_site.get(
            row["site_id"], 0) + 1
    expected_accounts = {
        str(key): max(0, int(value))
        for key, value in registry.get(
            "anonymous_population_by_settlement", {}).items()}
    for resident in living.values():
        account = str(resident.get("settlement_id"))
        expected_accounts[account] = expected_accounts.get(account, 0) + 1
    expected_accounts = {
        key: value for key, value in expected_accounts.items() if value > 0}
    spatial_accounts = {}
    weights_valid = True
    for site in state.get("sites", {}).values():
        if not site.get("active", True):
            continue
        named = named_by_site.get(site["id"], 0)
        weight = int(site.get("population_weight", named))
        weights_valid = weights_valid and weight >= named
        account = str(site.get("account_id"))
        spatial_accounts[account] = spatial_accounts.get(account, 0) + weight
    return (
        active_sites == active_households
        and set(positions) == set(living)
        and weights_valid
        and spatial_accounts == expected_accounts
        and all(
            row["household_id"] == living[resident_id]["household_id"]
            and row["site_id"] in state.get("sites", {})
            and 0.0 <= row["x"] <= 1.0 and 0.0 <= row["y"] <= 1.0
            for resident_id, row in positions.items())
    )
