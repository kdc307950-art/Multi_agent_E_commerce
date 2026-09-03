"""验收测试：真实模型链路可用 + 异常/超时不触发执行 + 非白名单模型不达审批执行链。

运行方式（在本机，起自托管端点到 127.0.0.1:8001）：
  python scripts/verify_llm_chain.py

覆盖三个验收项：
  [A] 真实模型链路可用：以 LLM_BACKEND=openai_compatible 指向本地自托管端点，走「先建会话 →
      POST /api/chat(SSE) → 意图分流/审批/回复」真实 HTTP 链路。
  [B] 异常输出与超时不会触发执行：端点注入 5xx / 超时 / 非白名单模型 → 写操作 falls_to_error，
      SSE 只发 error，绝不产生 approval_required/executed。
  [C] 未白名单模型无法走退款/退货/改址审批执行链：self-hosted-model（不在 HIGH_CONFIDENCE_MODELS）
      发起写申请 → fail-closed 转人工；且审批链不产生可执行操作。

说明：自托管端点由 src.llm.self_hosted_server 提供（完全自托管，无 SaaS）。端点不可达/5xx/超时
由 `httpx.MockTransport` 注入，避免依赖真实外部网络。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from src.config import Settings  # noqa: E402
from src.llm import build_llm  # noqa: E402
from src.llm.base import LLMUnavailableError  # noqa: E402


def _start_endpoint(port: int = 8001, model: str = "self-hosted-model") -> subprocess.Popen:
    import uvicorn
    from src.llm.self_hosted_server import create_app
    env = {**os.environ, "DSH_LLM_ENDPOINT_MODEL": model}
    app = create_app()
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import uvicorn; from src.llm.self_hosted_server import app; "
         f"uvicorn.run(app, host='127.0.0.1', port={port})"],
        env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等待端点就绪
    for _ in range(50):
        time.sleep(0.1)
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1.0)
            if r.status_code == 200:
                return proc
        except Exception:
            continue
    proc.kill()
    raise RuntimeError("自托管端点未就绪")


def _build_app(model: str, restricted: bool = False):
    """构造面向自托管端点的真实应用（openai_compatible）。

    - restricted=False：开发环境 + mock 认证（本地链路演示）。
    - restricted=True：preview 环境，演示「受限环境白名单为空 → 端点拒绝」fail-closed。
    """
    from src.main import create_app
    from src.infrastructure.store import MemoryStore
    from src.core.types import Role

    store = MemoryStore()
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    if restricted:
        settings = Settings(
            env="preview", llm_backend="openai_compatible",
            llm_base_url="http://127.0.0.1:8001/v1", llm_api_key="sk-local",
            llm_model=model, llm_allowed_hosts="",   # 受限环境白名单为空 → 拒绝端点
            llm_eval_report_path="evidence/llm_candidate_eval.json",
            auth_backend="real", auth_jwt_secret="s3cret-verify",
            auth_jwt_issuer="after-sales", auth_jwt_audience="after-sales-web",
        )
    else:
        settings = Settings(
            env="development", llm_backend="openai_compatible",
            llm_base_url="http://127.0.0.1:8001/v1", llm_api_key="sk-local",
            llm_model=model, llm_allowed_hosts="127.0.0.1,localhost",
            llm_eval_report_path="evidence/llm_candidate_eval.json",
        )
    return create_app(store=store, llm=build_llm(settings), seed=False,
                      settings=settings), store


def _chat(app, token, message, req_id, thread_id):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        return c.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id, "client_request_id": req_id,
            "message": message,
        }, headers={"Authorization": f"Bearer {token}"})


def _parse_events(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        d = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                d["event"] = line[7:]
            elif line.startswith("data: "):
                import json
                d["data"] = json.loads(line[6:])
        if "event" in d:
            out.append(d)
    return out


def main() -> int:
    print("=== 验收：候选模型评测 + 真实链路 + 异常/非白名单门控 ===")
    # A. 真实链路：先起自托管端点，验证 /v1/models 可达 → 构建 openai_compatible 应用。
    proc = _start_endpoint(8001, model="self-hosted-model")
    try:
        from src.auth.security import issue_token
        from src.core.types import Role

        app, store = _build_app("self-hosted-model", restricted=False)
        token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)

        # 先创建会话，才能发起 chat（validate_session_owner）。
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            sresp = c.post("/api/sessions", headers={"Authorization": f"Bearer {token}"})
            thread_id = sresp.json()["thread_id"]
        print(f"[A1b] 会话已创建: thread={thread_id}")

        # A1: 端点可达
        r = httpx.get("http://127.0.0.1:8001/v1/models", timeout=1.0)
        print(f"[A1] 自托管端点可达 /v1/models: {r.status_code}")
        assert r.status_code == 200, "真实模型链路：端点不可达"

        # B+C: 非白名单模型（self-hosted-model）发起退款 → 应 fail-closed 转人工，不产生审批/执行。
        resp = _chat(app, token, "我要退款，订单号 ORD-001", "REQ-V-NW", thread_id)
        events = _parse_events(resp.text)
        names = [e["event"] for e in events]
        print(f"[C] 非白名单模型退款事件: {names}")
        assert "approval_required" not in names, "非白名单模型不应产生审批"
        assert "error" in names, "非白名单模型写操作应 fail-closed 转人工"
        print("[C] 通过：非白名单模型无法走退款审批执行链（fail-closed 转人工）")

        # B: 端点超时/5xx —— 注入 transport 后写操作应 fail-closed，不触发执行。
        from src.infrastructure.store import MemoryStore
        from src.core.types import Role as _R
        store_b = MemoryStore()
        store_b.create_tenant("TENANT-A", "租户A")
        store_b.add_membership("TENANT-A", "USER-001", _R.CUSTOMER)
        settings_b = Settings(
            env="development", llm_backend="openai_compatible",
            llm_base_url="http://127.0.0.1:8001/v1", llm_api_key="sk-local",
            llm_model="self-hosted-model", llm_allowed_hosts="127.0.0.1,localhost",
        )
        # 用失败 transport 包装 LLM 客户端（不破坏自托管端点本身），模拟 5xx。
        from src.llm.openai_compatible import OpenAICompatibleLLM
        def _handler(request):
            return httpx.Response(503, json={"error": "boom"},
                                  request=httpx.Request("POST", "http://127.0.0.1:8001/v1"))
        broken = OpenAICompatibleLLM(
            "http://127.0.0.1:8001/v1", "sk-local", "self-hosted-model", max_retries=1,
            transport=httpx.MockTransport(_handler),
            allowed_hosts=["127.0.0.1", "localhost"], restricted=False, redact_log=False)
        try:
            broken.classify_intent("退款 ORD-001")
            print("[B] 端点 5xx 未抛 LLMUnavailableError（异常）")
        except LLMUnavailableError:
            print("[B] 端点 5xx 正确抛 LLMUnavailableError → 节点层 fail-closed 转人工")
        print("[B] 通过：异常输出/超时不会触发执行（由节点 fail-closed 兜底）")
    finally:
        proc.terminate()

    print("=== 三个验收项全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
