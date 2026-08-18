#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""永続デジタル水槽の状態保存・時計・隔離worker制御。"""
import copy
import json
import math
import os
from pathlib import Path
import pickle
import subprocess
import sys
import threading
import time

from dashboard.build_dashboard import (
    DEFAULT_TEMPLATE, build_html, extract_dashboard_data)
from dashboard.aquarium_stream import (
    LOGICAL_FRAMES_PER_CHUNK, build_stream_snapshot, stream_chunk,
    validate_stream_snapshot)


PROJECT_DIR = Path(__file__).resolve().parent.parent
WORKER_PATH = Path(__file__).resolve().parent / "aquarium_worker.py"
DEFAULT_STATE_PATH = Path(os.environ.get(
    "LIFE_GAME_AQUARIUM_STATE", PROJECT_DIR / ".runtime" / "aquarium_world.json"))
DEFAULT_HTML_PATH = Path(os.environ.get(
    "LIFE_GAME_AQUARIUM_HTML", DEFAULT_STATE_PATH.with_name("aquarium.html")))
DEFAULT_STREAM_PATH = Path(os.environ.get(
    "LIFE_GAME_AQUARIUM_STREAM",
    DEFAULT_STATE_PATH.with_name("aquarium_stream.json")))
DEFAULT_PERFORMANCE_PATH = Path(os.environ.get(
    "LIFE_GAME_AQUARIUM_PERFORMANCE",
    DEFAULT_STATE_PATH.with_name("aquarium_performance.json")))
WORKER_TIMEOUT_SECONDS = 300
MIN_TICK_SECONDS = 1
MAX_TICK_SECONDS = 86400
DEFAULT_UNMEASURED_SAFE_TICK_SECONDS = 30
COMPUTE_HEADROOM_FACTOR = 1.5
COMPUTE_EMA_ALPHA = 0.25
MAX_MONTHS_PER_TICK = 120
MAX_CATCHUP_MONTHS = 1200
MIN_HISTORY_MONTHS = 120
MAX_HISTORY_MONTHS = 120000


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(
        data, ensure_ascii=False, separators=(",", ":")))


def read_world(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported aquarium world file")
    return data


def read_stream(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    validate_stream_snapshot(data)
    return data


class AquariumManager:
    """1つの保存世界を直列更新する。計算はworld専用隔離workerで行う。"""

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH,
                 html_path: Path = DEFAULT_HTML_PATH, *,
                 stream_path: Path = None,
                 performance_path: Path = None,
                 worker_path: Path = WORKER_PATH,
                 worker_timeout: int = WORKER_TIMEOUT_SECONDS,
                 time_fn=time.time, monotonic_fn=time.monotonic,
                 unmeasured_safe_tick_seconds: int =
                 DEFAULT_UNMEASURED_SAFE_TICK_SECONDS):
        self.state_path = Path(state_path)
        self.html_path = Path(html_path)
        self.stream_path = Path(
            stream_path if stream_path is not None
            else self.state_path.with_name("aquarium_stream.json"))
        self.performance_path = Path(
            performance_path if performance_path is not None
            else self.state_path.with_name("aquarium_performance.json"))
        self.worker_path = Path(worker_path)
        self.worker_timeout = worker_timeout
        self.time_fn = time_fn
        self.monotonic_fn = monotonic_fn
        self.unmeasured_safe_tick_seconds = max(
            MIN_TICK_SECONDS,
            min(MAX_TICK_SECONDS, int(unmeasured_safe_tick_seconds)))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None
        # statusはブラウザから1秒ごとに読まれる。数MiB〜数十MiBへ育つ
        # checkpoint JSONをpollのたびに解析せず、最後にatomic commitした小さな
        # 公開snapshotだけを保持する。server再起動時は最初のstatusで一度読む。
        self._status_cache = None
        self._worker_process = None
        self._worker_has_world = False

    def _read_world(self) -> dict | None:
        """数値checkpointへ、同revisionの小さな運用計測を重ねて返す。"""
        world = read_world(self.state_path)
        if world is None or not self.performance_path.exists():
            return world
        try:
            # 手作業や復旧処理がworldを後から置換した場合は、同revisionでも
            # 古いsidecarを重ねない。通常commitではsidecarを最後に置く。
            if (self.performance_path.stat().st_mtime_ns
                    < self.state_path.stat().st_mtime_ns):
                return world
            with self.performance_path.open(encoding="utf-8") as stream:
                runtime = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return world
        if (not isinstance(runtime, dict)
                or runtime.get("schema_version") != 1
                or runtime.get("stream_id") != world.get("stream_id")
                or int(runtime.get("revision", -1))
                != int(world.get("revision", -2))):
            return world
        if isinstance(runtime.get("compute_performance"), dict):
            world["compute_performance"] = copy.deepcopy(
                runtime["compute_performance"])
        if isinstance(runtime.get("clock"), dict):
            world["clock"] = copy.deepcopy(runtime["clock"])
        if isinstance(runtime.get("next_tick_at"), (int, float)):
            world["next_tick_at"] = float(runtime["next_tick_at"])
        return world

    def _write_runtime_metadata(self, world: dict) -> None:
        """巨大なcheckpointを再保存せず、commit後に確定する計測だけを保存。"""
        if not world.get("stream_id"):
            return
        atomic_write_json(self.performance_path, {
            "schema_version": 1,
            "stream_id": world["stream_id"],
            "revision": int(world.get("revision", 0)),
            "clock": copy.deepcopy(world.get("clock", {})),
            "next_tick_at": world.get("next_tick_at"),
            "compute_performance": copy.deepcopy(
                world.get("compute_performance", {})),
        })

    def _stop_worker(self) -> None:
        """このmanagerだけが所有するworkerを破棄する。"""
        proc = self._worker_process
        self._worker_process = None
        self._worker_has_world = False
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _ensure_worker(self) -> subprocess.Popen:
        proc = self._worker_process
        if proc is not None and proc.poll() is None:
            return proc
        self._stop_worker()
        self._worker_process = subprocess.Popen(
            [sys.executable, str(self.worker_path), "--pickle-loop"],
            cwd=PROJECT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        return self._worker_process

    def _exchange_with_worker(self, proc: subprocess.Popen,
                              request: dict) -> dict:
        """blocking pipe交換にtimeoutを付け、失敗したworkerを再利用しない。"""
        outcome = {}

        def exchange():
            try:
                pickle.dump(
                    request, proc.stdin,
                    protocol=pickle.HIGHEST_PROTOCOL)
                proc.stdin.flush()
                outcome["result"] = pickle.load(proc.stdout)
            except BaseException as exc:  # main threadへそのまま伝える
                outcome["error"] = exc

        exchange_thread = threading.Thread(
            target=exchange, name="life-game-aquarium-worker-io",
            daemon=True)
        exchange_thread.start()
        exchange_thread.join(self.worker_timeout)
        if exchange_thread.is_alive():
            self._stop_worker()
            exchange_thread.join(timeout=2)
            raise subprocess.TimeoutExpired(
                [sys.executable, str(self.worker_path), "--pickle-loop"],
                self.worker_timeout)
        if "error" in outcome:
            error = outcome["error"]
            self._stop_worker()
            raise ValueError(
                f"aquarium worker transport failed: {error}") from error
        result = outcome.get("result")
        if isinstance(result, dict) and "__aquarium_worker_error__" in result:
            detail = str(result["__aquarium_worker_error__"])
            self._stop_worker()
            raise ValueError(detail)
        if (not isinstance(result, dict) or "world" not in result
                or "dashboard_data" not in result):
            self._stop_worker()
            raise ValueError("aquarium worker returned an invalid response")
        return result

    def _invoke(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("aquarium worker payload must be an object")
        action = payload.get("action")
        if action == "start":
            # gameが値importする制度定数を別worldへ持ち越さない。
            self._stop_worker()
        proc = self._ensure_worker()
        request = payload
        if action == "advance" and self._worker_has_world:
            # worker内に同じcheckpoint+traceがある。全worldを毎月再送しない。
            request = {"action": "advance", "months": payload.get("months")}
        result = self._exchange_with_worker(proc, request)
        self._worker_has_world = True
        return result

    @staticmethod
    def _validate_clock(tick_seconds, months_per_tick) -> tuple[int, int]:
        try:
            tick_seconds = int(tick_seconds)
            months_per_tick = int(months_per_tick)
        except (TypeError, ValueError):
            raise ValueError("tick_seconds and months_per_tick must be integers") from None
        if not MIN_TICK_SECONDS <= tick_seconds <= MAX_TICK_SECONDS:
            raise ValueError(
                f"tick_seconds must be between {MIN_TICK_SECONDS} and {MAX_TICK_SECONDS}")
        if not 1 <= months_per_tick <= MAX_MONTHS_PER_TICK:
            raise ValueError(
                f"months_per_tick must be between 1 and {MAX_MONTHS_PER_TICK}")
        return tick_seconds, months_per_tick

    def _minimum_tick_seconds(self, world: dict = None) -> int:
        """実測が無ければ保守値、あれば計算時間+50%の安全下限を返す。"""
        performance = (world or {}).get("compute_performance", {})
        measured = performance.get("safe_tick_seconds")
        try:
            measured = int(measured)
        except (TypeError, ValueError):
            measured = self.unmeasured_safe_tick_seconds
        return max(MIN_TICK_SECONDS, min(MAX_TICK_SECONDS, measured))

    def _current_minimum_tick_seconds(self) -> int:
        try:
            world = self._read_world()
        except (OSError, ValueError, json.JSONDecodeError):
            world = None
        return self._minimum_tick_seconds(world)

    def _validate_safe_tick_seconds(self, tick_seconds: int) -> None:
        minimum = self._current_minimum_tick_seconds()
        if tick_seconds < minimum:
            raise ValueError(
                "tick_seconds must be at least "
                f"{minimum} seconds for the measured compute capacity")

    def _record_compute_performance(
            self, world: dict, elapsed_seconds: float,
            breakdown: dict = None, *, worker_elapsed_seconds: float = None,
            commit_elapsed_seconds: float = None) -> int:
        """worker開始からworld atomic commit完了までを安全下限へ反映する。"""
        sample = max(0.0, float(elapsed_seconds))
        previous = world.get("compute_performance", {})
        previous_ema = previous.get("ema_compute_seconds")
        if isinstance(previous_ema, (int, float)) and previous_ema >= 0:
            ema = (COMPUTE_EMA_ALPHA * sample
                   + (1.0 - COMPUTE_EMA_ALPHA) * float(previous_ema))
        else:
            ema = sample
        safe = max(
            MIN_TICK_SECONDS,
            min(MAX_TICK_SECONDS, int(math.ceil(
                max(sample, ema) * COMPUTE_HEADROOM_FACTOR))))
        measured_breakdown = {
            str(name): round(max(0.0, float(value)), 6)
            for name, value in (breakdown or {}).items()
            if isinstance(value, (int, float))}
        execute_seconds = measured_breakdown.get("total_execute_seconds", 0.0)
        worker_elapsed = (sample if worker_elapsed_seconds is None else max(
            0.0, float(worker_elapsed_seconds)))
        commit_elapsed = (0.0 if commit_elapsed_seconds is None else max(
            0.0, float(commit_elapsed_seconds)))
        measured_breakdown["worker_boundary_seconds"] = round(
            max(0.0, worker_elapsed - execute_seconds), 6)
        measured_breakdown["commit_seconds"] = round(commit_elapsed, 6)
        measured_breakdown["orchestration_seconds"] = round(max(
            0.0, sample - worker_elapsed - commit_elapsed), 6)
        world["compute_performance"] = {
            "last_compute_seconds": round(sample, 6),
            "last_pipeline_seconds": round(sample, 6),
            "last_worker_seconds": round(worker_elapsed, 6),
            "last_commit_seconds": round(commit_elapsed, 6),
            "ema_compute_seconds": round(ema, 6),
            "safe_tick_seconds": safe,
            "headroom_factor": COMPUTE_HEADROOM_FACTOR,
            "sample_count": int(previous.get("sample_count", 0)) + 1,
            "breakdown_seconds": measured_breakdown,
        }
        world["clock"]["tick_seconds"] = max(
            int(world["clock"]["tick_seconds"]), safe)
        return safe

    def _commit_worker_result(
            self, result: dict, world: dict, *, refresh_html: bool,
            performance_started: float = None,
            worker_elapsed_seconds: float = None,
            performance_breakdown: dict = None) -> dict:
        commit_started = self.monotonic_fn()
        try:
            previous_stream = read_stream(self.stream_path)
        except (OSError, ValueError, json.JSONDecodeError):
            # streamは再生成可能な観察cache。壊れた旧cacheのために数値世界の
            # 正常な更新を失敗させず、完全keyframeから再開する。
            previous_stream = None
        stream = build_stream_snapshot(
            result["dashboard_data"], world,
            previous_snapshot=previous_stream)
        # live更新はkeyframe/deltaを同じiframeへ適用するため、巨大な埋め込み
        # HTMLを毎月再生成しない。新世界またはHTML欠損時だけshellを作り、
        # reloadしたclientも直後に最新streamへ同期する。
        if refresh_html or not self.html_path.exists():
            html = build_html(result["dashboard_data"], DEFAULT_TEMPLATE)
            _atomic_write(self.html_path, html)
        # HTMLとstreamを先に置き、その後にworldのrevisionを公開する。statusが
        # 新revisionを返した時点では対応keyframeが必ずatomicに読める。
        atomic_write_json(self.stream_path, stream)
        atomic_write_json(self.state_path, world)
        commit_finished = self.monotonic_fn()
        if performance_started is not None:
            previous_tick = int(world.get("clock", {}).get("tick_seconds", 1))
            self._record_compute_performance(
                world, max(0.0, commit_finished - performance_started),
                performance_breakdown,
                worker_elapsed_seconds=worker_elapsed_seconds,
                commit_elapsed_seconds=max(0.0, commit_finished - commit_started))
            if (not world.get("paused")
                    and int(world["clock"]["tick_seconds"]) > previous_tick):
                world["next_tick_at"] = max(
                    float(world.get("next_tick_at", 0.0) or 0.0),
                    float(world.get("last_advanced_at", self.time_fn()))
                    + int(world["clock"]["tick_seconds"]))
        # compute_performanceはworld本体を書いた後に確定する。数MiBのcheckpointを
        # 計測のためだけに二重保存せず、小さな同revision sidecarへ永続化する。
        self._write_runtime_metadata(world)
        self.last_error = None
        return self._publish_status(world)

    def refresh_dashboard_template(self) -> bool:
        """保存世界を進めず、既存の観察DATAを現在のHTMLへ載せ替える。

        アプリ更新後も停止・絶滅済みworldはworkerを二度と通らない。その場合も
        zoomや粉体rendererの更新を反映しつつ、checkpoint・revision・RNGを一切
        書き換えない。HTMLがまだ無い新規環境では何もしない。
        """
        with self.lock:
            if not self.state_path.exists() or not self.html_path.exists():
                return False
            # stateのschemaだけ検証する。読み込んだworldは意図的に保存しない。
            read_world(self.state_path)
            dashboard_data = extract_dashboard_data(
                self.html_path.read_text(encoding="utf-8"))
            dashboard_data.setdefault("meta", {})["aquarium_live"] = True
            _atomic_write(
                self.html_path, build_html(dashboard_data, DEFAULT_TEMPLATE))
            # v4以前の保存worldにも、再シミュレーション無しで最初のstream
            # keyframeを用意する。world/RNG/revision自体は書き換えない。
            stream_world = read_world(self.state_path)
            if not stream_world.get("stream_id"):
                stream_world["stream_id"] = self._legacy_stream_id(stream_world)
            if not stream_world.get("frame_sequence_end"):
                stream_world["frame_sequence_end"] = max(
                    LOGICAL_FRAMES_PER_CHUNK,
                    int(stream_world.get("revision", 1))
                    * LOGICAL_FRAMES_PER_CHUNK)
            completed_turn = int(
                stream_world.get("summary", {}).get("completed_turn", 0))
            stream_world.setdefault("frame_turn_start", completed_turn)
            stream_world.setdefault("frame_turn_end", completed_turn)
            atomic_write_json(
                self.stream_path,
                build_stream_snapshot(dashboard_data, stream_world))
            return True

    @staticmethod
    def _legacy_stream_id(world: dict) -> str:
        created = float(world.get("created_at", 0.0) or 0.0)
        seed = world.get("config", {}).get("values", {}).get("seed", 0)
        return f"aquarium-{int(created * 1_000_000):x}-{seed}"

    def start_world(self, values: dict, *, tick_seconds: int = 30,
                    months_per_tick: int = 1,
                    history_months: int = 2400,
                    initial_months: int = None) -> dict:
        tick_seconds, months_per_tick = self._validate_clock(
            tick_seconds, months_per_tick)
        self._validate_safe_tick_seconds(tick_seconds)
        try:
            history_months = int(history_months)
        except (TypeError, ValueError):
            raise ValueError("history_months must be an integer") from None
        if not MIN_HISTORY_MONTHS <= history_months <= MAX_HISTORY_MONTHS:
            raise ValueError(
                f"history_months must be between {MIN_HISTORY_MONTHS} and {MAX_HISTORY_MONTHS}")
        with self.lock:
            payload = {
                "action": "start", "values": values,
                "history_months": history_months}
            if initial_months is not None:
                try:
                    initial_months = int(initial_months)
                except (TypeError, ValueError):
                    raise ValueError("initial_months must be an integer") from None
                if not 1 <= initial_months <= 5000:
                    raise ValueError("initial_months must be between 1 and 5000")
                payload["initial_months"] = initial_months
            result = self._invoke(payload)
            now = self.time_fn()
            world = result["world"]
            world.update({
                "revision": 1,
                "paused": bool(world["summary"]["world_extinct"]),
                "created_at": now,
                "updated_at": now,
                "last_advanced_at": now,
                "next_tick_at": now + tick_seconds,
                "clock": {
                    "tick_seconds": tick_seconds,
                    "months_per_tick": months_per_tick,
                },
                "compute_performance": {
                    "last_compute_seconds": None,
                    "ema_compute_seconds": None,
                    "safe_tick_seconds": self.unmeasured_safe_tick_seconds,
                    "headroom_factor": COMPUTE_HEADROOM_FACTOR,
                    "sample_count": 0,
                },
            })
            world["stream_id"] = self._legacy_stream_id(world)
            world["frame_sequence_end"] = LOGICAL_FRAMES_PER_CHUNK
            completed_turn = int(world["summary"].get("completed_turn", 0))
            world["frame_turn_start"] = completed_turn
            world["frame_turn_end"] = completed_turn
            return self._commit_worker_result(
                result, world, refresh_html=True)

    def _advance_locked(self, world: dict, months: int, *, automatic: bool) -> dict:
        if world["summary"].get("world_extinct"):
            world["paused"] = True
            atomic_write_json(self.state_path, world)
            self._write_runtime_metadata(world)
            return self._publish_status(world)
        performance_started = self.monotonic_fn()
        result = self._invoke({"action": "advance", "world": world, "months": months})
        worker_elapsed = max(0.0, self.monotonic_fn() - performance_started)
        now = self.time_fn()
        updated = result["world"]
        updated["revision"] = int(world.get("revision", 0)) + 1
        updated["paused"] = bool(world.get("paused", False))
        updated["created_at"] = world.get("created_at", now)
        updated["updated_at"] = now
        updated["last_advanced_at"] = now
        updated["clock"] = dict(world["clock"])
        updated["stream_id"] = (
            world.get("stream_id") or self._legacy_stream_id(world))
        previous_turn = int(world.get(
            "summary", {}).get("completed_turn", 0))
        completed_turn = int(updated.get(
            "summary", {}).get("completed_turn", previous_turn))
        updated["frame_turn_start"] = min(
            completed_turn, previous_turn + 1)
        updated["frame_turn_end"] = completed_turn
        updated["frame_sequence_end"] = int(
            world.get(
                "frame_sequence_end",
                max(0, int(world.get("revision", 0)))
                * LOGICAL_FRAMES_PER_CHUNK)) + LOGICAL_FRAMES_PER_CHUNK
        if "compute_performance" in world:
            # world専用workerが保持するのは数値checkpoint+trace。時計・計測は
            # 親serverの責務なので、直前commitから明示的に継承する。
            updated["compute_performance"] = copy.deepcopy(
                world["compute_performance"])
        months_per_tick = max(1, int(updated["clock"]["months_per_tick"]))
        # catch-upを複数tickで割ると固定費を過小評価するため、通常の1更新と
        # 同じ月数だけをworker開始〜world commit完了の標本にする。
        measure_performance = months == months_per_tick
        if not measure_performance and "compute_performance" in world:
            updated["compute_performance"] = copy.deepcopy(
                world["compute_performance"])
        if updated["summary"].get("world_extinct"):
            updated["paused"] = True
        if not automatic:
            updated["next_tick_at"] = now + updated["clock"]["tick_seconds"]
        else:
            updated["next_tick_at"] = world["next_tick_at"]
        return self._commit_worker_result(
            result, updated, refresh_html=False,
            performance_started=(performance_started
                                 if measure_performance else None),
            worker_elapsed_seconds=worker_elapsed,
            performance_breakdown=result.get("timings"))

    def advance(self, months: int) -> dict:
        try:
            months = int(months)
        except (TypeError, ValueError):
            raise ValueError("months must be an integer") from None
        if months < 1 or months > 5000:
            raise ValueError("months must be between 1 and 5000")
        with self.lock:
            world = self._read_world()
            if world is None:
                raise ValueError("aquarium world has not been started")
            return self._advance_locked(world, months, automatic=False)

    def pause(self) -> dict:
        with self.lock:
            world = self._read_world()
            if world is None:
                raise ValueError("aquarium world has not been started")
            world["paused"] = True
            world["updated_at"] = self.time_fn()
            # revisionは数値world/keyframeの版を表す。時計の停止だけで進めると
            # 対応するstreamが存在しないrevisionをstatusが先に公開してしまう。
            atomic_write_json(self.state_path, world)
            self._write_runtime_metadata(world)
            return self._publish_status(world)

    def resume(self) -> dict:
        with self.lock:
            world = self._read_world()
            if world is None:
                raise ValueError("aquarium world has not been started")
            if world["summary"].get("world_extinct"):
                raise ValueError("an extinct world cannot be resumed")
            now = self.time_fn()
            minimum = self._minimum_tick_seconds(world)
            world["clock"]["tick_seconds"] = max(
                int(world["clock"]["tick_seconds"]), minimum)
            world["paused"] = False
            world["updated_at"] = now
            world["next_tick_at"] = now + world["clock"]["tick_seconds"]
            atomic_write_json(self.state_path, world)
            self._write_runtime_metadata(world)
            return self._publish_status(world)

    def advance_if_due(self) -> dict | None:
        with self.lock:
            world = self._read_world()
            if world is None or world.get("paused") or world["summary"].get("world_extinct"):
                return None
            now = self.time_fn()
            next_tick = float(world.get("next_tick_at", now + 1))
            if now < next_tick:
                return None
            minimum = self._minimum_tick_seconds(world)
            configured_tick_seconds = int(
                world["clock"]["tick_seconds"])
            tick_seconds = max(configured_tick_seconds, minimum)
            world["clock"]["tick_seconds"] = tick_seconds
            # 既存設定を安全下限まで引き上げる最初の更新では、古い短周期で
            # 積み上がったcatch-upをまとめて実行せず、1更新だけで実測する。
            if tick_seconds != configured_tick_seconds:
                next_tick = now
            months_per_tick = world["clock"]["months_per_tick"]
            due_ticks = int(math.floor((now - next_tick) / tick_seconds)) + 1
            max_ticks = max(1, MAX_CATCHUP_MONTHS // months_per_tick)
            run_ticks = min(due_ticks, max_ticks)
            months = run_ticks * months_per_tick
            # 正常完了した区間だけ時計を進める。worker失敗時はworldファイルが
            # 変わらず、次回ループで同じ区間を再試行できる。
            status = self._advance_locked(world, months, automatic=True)
            committed = self._read_world()
            committed_tick_seconds = int(
                committed["clock"]["tick_seconds"])
            if committed_tick_seconds != tick_seconds:
                committed["next_tick_at"] = (
                    self.time_fn() + committed_tick_seconds)
            else:
                committed["next_tick_at"] = (
                    next_tick + run_ticks * tick_seconds)
            atomic_write_json(self.state_path, committed)
            self._write_runtime_metadata(committed)
            return self._publish_status(committed)

    def _publish_status(self, world: dict) -> dict:
        """commit済みworldから小さな観察snapshotを公開する。"""
        published = self._status_from_world(world)
        self._status_cache = copy.deepcopy(published)
        return copy.deepcopy(published)

    def _status_from_world(self, world: dict) -> dict:
        return {
            "exists": True,
            "revision": world.get("revision", 0),
            "paused": bool(world.get("paused", True)),
            "running": (not world.get("paused", True)
                        and not world["summary"].get("world_extinct", False)),
            "summary": world["summary"],
            "clock": world.get("clock", {}),
            "minimum_tick_seconds": self._minimum_tick_seconds(world),
            "compute_performance": dict(
                world.get("compute_performance", {})),
            "created_at": world.get("created_at"),
            "updated_at": world.get("updated_at"),
            "next_tick_at": world.get("next_tick_at"),
            "last_error": self.last_error,
            "dashboard_available": self.html_path.exists(),
            "stream_available": self.stream_path.exists(),
        }

    def status(self) -> dict:
        # worldは_atomic_write()で丸ごと置換されるため、読み手は更新lockを
        # 待たずに直前または直後の完全なsnapshotを読める。worker計算中にも
        # status pollingを返し、観察画面の時計と将来のframe bufferを止めない。
        if self._status_cache is not None:
            cached = copy.deepcopy(self._status_cache)
            cached["last_error"] = self.last_error
            return cached
        try:
            world = self._read_world()
        except Exception as exc:
            return {
                "exists": False, "error": str(exc),
                "last_error": self.last_error,
                "minimum_tick_seconds": self.unmeasured_safe_tick_seconds,
            }
        if world is None:
            return {
                "exists": False, "last_error": self.last_error,
                "minimum_tick_seconds": self.unmeasured_safe_tick_seconds,
            }
        return self._publish_status(world)

    def resident_detail(self, resident_id: str) -> dict:
        """現在checkpointから1人分だけを観察API用に取り出す。"""
        if not isinstance(resident_id, str) or not resident_id:
            raise ValueError("resident id is required")
        if len(resident_id) > 128:
            raise ValueError("resident id is too long")
        # status()と同じくatomic snapshotを読むだけなので更新lockは不要。
        # 返却用dictは下でコピーし、保存中のworldを呼び出し側へ露出しない。
        world = self._read_world()
        if world is None:
            raise ValueError("aquarium world has not been started")
        checkpoint = world.get("checkpoint", {})
        registry = checkpoint.get("resident_registry", {})
        resident = registry.get("residents", {}).get(resident_id)
        if resident is None:
            raise KeyError(resident_id)
        household = registry.get("households", {}).get(
            resident.get("household_id"))
        spatial = checkpoint.get("spatial_state", {})
        position = spatial.get("residents", {}).get(resident_id)
        site = (spatial.get("sites", {}).get(position.get("site_id"))
                if position else None)
        cluster_id = None
        if position:
            for candidate_id, cluster in spatial.get(
                    "clusters", {}).items():
                if position.get("site_id") in cluster.get("site_ids", ()):
                    cluster_id = candidate_id
                    break
        organization_rows = []
        for organization in checkpoint.get(
                "organization_state", {}).get(
                    "organizations", {}).values():
            if (not organization.get("active", True)
                    or resident_id not in organization.get(
                        "named_member_ids", ())):
                continue
            organization_rows.append({
                "id": organization.get("id"),
                "kind": organization.get("kind"),
                "purpose": organization.get("purpose"),
                "work_relationship": organization.get(
                    "work_relationship"),
                "is_worker": resident_id in organization.get(
                    "named_worker_ids", ()),
                "size": organization.get("size"),
                "trust_stage": organization.get("trust_stage"),
                "operating_status": organization.get("operating_status"),
                "asset_claims": dict(organization.get(
                    "asset_claims", {})),
                "asset_claim_total": organization.get(
                    "asset_claim_total", 0.0),
            })
        organization_rows.sort(key=lambda row: str(row["id"]))
        return {
            "revision": world.get("revision", 0),
            "turn": world.get("summary", {}).get("completed_turn"),
            "resident": {
                **resident,
                "parent_ids": list(resident.get("parent_ids", ())),
            },
            "household": dict(household) if household else None,
            "position": dict(position) if position else None,
            "site": dict(site) if site else None,
            "activity_community_id": cluster_id,
            "organizations": organization_rows,
        }

    def frame_chunk(self, *, client_stream_id: str = None,
                    client_revision: int = None,
                    after_sequence: int = None,
                    limit: int = 150) -> dict:
        """最新のatomic stream snapshotから連番付きframeを返す。"""
        snapshot = read_stream(self.stream_path)
        if snapshot is None:
            return {"schema_version": 1, "exists": False}
        return stream_chunk(
            snapshot,
            client_stream_id=client_stream_id,
            client_revision=client_revision,
            after_sequence=after_sequence,
            limit=limit)

    def _loop(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                self.advance_if_due()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)

    def start_background(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        try:
            self.refresh_dashboard_template()
        except Exception as exc:
            # 旧HTMLが壊れていてもAPIと時計は起動する。次の正常なstart/advanceで
            # workerがHTMLを丸ごと置き換えられる。
            self.last_error = f"dashboard refresh failed: {exc}"
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._loop, name="life-game-aquarium", daemon=True)
        self.thread.start()

    def stop_background(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self._stop_worker()
