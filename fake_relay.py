#!/usr/bin/env python3
"""fake_relay — 自测用的「假中转」本地服务器（仅供 relay-check 内部自测）。

它模拟一个掺假的中转站：
  - 接受 OpenAI 格式的 POST /v1/chat/completions；
  - 把请求转发给一个真实上游（由环境变量配置）；
  - 把返回的 model 字段改写成另一个模型名（冒充）；
  - 把返回的 usage 数字按倍数放大（虚报计费）。

配置全部走环境变量：
  FAKE_UPSTREAM_URL    上游聊天接口的完整 URL（必填），
                       如 http://127.0.0.1:9000/v1/chat/completions
  FAKE_UPSTREAM_KEY    上游的 Key（可选；只用于转发请求头，绝不打印/记录）
  FAKE_UPSTREAM_MODEL  转发时改写的请求 model（可选；不设则透传原请求）
  FAKE_SPOOF_MODEL     返回时改写的 model 字段（可选；不设则不改）
  FAKE_USAGE_INFLATE   usage 放大倍数（可选，默认 1.0；如 1.5 表示虚报 50%）
  FAKE_RELAY_PORT      本服务监听端口（可选，默认 8080）

仅使用 Python 标准库。本服务只应在本机自测时运行，不要对外暴露。
"""

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class FakeRelayHandler(BaseHTTPRequestHandler):
    # 关掉默认的请求日志，避免任何敏感信息进日志。
    def log_message(self, fmt, *args):  # noqa: A003 - 父类签名
        pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        upstream_url = os.environ.get("FAKE_UPSTREAM_URL")
        if not upstream_url:
            self._send(500, b"FAKE_UPSTREAM_URL not set", "text/plain; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, b"invalid json", "text/plain; charset=utf-8")
            return

        upstream_model = os.environ.get("FAKE_UPSTREAM_MODEL")
        if upstream_model:
            body["model"] = upstream_model

        headers = {"Content-Type": "application/json"}
        upstream_key = os.environ.get("FAKE_UPSTREAM_KEY")
        if upstream_key:
            headers["Authorization"] = "Bearer " + upstream_key  # 只进请求头，绝不记录

        req = urllib.request.Request(
            upstream_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.status
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except (urllib.error.URLError, TimeoutError, OSError):
            self._send(502, b"upstream unreachable", "text/plain; charset=utf-8")
            return

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._send(status, payload.encode("utf-8"), "application/json; charset=utf-8")
            return

        spoof_model = os.environ.get("FAKE_SPOOF_MODEL")
        if spoof_model:
            data["model"] = spoof_model

        inflate = env_float("FAKE_USAGE_INFLATE", 1.0)
        usage = data.get("usage")
        if inflate != 1.0 and isinstance(usage, dict):
            for field in ("prompt_tokens", "completion_tokens", "total_tokens",
                          "input_tokens", "output_tokens"):
                value = usage.get(field)
                if isinstance(value, (int, float)):
                    usage[field] = int(round(value * inflate))

        # 只记录路径与状态码，不记录请求头、请求体或任何 Key。
        print(f"fake_relay: POST /v1/chat/completions -> {status}", file=sys.stderr)
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status, body_bytes, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def main():
    port = int(os.environ.get("FAKE_RELAY_PORT", "8080"))
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeRelayHandler)
    print(f"fake_relay listening on http://127.0.0.1:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
