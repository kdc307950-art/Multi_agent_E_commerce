"""登录双维度限流 + 失败退避。

安全红线（与 AGENTS.md 对齐）：
- 只对 `POST /api/auth/login` 生效；拒绝路径（账号超限 / IP 超限 / 退避期 / 后端不可用）
  一律 fail-closed（拒绝 + 脱敏审计），绝不放行。
- **脱敏**：凭据绝不进入本模块；账号以 `<tenant_id>:<user_id>` 这种资源标识形式记录，
  绝不记录 credential/哈希；客户端 IP 允许记录（非敏感键）。
- 指标不落高基数标签（见 metrics.py）；本模块自身不写指标，由路由统一记录。
- 存储后端：`memory`（默认，单进程/开发）| `redis`（多副本 api worker 用）。
  redis 连接失败 → fail-closed（拒绝并审计），绝不放行。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class RateLimitDecision:
    """限流判定结果。

    `allowed=False` 时：
      - `reason` ∈ {login_rate_limited, ip_rate_limited, login_backoff, redis_unavailable}；
      - `status_code` 恒为 429；
      - `retry_after` 为建议等待秒数（退避剩余 / 窗口剩余）。
    """
    allowed: bool
    reason: str | None = None
    status_code: int = 429
    retry_after: float = 0.0
    client_ip: str = ""
    detail: dict = field(default_factory=dict)


def extract_client_ip(request, trusted_proxy_depth: int = 0) -> str:
    """从请求提取客户端 IP。

    - `trusted_proxy_depth>0`（nginx 之后可信）：取 `X-Forwarded-For` 倒数第 depth 个
      （最接近可信代理的条目视为真实客户端），忽略更早/伪造的前段。
    - `trusted_proxy_depth==0`（直接暴露）：取 `request.client.host`，不信任任何代理头。
    - 返回空串表示无法确定（调用方按无 IP 处理，通常由 account 维度兜底）。
    不信任请求端伪造的 X-Forwarded-For：仅当配置了可信代理深度时才读取。
    """
    depth = int(trusted_proxy_depth or 0)
    if depth > 0:
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) >= depth:
                return parts[-depth]
            if parts:
                return parts[-1]
    client = getattr(request, "client", None)
    if client is not None:
        return getattr(client, "host", "") or ""
    return ""


class LoginRateLimiter(Protocol):
    """登录限流抽象。所有方法使用可注入 clock（便于测试）。"""

    def check(self, tenant_id: str, user_id: str, client_ip: str,
              now: float | None = None) -> RateLimitDecision:
        ...

    def record(self, tenant_id: str, user_id: str, client_ip: str, ok: bool,
               now: float | None = None) -> None:
        ...

    def check_and_register(self, tenant_id: str, user_id: str, client_ip: str, ok: bool,
                           now: float | None = None) -> RateLimitDecision:
        ...


class MemoryLoginRateLimiter:
    """进程内限流（滑动窗口 + 失败退避）。单进程 / 开发环境够用；多副本需 redis。

    状态：
      - `_acct_attempts[account]`：窗口内该账号的尝试时间戳（滑动窗口）；
      - `_ip_attempts[ip]`：窗口内该 IP 的尝试时间戳（跨账号聚合）；
      - `_failures[account]`：连续失败次数；`_backoff_until[account]`：退避锁定时戳。
    """

    def __init__(self, settings, clock=time.time) -> None:
        self._clock = clock
        self._window = float(settings.login_rate_limit_window_seconds)
        self._account_limit = int(settings.login_account_rate_limit)
        self._ip_limit = int(settings.login_ip_rate_limit)
        self._backoff_max_failures = int(settings.login_backoff_max_failures)
        self._backoff_base = float(settings.login_backoff_base_seconds)
        self._backoff_max = float(settings.login_backoff_max_seconds)
        self._lock = threading.Lock()
        self._acct_attempts: dict[str, deque[float]] = {}
        self._ip_attempts: dict[str, deque[float]] = {}
        self._failures: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}

    @staticmethod
    def _account_key(tenant_id: str, user_id: str) -> str:
        return f"{tenant_id}:{user_id}"

    @staticmethod
    def _prune(dq: deque[float], now: float, window: float) -> None:
        while dq and now - dq[0] > window:
            dq.popleft()

    def check(self, tenant_id: str, user_id: str, client_ip: str,
              now: float | None = None) -> RateLimitDecision:
        now = self._clock() if now is None else now
        acct = self._account_key(tenant_id, user_id)
        with self._lock:
            # 1) 失败退避（指数退避 + 封顶）。
            until = self._backoff_until.get(acct, 0.0)
            if until > now:
                return RateLimitDecision(False, "login_backoff", 429, until - now, client_ip,
                                         {"account": acct})
            # 2) 账号维度滑动窗口。
            adq = self._acct_attempts.get(acct)
            if adq is not None:
                self._prune(adq, now, self._window)
                if len(adq) >= self._account_limit:
                    return RateLimitDecision(False, "login_rate_limited", 429, self._window,
                                             client_ip, {"account": acct})
            # 3) IP 维度滑动窗口（跨账号聚合）。
            idq = self._ip_attempts.get(client_ip)
            if idq is not None:
                self._prune(idq, now, self._window)
                if len(idq) >= self._ip_limit:
                    return RateLimitDecision(False, "ip_rate_limited", 429, self._window,
                                             client_ip)
            return RateLimitDecision(True, None, 0, 0.0, client_ip)

    def record(self, tenant_id: str, user_id: str, client_ip: str, ok: bool,
               now: float | None = None) -> None:
        now = self._clock() if now is None else now
        acct = self._account_key(tenant_id, user_id)
        with self._lock:
            adq = self._acct_attempts.setdefault(acct, deque())
            idq = self._ip_attempts.setdefault(client_ip, deque())
            self._prune(adq, now, self._window)
            self._prune(idq, now, self._window)
            adq.append(now)
            idq.append(now)
            if ok:
                # 成功：重置该账号失败计数与退避，避免正常登录被误杀。
                self._failures[acct] = 0
                self._backoff_until.pop(acct, None)
            else:
                fails = self._failures.get(acct, 0) + 1
                self._failures[acct] = fails
                if fails >= self._backoff_max_failures:
                    backoff = self._backoff_base * (2 ** (fails - self._backoff_max_failures))
                    backoff = min(backoff, self._backoff_max)
                    self._backoff_until[acct] = now + backoff

    def check_and_register(self, tenant_id: str, user_id: str, client_ip: str, ok: bool,
                           now: float | None = None) -> RateLimitDecision:
        decision = self.check(tenant_id, user_id, client_ip, now)
        if decision.allowed:
            self.record(tenant_id, user_id, client_ip, ok, now)
        return decision


class RedisLoginRateLimiter:
    """基于 Redis 的分布式限流（多副本 api worker 用）。

    连接失败 → **fail-closed**：`check` 返回 `redis_unavailable` 拒绝（并交由路由审计），
    绝不放行。使用固定 TTL 窗口（INCR + EXPIRE）。
    """

    def __init__(self, settings, clock=time.time, redis_url: str = "") -> None:
        self._clock = clock
        self._window = max(1, int(settings.login_rate_limit_window_seconds))
        self._account_limit = int(settings.login_account_rate_limit)
        self._ip_limit = int(settings.login_ip_rate_limit)
        self._backoff_max_failures = int(settings.login_backoff_max_failures)
        self._backoff_base = float(settings.login_backoff_base_seconds)
        self._backoff_max = float(settings.login_backoff_max_seconds)
        self._redis_url = redis_url or getattr(settings, "redis_url", "") or "redis://localhost:6379/0"
        self._client = None
        self._build_lock = threading.Lock()

    def _conn(self):
        if self._client is None:
            import redis

            with self._build_lock:
                if self._client is None:
                    self._client = redis.Redis.from_url(
                        self._redis_url, socket_connect_timeout=1, socket_timeout=1)
        return self._client

    def check(self, tenant_id: str, user_id: str, client_ip: str,
              now: float | None = None) -> RateLimitDecision:
        now = self._clock() if now is None else now
        acct = f"rl:acct:{tenant_id}:{user_id}"
        ipn = f"rl:ip:{client_ip}"
        bf = f"rl:bf:{tenant_id}:{user_id}"
        try:
            p = self._conn().pipeline()
            p.get(bf)
            p.get(acct)
            p.get(ipn)
            bf_val, acct_val, ip_val = p.execute()
        except Exception:
            # fail-closed：redis 不可用且处于潜在攻击面 → 拒绝并审计。
            return RateLimitDecision(False, "redis_unavailable", 429, 0.0, client_ip)

        if bf_val is not None and float(bf_val) > now:
            return RateLimitDecision(False, "login_backoff", 429, float(bf_val) - now, client_ip)
        if acct_val is not None and int(acct_val) >= self._account_limit:
            return RateLimitDecision(False, "login_rate_limited", 429, float(self._window), client_ip)
        if ip_val is not None and int(ip_val) >= self._ip_limit:
            return RateLimitDecision(False, "ip_rate_limited", 429, float(self._window), client_ip)
        return RateLimitDecision(True, None, 0, 0.0, client_ip)

    def record(self, tenant_id: str, user_id: str, client_ip: str, ok: bool,
               now: float | None = None) -> None:
        now = self._clock() if now is None else now
        acct = f"rl:acct:{tenant_id}:{user_id}"
        ipn = f"rl:ip:{client_ip}"
        failk = f"rl:fail:{tenant_id}:{user_id}"
        bfk = f"rl:bf:{tenant_id}:{user_id}"
        try:
            c = self._conn()
            p = c.pipeline()
            p.incr(acct)
            p.expire(acct, self._window)
            p.incr(ipn)
            p.expire(ipn, self._window)
            if ok:
                p.delete(failk)
                p.delete(bfk)
                p.execute()
            else:
                p.incr(failk)
                p.expire(failk, self._window)
                p.execute()
                # 读回递增后的失败次数，再决定是否进入退避（指数退避 + 封顶）。
                fails = int(c.get(failk) or 0)
                if fails >= self._backoff_max_failures:
                    backoff = self._backoff_base * (2 ** (fails - self._backoff_max_failures))
                    backoff = min(backoff, self._backoff_max)
                    c.set(bfk, now + backoff, ex=max(1, int(backoff)))
        except Exception:
            # fail-closed：无法记录 → 由下一次 check 判定（redis 不可用则拒绝）。
            return

    def check_and_register(self, tenant_id: str, user_id: str, client_ip: str, ok: bool,
                           now: float | None = None) -> RateLimitDecision:
        decision = self.check(tenant_id, user_id, client_ip, now)
        if decision.allowed:
            self.record(tenant_id, user_id, client_ip, ok, now)
        return decision


def create_login_rate_limiter(settings, clock=time.time) -> LoginRateLimiter:
    """按 `settings.login_rate_limit_store` 构建限流器（memory 默认 | redis）。"""
    backend = (getattr(settings, "login_rate_limit_store", "memory") or "memory").strip().lower()
    if backend == "redis":
        return RedisLoginRateLimiter(settings, clock=clock)
    return MemoryLoginRateLimiter(settings, clock=clock)


def resolve_login_rate_limiter(settings, app_state) -> LoginRateLimiter:
    """在 app.state 上缓存单例限流器（每进程一个，避免每请求重建）。"""
    limiter = getattr(app_state, "_login_rate_limiter", None)
    if limiter is None:
        limiter = create_login_rate_limiter(settings)
        setattr(app_state, "_login_rate_limiter", limiter)
    return limiter
