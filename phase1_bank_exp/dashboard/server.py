#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ローカル/LAN用の実験コンソール。

使い方:
    python dashboard/server.py
    # ブラウザで http://127.0.0.1:8765/

    # 信頼できるLAN内の他端末から使う場合だけ明示的に公開
    python dashboard/server.py --host 0.0.0.0
    # 他端末から http://<このPCのLAN IPv4>:8765/

任意コマンド・任意パスは受け取らない。runは隔離workerへ渡し、グローバルRNGや
実験定数のrun間干渉を防ぐ。永続水槽の更新だけはAquariumManagerのlockで直列化し、
HTTP応答とバックグラウンド時計が同じworldを同時更新しない。
ブラウザからの更新は、localhostまたはプライベートIPv4の同一オリジンに限定する。
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, unquote, urlsplit

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_DIR = DASHBOARD_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard.build_dashboard import DEFAULT_TEMPLATE, build_html  # noqa: E402
from dashboard.aquarium_runtime import (  # noqa: E402
    AquariumManager,
    DEFAULT_HTML_PATH as DEFAULT_AQUARIUM_HTML_PATH,
    DEFAULT_STATE_PATH as DEFAULT_AQUARIUM_STATE_PATH,
)
from dashboard.experiment_parameters import parameter_schema  # noqa: E402


APP_PATH = DASHBOARD_DIR / "app.html"
BUILT_DASHBOARD_PATH = DASHBOARD_DIR / "life_ledger.html"
WORKER_PATH = DASHBOARD_DIR / "experiment_worker.py"
DEFAULT_REPORT_DIR = Path(os.environ.get(
    "LIFE_GAME_SWEEP_REPORT_DIR", DASHBOARD_DIR / "reports"))
MAX_REQUEST_BYTES = 128 * 1024
WORKER_TIMEOUT_SECONDS = 120


def _allowed_local_host(hostname: str | None, *, allow_unspecified: bool = False) -> bool:
    """localhostとLAN用IPv4だけを許可する。公開IPv4/ホスト名は拒否。"""
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if address.version != 4:
        return False
    return (address.is_loopback or address.is_private or address.is_link_local
            or (allow_unspecified and address.is_unspecified))


def allowed_bind_host(hostname: str) -> bool:
    """CLIから指定できるbind先をlocalhost/LANに限定する。"""
    return _allowed_local_host(hostname, allow_unspecified=True)


def request_origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """LAN IPを固定列挙せず、HTTPの同一オリジンだけを許可する。"""
    if not origin:
        return True  # curl等のブラウザ外クライアント
    if not host_header:
        return False
    try:
        origin_parts = urlsplit(origin)
        request_parts = urlsplit(f"//{host_header}")
        origin_port = origin_parts.port or 80
        request_port = request_parts.port or 80
    except ValueError:
        return False
    origin_host = origin_parts.hostname
    request_host = request_parts.hostname
    return (
        origin_parts.scheme == "http"
        and origin_parts.username is None
        and origin_parts.password is None
        and _allowed_local_host(origin_host)
        and origin_host.lower() == request_host.lower()
        and origin_port == request_port
    )


def resolve_report_path(report_dir: Path, encoded_name: str) -> Path | None:
    """basenameの自己完結HTMLだけをreport_dir内から解決する。"""
    name = unquote(encoded_name)
    if not name or Path(name).name != name or not name.endswith(".html"):
        return None
    resolved_dir = report_dir.resolve()
    candidate = (resolved_dir / name).resolve()
    if candidate.parent != resolved_dir or not candidate.is_file():
        return None
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "LifeGameExperiment/2"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data: dict) -> None:
        self._send(
            status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8")

    def _report_dir(self) -> Path:
        return self.server.report_dir

    def _aquarium(self) -> AquariumManager:
        return self.server.aquarium_manager

    def _send_report_index(self) -> None:
        report_dir = self._report_dir()
        reports = sorted(report_dir.glob("barter_sweep_*.html"), reverse=True)
        links = "".join(
            f'<li><a href="/sweeps/{html.escape(path.name)}">'
            f'{html.escape(path.stem)}</a></li>' for path in reports)
        if not links:
            links = "<li>まだレポートがありません。PCでbatch_sweep.pyを実行してください。</li>"
        body = ("<!doctype html><html lang=ja><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>走査レポート</title><style>body{font:16px system-ui;"
                "max-width:820px;margin:40px auto;padding:0 20px}li{margin:12px 0}</style>"
                "<h1>パラメータ走査レポート</h1><p><a href='/'>実験コンソールへ戻る</a>"
                f"</p><ul>{links}</ul></html>")
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _report_path(self, encoded_name: str) -> Path | None:
        return resolve_report_path(self._report_dir(), encoded_name)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, APP_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/schema":
            self._send_json(200, parameter_schema())
        elif path == "/api/aquarium/status":
            self._send_json(200, self._aquarium().status())
        elif path == "/api/aquarium/frames":
            query = parse_qs(parsed.query)
            try:
                def optional_one(name):
                    values = query.get(name, [])
                    if len(values) > 1:
                        raise ValueError(f"{name} must occur at most once")
                    return values[0] if values else None

                chunk = self._aquarium().frame_chunk(
                    client_stream_id=optional_one("stream_id"),
                    client_revision=optional_one("revision"),
                    after_sequence=optional_one("after"),
                    limit=optional_one("limit") or 150)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            else:
                self._send_json(200, chunk)
        elif path == "/api/aquarium/resident":
            resident_ids = parse_qs(parsed.query).get("id", [])
            if len(resident_ids) != 1:
                self._send_json(400, {"error": "exactly one resident id is required"})
            else:
                try:
                    detail = self._aquarium().resident_detail(resident_ids[0])
                except KeyError:
                    self._send_json(404, {"error": "resident not found"})
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                else:
                    self._send_json(200, detail)
        elif path == "/aquarium.html" and self._aquarium().html_path.exists():
            self._send(
                200, self._aquarium().html_path.read_bytes(),
                "text/html; charset=utf-8")
        elif path == "/life_ledger.html" and BUILT_DASHBOARD_PATH.exists():
            self._send(200, BUILT_DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path in ("/sweeps", "/sweeps/"):
            self._send_report_index()
        elif path.startswith("/sweeps/"):
            report_path = self._report_path(path[len("/sweeps/"):])
            if report_path is None:
                self._send_json(404, {"error": "report not found"})
            else:
                self._send(200, report_path.read_bytes(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        allowed_paths = {
            "/api/run", "/api/aquarium/start", "/api/aquarium/pause",
            "/api/aquarium/resume", "/api/aquarium/advance",
        }
        if path not in allowed_paths:
            self._send_json(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        if not request_origin_allowed(origin, self.headers.get("Host")):
            self._send_json(403, {"error": "origin is not allowed"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "request body is empty or too large"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid UTF-8 JSON"})
            return

        if path == "/api/run":
            try:
                proc = subprocess.run(
                    [sys.executable, str(WORKER_PATH)], cwd=PROJECT_DIR,
                    input=json.dumps(payload, ensure_ascii=False), text=True,
                    capture_output=True, encoding="utf-8", timeout=WORKER_TIMEOUT_SECONDS,
                    check=False)
            except subprocess.TimeoutExpired:
                self._send_json(504, {"error": "simulation timed out"})
                return
            if proc.returncode != 0:
                detail = (proc.stderr.strip().splitlines()[-1]
                          if proc.stderr.strip() else "worker failed")
                self._send_json(400, {"error": detail})
                return
            try:
                dashboard_data = json.loads(proc.stdout)
                dashboard_html = build_html(dashboard_data, DEFAULT_TEMPLATE)
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(500, {"error": f"dashboard build failed: {exc}"})
                return
            self._send(
                200, dashboard_html.encode("utf-8"), "text/html; charset=utf-8")
            return

        try:
            if path == "/api/aquarium/start":
                status = self._aquarium().start_world(
                    payload.get("values", {}),
                    tick_seconds=payload.get("tick_seconds", 30),
                    months_per_tick=payload.get("months_per_tick", 1),
                    history_months=payload.get("history_months", 2400),
                    initial_months=payload.get("initial_months", 12))
            elif path == "/api/aquarium/pause":
                status = self._aquarium().pause()
            elif path == "/api/aquarium/resume":
                status = self._aquarium().resume()
            else:
                status = self._aquarium().advance(payload.get("months", 1))
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "aquarium simulation timed out"})
            return
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, status)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind先IPv4。LAN公開時だけ0.0.0.0を指定(既定: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--report-dir", type=Path, default=DEFAULT_REPORT_DIR,
        help="batch_sweep.pyが生成したHTMLレポートの読取専用ディレクトリ")
    parser.add_argument(
        "--aquarium-state", type=Path, default=DEFAULT_AQUARIUM_STATE_PATH,
        help="永続水槽のJSON状態ファイル")
    parser.add_argument(
        "--aquarium-html", type=Path, default=DEFAULT_AQUARIUM_HTML_PATH,
        help="永続水槽の最新観察HTML")
    args = parser.parse_args()
    if not allowed_bind_host(args.host):
        parser.error("--host must be localhost, 0.0.0.0, or a private/loopback IPv4 address")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.report_dir = args.report_dir
    server.aquarium_manager = AquariumManager(
        args.aquarium_state, args.aquarium_html)
    server.aquarium_manager.start_background()
    if args.host == "0.0.0.0":
        print(f"Life Game experiment console (LAN): "
              f"http://<このPCのLAN IPv4>:{server.server_port}/")
        print("信頼できるLAN内限定で使用してください。")
    else:
        print(f"Life Game experiment console: http://{args.host}:{server.server_port}/")
    print("停止: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
    finally:
        server.aquarium_manager.stop_background()
        server.server_close()


if __name__ == "__main__":
    main()
