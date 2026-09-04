#!/usr/bin/env python3
# login_and_run_api.py —— 用运行时注入的明文登录凭据登录拿 JWT，再驱动 run_api_drills.py。
#
# 用途：D3/D4/D5/D6 需要 PREVIEW_TOKEN（白名单租户 admin/approver 的真实 JWT）。本脚本从
#       **运行时注入**的明文登录取得口令（不再读取任何明文凭据文件，也不再硬编码），
#       POST /api/auth/login 签发 JWT，然后以该 token 运行 run_api_drills.py <scenario>。
#       不打印任何口令。
#
# 明文注入方式（二选一，不落日志）：
#   1) 环境变量 PREVIEW_LOGIN_CREDENTIALS = JSON 对象 {"<tenant_id>:<user_id>": "<明文>"}
#      例如（PowerShell）： $env:PREVIEW_LOGIN_CREDENTIALS='{"TENANT-A:APPROVER-A":"S3cret!"}'
#   2) 未提供该变量时，交互式用 getpass 提示输入口令（无回显）。
#
# 注意：服务端校验用的是 AUTH_LOGIN_CREDENTIALS（PHC 哈希，argon2id/bcrypt），
#       本脚本**不读取**它；这里注入的是**登录明文**（仅本次运行用，不回显、不写文件、不打印）。
#
# 用法（仓库根 + 已启动 preview 栈；LLM_BACKEND=mock 可驱动到 approval_required/accepted/done）：
#   python deploy/drills/pwsh-runbook/login_and_run_api.py approval   # 或 sse / concurrent / reconcile
#   可选：$env:PREVIEW_LOGIN_USER='TENANT-A:APPROVER-A'（默认）
#
# 退出码：透传 run_api_drills.py（0=PASS；1=FAIL；2=BLOCKED 前置未满足；3=参数/登录失败）。
# 若 LLM 无法驱动预期事件，run_api_drills.py 会因得不到预期事件而返回 BLOCKED — 这是如实标注，不是伪造 PASS。

import getpass
import json
import os
import ssl
import sys
import subprocess
import urllib.request

BASE = os.environ.get("PREVIEW_BASE", "https://127.0.0.1").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
LOGIN_USER = os.environ.get("PREVIEW_LOGIN_USER", "TENANT-A:APPROVER-A").strip()

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def load_all_creds() -> dict[str, str]:
    """从环境变量 `PREVIEW_LOGIN_CREDENTIALS`（JSON 对象）读取明文表；无则返回 {}。

    绝不回显/打印口令内容。失败返回空 dict，由调用方转 BLOCKED。
    """
    raw = os.environ.get("PREVIEW_LOGIN_CREDENTIALS", "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print("[登录失败] PREVIEW_LOGIN_CREDENTIALS 非法 JSON：%s" % exc)
        return {}
    if not isinstance(obj, dict):
        print("[登录失败] PREVIEW_LOGIN_CREDENTIALS 应为 JSON 对象 {\"<tenant>:<user>\": \"<明文>\"}。")
        return {}
    return {str(k): str(v) for k, v in obj.items()}


def credential_for(user_key: str) -> str:
    """返回 user_key 的明文口令：优先环境变量表，否则 getpass 交互（无回显、不落日志）。"""
    creds = load_all_creds()
    if user_key in creds:
        return creds[user_key]
    try:
        return getpass.getpass("请输入 %s 的明文口令（不落日志）：" % user_key)
    except EOFError:
        return ""


def login():
    if ":" not in LOGIN_USER:
        print("[参数错误] PREVIEW_LOGIN_USER 应为 '<tenant>:<user>'（如 TENANT-A:APPROVER-A）。")
        return None
    tid, uid = LOGIN_USER.split(":", 1)
    secret = credential_for(LOGIN_USER)
    if not secret:
        print("[登录失败] 未为 %s 提供明文口令（设置 PREVIEW_LOGIN_CREDENTIALS 或交互输入）。"
              % LOGIN_USER)
        return None
    body = json.dumps({"tenant_id": tid, "user_id": uid, "credential": secret}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ctx, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]
    except Exception as e:  # noqa: BLE001
        print("[登录失败] 无法签发 JWT（可能 LLM/认证未就绪或栈未全起）：%s" % e)
        return None


def main():
    scen = sys.argv[1] if len(sys.argv) > 1 else ""
    if scen not in ("approval", "sse", "concurrent", "reconcile"):
        print("用法: login_and_run_api.py {approval|sse|concurrent|reconcile}")
        return 3
    token = login()
    if not token:
        # 登录失败（认证/栈未就绪或未注入口令）→ 视为 BLOCKED（前置未满足），不伪造 PASS。
        print("[BLOCKED] 无法登录获取 JWT；D3-D6 前置未满足（需审计认证可用 + 可达 LLM 或 mock 驱动事件，且已注入明文口令）。")
        rec = {"scenario": "D_api_%s" % scen, "result": "BLOCKED", "ok": False,
               "detail": {"reason": "login_failed_need_reachable_llm_or_auth"}}
        os.makedirs(os.path.join(HERE, "..", "records"), exist_ok=True)
        with open(os.path.join(HERE, "..", "records", "drill-api-%s.json" % scen), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        return 2
    env = dict(os.environ, PREVIEW_TOKEN=token)
    return subprocess.call([sys.executable, os.path.join(HERE, "run_api_drills.py"), scen], env=env)


if __name__ == "__main__":
    sys.exit(main())
