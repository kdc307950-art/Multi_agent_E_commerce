#!/usr/bin/env python3
"""t4/t11 演练用——Alertmanager webhook MOCK 接收端（自托管、仅标准库）。

用途：作为"受控通道"的自托管接收方，接收 Alertmanager 推送的 webhook 通知；把收到的告警
按 JSONL 追加落盘（`SINK_OUT`）并返回 200，用于证明「告警能触发 → 路由 → 通知」闭环。

本文件既是 obs 栈内 alert-webhook-sink 服务的运行时入口（compose 挂载），也是 t11 告警演练可独立
运行的 mock 接收端。

纪律：
  * 仅用 Python 标准库（http.server），无第三方依赖、无需构建；
  * 监听 0.0.0.0:${SINK_PORT:-8080}，只在 obs 内网对 alertmanager 可达（obs network internal:true）；
  * 收到任何请求一律 200（仅留痕，不转发；不接第三方托管 SaaS）。

用法（容器内）：
  SINK_OUT=/data/inbox.jsonl SINK_PORT=8080 python /app/mock_webhook_receiver.py
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = os.environ.get("SINK_OUT", "/data/inbox.jsonl")
PORT = int(os.environ.get("SINK_PORT", "8080"))


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception as exc:  # noqa: BLE001
            body = {"_parse_error": str(exc)}
        line = json.dumps({"ts": time.time(), "path": self.path, "body": body}, ensure_ascii=False)
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print("[sink] received webhook:", line, flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alert-webhook-sink")

    def log_message(self, *args) -> None:  # 静默
        return


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    print(f"[sink] listening on 0.0.0.0:{PORT}, out={OUT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), _Handler).serve_forever()
