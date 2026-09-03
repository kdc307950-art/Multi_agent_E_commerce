"""回调签名/重放保护。

外部资金网关的回调以「raw body + HMAC-SHA256 签名」传达。本模块：
- `sign_payload` / `build_callback_signature`：生成签名（供测试/沙箱构造回调）；
- `verify_hmac_signature`：常量时间比对，防时序侧信道。

回调重放防护放在 engine.apply_callback：以 (execution_id, nonce) 做 CAS，第一次生效，
后续同 nonce 一律视为重放（只重放不重复生效）；不同 nonce 视为冲突 → 转人工对账。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from src.execution.types import ExecutionStatus


def sign_payload(secret: str, payload: bytes | str) -> str:
    """按 HMAC-SHA256 计算签名（十六进制）。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_callback_signature(secret: str, payload: dict) -> str:
    """对回调 JSON 载荷（紧凑序列化）签名，用于构造测试/回放载荷。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sign_payload(secret, body)


def verify_hmac_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    """常量时间校验回调签名。签名缺失/为空一律 False（fail-closed）。"""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def normalize_callback_payload(record_status: str, payload: dict) -> dict:
    """把外部回调状态映射为执行状态机可接受的下一态（顺带做基础合法性检查）。

    外部 status 取值约定：succeeded / failed / processing / timeout。仅 succeeded / failed
    会触发状态跃迁；其余视为中间态（在 SUBMITTED 保持，等待最终回调）。
    """
    ext_status = payload.get("status")
    if ext_status in ("succeeded", "success", "confirmed"):
        return {"next": ExecutionStatus.CONFIRMED.value}
    if ext_status in ("failed", "rejected", "refunded"):
        return {"next": ExecutionStatus.FAILED_DISPATCHED.value}
    # 其它（processing/pending/unknown）：保持 submitted，不跃迁（等待最终回调）。
    return {"next": record_status}
