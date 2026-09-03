"""LLM/日志脱敏与端点网络白名单守卫。

完全自托管纪律（Agent 宪法 1.8）：日志默认脱敏，不把订单地址、完整支付信息或跨租户
聚合明细写入日志；模型端点只能落在受控内网/私网，禁止未经批准的公网外联。

- `redact()`：掩码敏感值（手机号、详细地址、订单号、完整支付/密钥、Authorization 头）。
- `EndpointGuard.allowed()`：按 host / IP / CIDR 网络白名单校验端点；受限环境空白名单
  fail-closed（拒绝访问任何非白名单端点，从而阻断未经批准的外联）。
"""
from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urlparse

# 敏感模式（用脱敏符号替换，绝不记录真实值）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ORDER_RE = re.compile(r"(ORD[-_]?\d+)", re.IGNORECASE)
# 地址/号码：命中"省/市/区 + 路/街/道/巷/弄/号 + 数字"的连续片段，保守掩码。
_ADDR_RE = re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|市|区|县|镇|乡|自治州|自治区|特别行政区)"
                      r"[\u4e00-\u9fa5]{2,20}[\u4e00-\u9fa5\d]{2,30}"
                      r"(?:路|街|巷|道|弄|号|楼|栋|单元|室)?")
# 各类密钥/token/授权头（避免把 api_key、Bearer 泄露到日志）
_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]+|"
                     r"token[\"'\s:=]+[A-Za-z0-9._\-]{6,}|secret[\"'\s:=]+[A-Za-z0-9._\-]{6,})", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
# 完整银行卡/支付账号（16-19 位）
_CARD_RE = re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")


def redact(text: str) -> str:
    """对字符串做脱敏：手机号/地址/订单号/密钥/邮箱/卡号 → 掩码。"""
    if not isinstance(text, str) or not text:
        return text
    text = _ORDER_RE.sub("ORD-***", text)
    text = _PHONE_RE.sub("138****0000", text)
    text = _EMAIL_RE.sub("*@*.com", text)
    text = _CARD_RE.sub("**** **** **** ****", text)
    text = _KEY_RE.sub("***", text)
    # 地址最后处理（可能嵌套其它模式），保守掩码避开误报。
    text = _ADDR_RE.sub("[地址已脱敏]", text)
    return text


def redact_url(url: str) -> str:
    """掩码 URL 中的 query 参数值（如 ?token=xxx → ?token=***）。"""
    if not isinstance(url, str) or not url:
        return url
    return re.sub(r"\?([^&#]+)=([^&#]+)", r"?\1=***", url)


class EndpointGuard:
    """端点网络白名单守卫。

    指标：`allowed_hosts` 为逗号分隔的 host / IP / CIDR。`base_url` 的 host 必须命中：
    - 命中显式白名单之一；
    - 或（开发/测试且白名单为空时）落在 loopback / RFC1918 私网；
    - 受限环境白名单为空 → fail-closed（全部拒绝，阻断未批准外联）。
    """

    def __init__(self, allowed_hosts: list[str], restricted: bool = False) -> None:
        self._raw = list(allowed_hosts or [])
        self._restricted = restricted

    def _matches(self, host: str) -> bool:
        """判断 host 是否命中任一条白名单规则（host 精确 / IP / CIDR）。"""
        hl = host.lower().rstrip(".")
        if not self._raw:
            # 无显式白名单：仅放行 loopback / RFC1918 私网（完全自托管默认）；受限环境不走到这。
            if self._restricted:
                return False
            return self._is_private(host)
        for rule in self._raw:
            rl = rule.strip().lower().rstrip(".")
            if not rl:
                continue
            if rl == hl:
                return True
            if rl in {"*", "0.0.0.0/0", "::/0"}:
                return True
            try:
                net = ipaddress.ip_network(rl, strict=False)
                ip = ipaddress.ip_address(hl if "/" not in hl else hl.split("/")[0])
                if ip in net:
                    return True
            except ValueError:
                # 非 IP/CIDR 规则（主机名等）已在上方精确匹配；忽略解析失败。
                continue
        return False

    @staticmethod
    def _is_private(host: str) -> bool:
        h = host.split(":")[0]
        if h in {"localhost", "::1"}:
            return True
        try:
            ip = ipaddress.ip_address(h)
            return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
        except ValueError:
            # 若不是 IP（如裸主机名），允许 loopback/私网命名空间（如 *.local）。
            return h in {"localhost"} or h.endswith(".local") or h.startswith("10.") \
                or h.startswith("127.") or h.startswith("172.") or h.startswith("192.168.")

    def allowed(self, base_url: str) -> bool:
        """base_url 端点是否可用；不可用应 fail-closed（拒绝调用）。"""
        if not base_url:
            return False
        try:
            host = urlparse(base_url).hostname or ""
        except ValueError:
            return False
        return self._matches(host)

    @property
    def restrict_mode(self) -> str:
        return "restricted" if self._restricted else "default"


def make_logger(name: str, *, redact_payload: bool = True) -> logging.Logger:
    """构建项目 logger（默认 WARNING，避免噪声）。脱敏日志由调用点用 redact() 后再打印。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger._dsh_redact = redact_payload  # type: ignore[attr-defined]
    return logger
