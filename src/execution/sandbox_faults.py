"""网关沙箱的可复用故障注入原语（供后续端到端验证与故障注入测试复用）。

完全自托管红线：本模块只是沙箱网关与测试的**确定性故障注入工具**，不触真实资金，
也不接入任何第三方 SaaS。它通过约定的请求头向自部署沙箱网关声明「本次请求要模拟的故障」，
从而在不依赖 real 网关的前提下，可复现地验证超时、5xx、签名错误、重复通知等路径。

用法（网关端）：沙箱网关读取 ``X-Sandbox-Fault`` 请求头（逗号分隔的故障标识 + 可选参数），
按标识模拟对应故障。Provider 端：把 ``build_sandbox_fault_header(...)`` 的返回值加到请求头，
即可让网关按需注入故障。测试端：直接用本模块构造故障，无需重新实现注入逻辑。

故障标识约定（见 :class:`SandboxFault`）：
- timeout：模拟网关超时（响应延迟超过调用方 deadline）；
- http_500 / http_503：网关返回对应 5xx（调用方应视为 ProviderError）；
- signature_error：网关头（回调签名错误路径）；
- duplicate_notify：网关对同一回调通知重发一次（验证非重放窗口）；
- delay_ms:NNNN：按毫秒延迟响应（调用方可配小 timeout 观察超时）。
"""
from __future__ import annotations

from enum import Enum

# 沙箱网关识别故障注入的请求头名。
SANDBOX_FAULT_HEADER = "X-Sandbox-Fault"


class SandboxFault(str, Enum):
    """网关沙箱可注入的确定性故障。"

    仅用于本地沙箱/测试链路，绝不用于生产 real 网关。
    """

    TIMEOUT = "timeout"                 # 模拟网关超时（响应延迟超过调用方 deadline）
    HTTP_500 = "http_500"               # 模拟网关 500
    HTTP_503 = "http_503"               # 模拟网关 503（可重试类）
    SIGNATURE_ERROR = "signature_error"   # 模拟回调签名错误
    DUPLICATE_NOTIFY = "duplicate_notify"  # 模拟重复通知（同一回调重发一次）


def build_sandbox_fault_header(faults: list[SandboxFault] | list[str],
                               delay_ms: int | None = None) -> str | None:
    """构造 `X-Sandbox-Fault` 请求头值。

    参数：
    - faults：要注入的故障标识（枚举或字符串均可）。
    - delay_ms：可选。若提供，额外追加 ``delay_ms:NNNN``（可配小 timeout 观察超时）。

    返回：可直接作为 ``httpx`` 请求头值的字符串；无故障时返回 ``None``。
    供测试／后续端到端验证复用，避免重复实现注入逻辑。
    """
    tokens: list[str] = []
    for f in faults:
        tokens.append(f.value if isinstance(f, SandboxFault) else str(f))
    if delay_ms is not None:
        tokens.append(f"delay_ms:{int(delay_ms)}")
    return ",".join(tokens) if tokens else None


def parse_sandbox_fault_header(value: str | None) -> dict[str, str]:
    """解析 `X-Sandbox-Fault` 为 `{fault_name: param_or_""}` 映射。

    供沙箱网关端读取故障声明；不支持/空值返回空 dict。
    """
    if not value:
        return {}
    result: dict[str, str] = {}
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            name, param = token.split(":", 1)
            result[name.strip()] = param.strip()
        else:
            result[token] = ""
    return result


def resolve_delay_ms(faults: dict[str, str], default_timeout_sleep: float = 3.0) -> float:
    """解析故障映射中的延迟毫秒数；未显式指定时用默认的「超时注入」秒数。

    用于网关端在 TIMEOUT 故障下决定 sleep 多久（默认超过常见调用方 deadline）。
    """
    raw = faults.get("delay_ms")
    if raw:
        try:
            return float(raw) / 1000.0
        except ValueError:
            return default_timeout_sleep
    if SandboxFault.TIMEOUT.value in faults:
        return default_timeout_sleep
    return 0.0
