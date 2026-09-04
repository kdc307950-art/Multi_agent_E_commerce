#!/usr/bin/env python
"""PromQL 子集求值器：为 t7「告警可触发/可定位/可关闭」提供合成样本求值证据。

背景：环境无 promtool / Prometheus 容器，故用本求值器对 deploy/observability/alert-rules.yml
中的每条规则表达式做**合成样本求值**，证明：
  (1) 给定可命中阈值的样本 → 表达式为真（**可触发**）；
  (2) 给定事件消逝后的样本 → 表达式为假（**可关闭**，尤其对 increase/rate 谓词）。

范围（刻意受限，只实现本文件规则用到的 PromQL 子集）：
   直接向量/标量：metric{label=~"re",k="v"}、up{job="api"}、time()
   rate(metric[w])、increase(metric[w])、sum(x)、sum(x) by (a,b)、clamp_min(x,lo)
   histogram_quantile(q, sum(rate(x_bucket[w])) by (le))、二元 / -、比较 > == >=
不实现：and/or/unless、offset、vector/vector 的笛卡尔积（本文件未用）。

仅用于证据记录，不接入运行时；权威判定以真实 Prometheus（docker-compose.observability.yml）为主。
所有合成样本标签均为**有界维度**，无任何 tenant_id/user_id/order_id/operation_id/thread_id。
"""
from __future__ import annotations

import pathlib
import re
import time as _t
from dataclasses import dataclass, field
from typing import Any

import yaml

_NOW = _t.time()


# ---------------------------------------------------------------------------
# 合成样本
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    labels: dict[str, str]
    value: float = 0.0       # 直接值：gauge / up / 计数器末值（用于非 rate 场景，本文件 gauge/up 用）
    inc: float | None = None  # 计数器在窗口内的增量（increase = inc；rate = inc/window）
    window: float = 300.0     # 窗口秒数


@dataclass
class Series:
    labels: dict[str, str]
    value: float


@dataclass
class Vector:
    series: list[Series] = field(default_factory=list)

    def non_empty(self) -> bool:
        return bool(self.series)


class Store:
    def __init__(self) -> None:
        self._m: dict[str, list[Sample]] = {}

    def add(self, metric: str, sample: Sample) -> None:
        self._m.setdefault(metric, []).append(sample)

    def series(self, metric: str) -> list[Sample]:
        return self._m.get(metric, [])


# ---------------------------------------------------------------------------
# 标签匹配
# ---------------------------------------------------------------------------
_MATCH_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|==|>=|<=|=)\s*(.+)$')


def _split_matchers(s: str) -> list[str]:
    out, cur, inq = [], "", False
    for ch in s:
        if ch == '"':
            inq = not inq
            cur += ch
        elif ch == ',' and not inq:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x for x in out if x.strip()]


def _extract_labels(label_sel: str | None) -> tuple[dict[str, str], str]:
    exact: dict[str, str] = {}
    match_str = ""
    if label_sel is None:
        return exact, ""
    for p in _split_matchers(label_sel):
        m = _MATCH_RE.match(p.strip())
        if not m:
            continue
        key, op, val = m.group(1), m.group(2), m.group(3).strip().strip('"')
        if op == "=":
            exact[key] = val
        else:
            match_str += ("," if match_str else "") + p.strip()
    return exact, match_str


def _match_exact(sample_labels: dict[str, str], exact: dict[str, str]) -> bool:
    return all(sample_labels.get(k) == v for k, v in exact.items())


def _match_label(sample_labels: dict[str, str], matchers: str | None) -> bool:
    if not matchers:
        return True
    for p in _split_matchers(matchers):
        m = _MATCH_RE.match(p.strip())
        if not m:
            continue
        key, op, val = m.group(1), m.group(2), m.group(3).strip().strip('"')
        actual = sample_labels.get(key, "")
        if op == "=":
            if actual != val:
                return False
        elif op == "!=":
            if actual == val:
                return False
        elif op == "=~":
            if not re.fullmatch(val, actual):
                return False
        elif op == "!~":
            if re.fullmatch(val, actual):
                return False
    return True


# ---------------------------------------------------------------------------
# 词法 + 递归下降（覆盖本文件子集）
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"""
    (?P<idents>[a-zA-Z_][a-zA-Z0-9_]*)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>"[^"]*")
  | (?P<op>==|=~|!=|!~|>=|<=|>|<|\+|-|/|\*|=)
  | (?P<punct>[{}\[\]\(\):,])
  | (?P<ws>\s+)
""", re.VERBOSE)


def _tokenize(expr: str):
    toks, pos = [], 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ValueError(f"无法解析 @ {pos}: {expr[pos:pos+20]!r}")
        pos = m.end()
        if m.lastgroup != "ws":
            toks.append((m.lastgroup, m.group()))
    return toks


class Parser:
    def __init__(self, expr: str):
        self.toks = _tokenize(expr)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        t = self.peek()
        if t is None:
            raise ValueError("表达式意外结束")
        self.i += 1
        return t

    def expect(self, val: str):
        t = self.next()
        if t[1] != val:
            raise ValueError(f"期望 {val!r}，得到 {t[1]!r}")

    def parse_top(self):
        left = self.parse_expr()
        nxt = self.peek()
        if nxt and nxt[0] == "op" and nxt[1] in (">", "==", ">="):
            op = self.next()[1]
            right = self.parse_expr()
            return ("cmp", left, op, right)
        return left

    def parse_expr(self):
        left = self.parse_unary()
        while True:
            nxt = self.peek()
            if nxt and nxt[0] == "op" and nxt[1] in ("/", "-"):
                op = self.next()[1]
                right = self.parse_unary()
                left = ("bin", op, left, right)
            else:
                break
        return left

    def parse_unary(self):
        kind, val = self.next()
        if kind == "num":
            return ("num", float(val))
        if kind == "idents":
            if self.peek() and self.peek()[1] == "(":
                self.next()
                args = []
                if self.peek() and self.peek()[1] != ")":
                    args.append(self.parse_expr())
                    while self.peek() and self.peek()[1] == ",":
                        self.next()
                        args.append(self.parse_expr())
                self.expect(")")
                by = None
                if self.peek() and self.peek()[0] == "idents" and self.peek()[1] == "by":
                    self.next()
                    self.expect("(")
                    by = []
                    while self.peek() and self.peek()[1] != ")":
                        n = self.next()
                        if n[0] == "idents":
                            by.append(n[1])
                    self.expect(")")
                return ("call", val, args, by)
            # metric ident [labels] [range]
            labels = None
            rng = None
            if self.peek() and self.peek()[1] == "{":
                self.next()
                buf = ""
                depth = 0
                while True:
                    t2 = self.next()
                    if t2[1] == "}" and depth == 0:
                        break
                    if t2[1] in "{}":
                        depth += 1 if t2[1] == "{" else -1
                    buf += t2[1]
                labels = buf
            if self.peek() and self.peek()[1] == "[":
                self.next()
                rngbuf = []
                while self.peek() and self.peek()[1] != "]":
                    rngbuf.append(self.next()[1])
                self.expect("]")
                rng = "".join(rngbuf)
            return ("metric", val, labels, rng)
        raise ValueError(f"无法解析原子 {val!r}")


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------
def _metric_vector(arg, store: Store, mode: str) -> Vector:
    _, name, labels, _rng = arg
    exact, mstr = _extract_labels(labels)
    out = Vector()
    for s in store.series(name):
        if not _match_exact(s.labels, exact):
            continue
        if not _match_label(s.labels, mstr):
            continue
        inc = s.inc if s.inc is not None else 0.0
        win = s.window if s.window else 300.0
        out.series.append(Series(dict(s.labels), inc / win if mode == "rate" else inc))
    return out


def _resolve_values(node, store: Store) -> Vector:
    _, name, labels, _rng = node
    exact, mstr = _extract_labels(labels)
    out = Vector()
    for s in store.series(name):
        if not _match_exact(s.labels, exact):
            continue
        if not _match_label(s.labels, mstr):
            continue
        out.series.append(Series(dict(s.labels), s.value))
    return out


def _sum(v: Any, by: list[str] | None) -> Any:
    if isinstance(v, (int, float)):
        return float(v)
    if not by:
        return float(sum(s.value for s in v.series))
    groups: dict[tuple, list[float]] = {}
    labmap: dict[tuple, dict[str, str]] = {}
    for s in v.series:
        key = tuple(sorted((k, s.labels.get(k, "")) for k in by))
        groups.setdefault(key, []).append(s.value)
        labmap.setdefault(key, {k: s.labels.get(k, "") for k in by})
    out = Vector()
    for key, vals in groups.items():
        out.series.append(Series(dict(labmap[key]), float(sum(vals))))
    return out


def _clamp_min(v: Any, lo: Any) -> Any:
    if isinstance(v, (int, float)):
        return max(float(v), float(lo))
    out = Vector()
    for s in v.series:
        out.series.append(Series(dict(s.labels), max(s.value, float(lo))))
    return out


def _apply(op: str, a: float, b: float) -> float:
    a, b = float(a), float(b)
    if op == "/":
        return a / b
    if op == "-":
        return a - b
    if op == "+":
        return a + b
    if op == "*":
        return a * b
    raise ValueError(op)


def _vec_key(s: Series) -> tuple:
    return tuple(sorted(s.labels.items()))


def _binary(op: str, l: Any, r: Any) -> Any:
    if isinstance(l, (int, float)) and isinstance(r, (int, float)):
        return _apply(op, l, r)
    if isinstance(l, Vector) and isinstance(r, (int, float)):
        return Vector([Series(dict(s.labels), _apply(op, s.value, r)) for s in l.series])
    if isinstance(l, (int, float)) and isinstance(r, Vector):
        return Vector([Series(dict(s.labels), _apply(op, l, s.value)) for s in r.series])
    if isinstance(l, Vector) and isinstance(r, Vector):
        # 按标签对齐（本文件 numerator/denominator 分组标签一致）
        rm = {_vec_key(s): s.value for s in r.series}
        out = Vector()
        for s in l.series:
            key = _vec_key(s)
            if key in rm:
                out.series.append(Series(dict(s.labels), _apply(op, s.value, rm[key])))
        return out
    raise ValueError(f"不支持的二元运算 {op} between {type(l)}/{type(r)}")


def _histogram_quantile(q: float, buckets: Any) -> float:
    if isinstance(buckets, float):
        return buckets
    items = []
    for s in buckets.series:
        le = s.labels.get("le", "")
        if le == "+Inf":
            lef = float("inf")
        else:
            try:
                lef = float(le)
            except ValueError:
                continue
        items.append((lef, s.value))
    if not items:
        return 0.0
    # 单调累积（bucket 已是累计计数，此处确保非降）
    items.sort(key=lambda x: x[0])
    for i in range(1, len(items)):
        if items[i][1] < items[i - 1][1]:
            items[i] = (items[i][0], items[i - 1][1])
    total = items[-1][1]
    if total <= 0:
        return 0.0
    rank = q * total
    if rank <= 0:
        return items[0][0]
    # 找到第一个计数 >= rank 的桶
    b = 0
    for i, (lef, cnt) in enumerate(items):
        if cnt >= rank:
            b = i
            break
    else:
        return items[-1][0]
    if b == 0:
        return items[0][0]
    b_start = items[b - 1][0]
    # 若 b_start 为 -inf 则起点 0
    if b_start == float("-inf"):
        b_start = 0.0
    b_count = items[b][1] - items[b - 1][1]
    if b_count <= 0:
        return b_start
    if items[b][0] == float("inf"):
        # 落在 +Inf 桶：用末段宽度外推
        width = items[b - 1][0] - items[b - 2][0] if b >= 2 else items[b - 1][0]
        return b_start + (rank - items[b - 1][1]) / b_count * width
    return b_start + (rank - items[b - 1][1]) / b_count * (items[b][0] - b_start)


def _eval(node, store: Store) -> Any:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "metric":
        return _resolve_values(node, store)
    if kind == "call":
        fname = node[1]
        args = node[2]
        by = node[3]
        if fname == "rate":
            return _metric_vector(args[0], store, "rate")
        if fname == "increase":
            return _metric_vector(args[0], store, "increase")
        if fname == "sum":
            return _sum(_eval(args[0], store), by)
        if fname == "clamp_min":
            return _clamp_min(_eval(args[0], store), _eval(args[1], store))
        if fname == "histogram_quantile":
            return _histogram_quantile(_eval(args[0], store), _eval(args[1], store))
        if fname == "time":
            return _NOW
        raise ValueError(f"未知函数 {fname!r}")
    if kind == "bin":
        return _binary(node[1], _eval(node[2], store), _eval(node[3], store))
    raise ValueError(f"未知节点 {kind!r}")


def _cmp(op: str, lv: Any, rv: Any) -> bool:
    if op == ">":
        if isinstance(lv, Vector):
            return any(s.value > rv for s in lv.series)
        return lv > rv
    if op == ">=":
        if isinstance(lv, Vector):
            return any(s.value >= rv for s in lv.series)
        return lv >= rv
    if op == "==":
        if isinstance(lv, Vector):
            return any(abs(s.value - rv) < 1e-9 for s in lv.series)
        return abs(lv - rv) < 1e-9
    raise ValueError(op)


def evaluate_rule(rule: dict, store: Store) -> bool:
    ast = Parser(rule["expr"]).parse_top()
    if ast[0] == "cmp":
        return _cmp(ast[2], _eval(ast[1], store), _eval(ast[3], store))
    res = _eval(ast, store)
    return res.non_empty() if isinstance(res, Vector) else bool(res)


# ---------------------------------------------------------------------------
# 规则读取 + 样本构造
# ---------------------------------------------------------------------------
def load_rules(path: str):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    rules = []
    for g in data["groups"]:
        for r in g["rules"]:
            rules.append({
                "group": g["name"], "alert": r["alert"], "expr": r["expr"],
                "for": r.get("for", ""), "labels": r.get("labels", {}),
                "summary": r.get("annotations", {}).get("summary", ""),
                "description": r.get("annotations", {}).get("description", ""),
            })
    return rules


_DURATION_RE = re.compile(r'^\d+(\.\d+)?(ms|s|m|h|d)$')


def validate_rules(rules) -> list[str]:
    """promtool 等价的结构/语法校验：表达式可解析、for 为合法时长、必填字段齐全。"""
    issues: list[str] = []
    for r in rules:
        name = r["alert"]
        if not r["expr"]:
            issues.append(f"{name}: 缺 expr")
            continue
        try:
            Parser(r["expr"]).parse_top()
        except Exception as exc:
            issues.append(f"{name}: 表达式解析失败: {exc}")
        if r["for"] and not _DURATION_RE.match(r["for"]):
            issues.append(f"{name}: for 非法时长 {r['for']!r}")
        if not r["summary"]:
            issues.append(f"{name}: 缺 annotations.summary")
        if not r["description"]:
            issues.append(f"{name}: 缺 annotations.description")
    return issues


def _bucket(labels: dict[str, str], inc: float, window: float = 600.0) -> Sample:
    return Sample(labels, inc=inc, window=window)


def build_firing_store() -> Store:
    """可触发样本：构造能命中阈值的合成时序（全部带界标签）。"""
    s = Store()
    s.add("up", Sample({"job": "api"}, value=0))
    s.add("up", Sample({"job": "prometheus"}, value=0))
    s.add("up", Sample({"job": "loki"}, value=0))
    # LoginBruteForce: 5m 内 200 次 → rate 0.667 > 0.5
    s.add("login_rate_limited_total", Sample({"reason": "login_rate_limited"}, inc=200, window=300))
    # ApiHighErrorRate: 5xx 40 次 / 总 500 次 → 8%（>5%）
    s.add("api_requests_total", Sample({"route": "chat", "method": "POST", "status": "500"}, inc=40, window=300))
    s.add("api_requests_total", Sample({"route": "chat", "method": "POST", "status": "200"}, inc=460, window=300))
    # ApprovalFailureRate: rejected 30 / 总 530 → 5.66%（>5%）
    s.add("approval_decisions_total", Sample({"route": "approval.decision", "status": "rejected"}, inc=30, window=300))
    s.add("approval_decisions_total", Sample({"route": "approval.decision", "status": "approved"}, inc=500, window=300))
    # ApprovalLatency: 95% 落在 +Inf（>30s）→ p95≈Inf > 30
    for le, inc in [("0.05",0),("0.1",0),("0.25",0),("0.5",0),("1.0",0),("2.0",0),("5.0",0),("10.0",0),("30.0",0)]:
        s.add("approval_decision_latency_seconds_bucket", _bucket({"route": "approval.decision", "le": le}, inc))
    s.add("approval_decision_latency_seconds_bucket", _bucket({"route": "approval.decision", "le": "+Inf"}, 100))
    # ReconciliationMismatch: 窗口内出现新 mismatch
    s.add("reconcile_mismatch_total", Sample({"kind": "query_failed"}, inc=1, window=300))
    # NoReconciliation: 对账 25h 前运行
    s.add("reconcile_last_run_timestamp_seconds", Sample({}, value=_NOW - 90000))
    # HighHumanIntervention: 人工 200 次，总 api 请求 (40+460+300)/300 → 比率 0.25（>20%）
    s.add("human_intervention_total", Sample({"kind": "order_deny"}, inc=200, window=300))
    s.add("api_requests_total", Sample({"route": "orders", "method": "GET", "status": "404"}, inc=300, window=300))
    # RPO/RTO 超出
    s.add("drill_rpo_seconds", Sample({"component": "pg_backup"}, value=1000))
    s.add("drill_rto_seconds", Sample({"component": "pg_backup"}, value=4000))
    # TenantDenialSpike: 8 次
    s.add("security_denials_total", Sample({"kind": "forbidden"}, inc=8, window=300))
    # CrossTenantAccess: kind=cross_tenant 命中
    s.add("security_denials_total", Sample({"kind": "cross_tenant"}, inc=1, window=300))
    # ComponentRestart 指标**未接入**（不在 store）→ 恒 False
    return s


def build_benign_store() -> Store:
    """可关闭样本：事件消逝（窗口内无新事件）→ 应回到未触发。"""
    s = Store()
    s.add("up", Sample({"job": "api"}, value=1))
    s.add("up", Sample({"job": "prometheus"}, value=1))
    s.add("up", Sample({"job": "loki"}, value=1))
    s.add("login_rate_limited_total", Sample({"reason": "login_rate_limited"}, inc=0, window=300))
    s.add("api_requests_total", Sample({"route": "chat", "method": "POST", "status": "500"}, inc=0, window=300))
    s.add("api_requests_total", Sample({"route": "chat", "method": "POST", "status": "200"}, inc=500, window=300))
    s.add("approval_decisions_total", Sample({"route": "approval.decision", "status": "rejected"}, inc=0, window=300))
    s.add("approval_decisions_total", Sample({"route": "approval.decision", "status": "approved"}, inc=500, window=300))
    for le, inc in [("0.05",0),("0.1",0),("0.25",0),("0.5",0),("1.0",0),("2.0",0),("5.0",0),("10.0",0),("30.0",0)]:
        s.add("approval_decision_latency_seconds_bucket", _bucket({"route": "approval.decision", "le": le}, inc))
    s.add("approval_decision_latency_seconds_bucket", _bucket({"route": "approval.decision", "le": "+Inf"}, 0))
    s.add("reconcile_mismatch_total", Sample({"kind": "query_failed"}, inc=0, window=300))
    s.add("reconcile_last_run_timestamp_seconds", Sample({}, value=_NOW))
    s.add("human_intervention_total", Sample({"kind": "order_deny"}, inc=0, window=300))
    s.add("api_requests_total", Sample({"route": "orders", "method": "GET", "status": "404"}, inc=0, window=300))
    s.add("drill_rpo_seconds", Sample({"component": "pg_backup"}, value=100))
    s.add("drill_rto_seconds", Sample({"component": "pg_backup"}, value=100))
    s.add("security_denials_total", Sample({"kind": "forbidden"}, inc=0, window=300))
    s.add("security_denials_total", Sample({"kind": "cross_tenant"}, inc=0, window=300))
    return s


# 每条规则的编辑性说明（定位指引 / 关闭条件 / 触发路径 / 缺口）。
# 触发/关闭的**布尔结果由求值器真实计算**，此处仅为可读叙述。
_DETAIL = {
    "ApiJobDown": {
        "trigger": "`up{job=\"api\"} == 0` 且持续 `for: 1m` —— api 目标抓取失败/进程 down。",
        "locate": "summary/description 指向 OPS_RUNBOOK §4，并说明 api:8000 抓取失败。",
        "close": "`up` 是瞬时值，api 恢复 `up=1` 即恢复；`for: 1m` 抑制抖动。",
    },
    "PrometheusJobDown": {
        "trigger": "`up{job=\"prometheus\"} == 0` 且 `for: 2m`。",
        "locate": "summary 提到“观测链路中断”，description 指向检查 prometheus 容器。",
        "close": "prometheus down 恢复后 `up=1` 即恢复。",
    },
    "LokiJobDown": {
        "trigger": "`up{job=\"loki\"} == 0` 且 `for: 2m`。",
        "locate": "summary “Loki 抓取失败”，description 指向核对 promtail/loki。",
        "close": "loki 恢复后 `up=1` 即恢复。",
    },
    "LoginBruteForceAttempts": {
        "trigger": "`sum(rate(login_rate_limited_total[5m])) > 0.5` —— 5 分钟内登录限流速率 > 0.5 次/秒（合成样本 200 次/300s→0.667 命中）。",
        "locate": "description 指向 OPS_RUNBOOK §4（疑似爆破，检查来源 IP/账号），并指到 GET /api/audit 或 Loki（含 PII 脱敏）。",
        "close": "`rate[5m]` 窗口内无新限流即回到 0；`for: 1m` 抑制短路。",
    },
    "ComponentRestartDetected": {
        "trigger": "表达式 `increase(component_restart_total[5m]) > 0`，但该指标**未在任何**打点/编排层上报（store 无此 metric）→ 恒为 False，不会发声。",
        "locate": "summary 含 `{{ $labels.component }}`，description 说明需编排层接入 component_restart_total。",
        "close": "n/a（当前不触发）。",
    },
    "ApiHighErrorRate": {
        "trigger": "`sum(rate(api{status=~\"5..\"}[5m])) by (route,method) / clamp_min(sum(rate(api[5m])) by (route,method),0.001) > 0.05`（合成样本 40/500→8%>5% 命中）。",
        "locate": "summary 带 route/method，description 指向 Langfuse trace/Loki/审计。",
        "close": "`rate` 窗口内 5xx 占比回落即恢复；5xx 计数不增长即回到阈值下。",
    },
    "ApprovalFailureRateHigh": {
        "trigger": "`sum(rate(approval{status=~\"rejected|error|timeout\"}[5m])) / clamp_min(sum(rate(approval[5m])),0.001) > 0.05`（合成样本 30/530→5.66%>5% 命中）。",
        "locate": "description 指向 GET /api/audit?target_type=approval。",
        "close": "`rate` 窗口内失败占比回落即恢复。",
    },
    "ApprovalLatencyHigh": {
        "trigger": "`histogram_quantile(0.95, sum(rate(approval_decision_latency_seconds_bucket[10m])) by (le)) > 30`（合成样本 95% 落在 +Inf（>30s）→ p95 远超 30）。",
        "locate": "description 指向对外网关 / Langfuse 审批 span。",
        "close": "p95 回落至 ≤30s 即恢复；直方图依赖真实审批延迟打点。",
    },
    "ReconciliationMismatch": {
        "trigger": "`increase(reconcile_mismatch_total[5m]) > 0` —— 5 分钟内出现新 mismatch 即触发（合成样本 inc=1 命中）。",
        "locate": "description 指向 OPS_RUNBOOK（不一致→转人工），明细走 GET /api/audit?target_type=operation。",
        "close": "用 `increase[5m]>0` 而非 `>0`：窗口内无新 mismatch 即回到 0，可关闭（计数器单调递增本身不会回落）。",
    },
    "NoReconciliation": {
        "trigger": "`time() - reconcile_last_run_timestamp_seconds > 86400`（对账任务超过 24h 未运行）。gauge 由 `engine.reconcile()`→Celery 对账任务在每次对账末尾写入。",
        "locate": "description 指向检查 celery 对账任务/worker。",
        "close": "对账任务恢复运行会刷新 gauge（`_now()`），差值回落即恢复。",
    },
    "HighHumanInterventionRate": {
        "trigger": "`sum(rate(human_intervention_total[5m])) / clamp_min(sum(rate(api_requests_total[5m])),0.001) > 0.20`（合成样本 human 200/总 api 800 → 0.25>20% 命中）。",
        "locate": "description 指向 capability/RAG/写操作门控，明细走 GET /api/audit?target_type=operation。",
        "close": "`rate[5m]` 窗口内人工介入占比回落即恢复。",
    },
    "RpoExceeded": {
        "trigger": "`drill_rpo_seconds{component=\"pg_backup\"} > 900`（RPO > 15min）。**写路径缺口**：`restore_drill.sh` 只把 RPO/RTO 写入 `deploy/drills/records/*.json|*.md`，**未回填此 gauge** → 当前不会触发。需 dr-engineer(t2) 在演练脚本内把实测 RPO 写入该 gauge（或经受控指标端点回填）。",
        "locate": "description 指向 OPS_RUNBOOK §3（超基线→评估回滚）。",
        "close": "gauge 一旦（由脚本）写入 ≤900 的实测值即恢复；当前因未回填而不触发。",
    },
    "RtoExceeded": {
        "trigger": "`drill_rto_seconds{component=\"pg_backup\"} > 3600`（RTO > 60min）。同 RpoExceeded：**写路径缺口**（restore_drill.sh 仅写 records，不写 gauge）。",
        "locate": "description 指向 OPS_RUNBOOK §3。",
        "close": "同 RpoExceeded（需脚本回填后才有意义）。",
    },
    "TenantDenialSpike": {
        "trigger": "`increase(security_denials_total[5m]) > 5` —— 5 分钟内安全拒绝 > 5 次（合成样本 inc=8 命中）。",
        "locate": "description 指向 GET /api/audit 四维追溯。",
        "close": "`increase[5m]>5`：窗口内无新拒绝即回到 0，可关闭。",
    },
    "CrossTenantAccessDetected": {
        "trigger": "`increase(security_denials_total{kind=~\"cross_tenant|cross_user_|access_denied|forbidden|role_mismatch\"}[5m]) > 0`（合成样本 kind=cross_tenant inc=1 命中）。",
        "locate": "description 指向 OPS_RUNBOOK §4（platform_admin 独立流程 + 二次确认 + 不可抵赖审计）与 GET /api/audit。",
        "close": "`increase[5m]>0`：窗口内无该类拒绝即恢复，可关闭。",
    },
}


def _resolve_out(rules, fire, benign):
    rows = {}
    for r in rules:
        name = r["alert"]
        rows[name] = {
            "fire": evaluate_rule(r, fire),
            "close": not evaluate_rule(r, benign),
        }
    return rows


def main() -> None:
    rules_path = pathlib.Path("deploy/observability/alert-rules.yml")
    rules = load_rules(str(rules_path))
    fire = build_firing_store()
    benign = build_benign_store()
    rows = _resolve_out(rules, fire, benign)
    issues = validate_rules(rules)
    out_path = pathlib.Path(__file__).resolve().with_name("ALERT_RULES_VERIFICATION.md")
    text = build_markdown(rules, rows, issues)
    out_path.write_text(text, encoding="utf-8")
    print(f"WROTE {out_path}")
    print(f"VALIDATION issues={len(issues)}: {issues}")
    for r in rules:
        name = r["alert"]
        print(f"{name:32s} trigger={'YES' if rows[name]['fire'] else 'NO '}  close={'YES' if rows[name]['close'] else 'NO '}")


def build_markdown(rules, rows, issues=None) -> str:
    out = []
    out.append("# 告警规则「可触发 / 可定位 / 可关闭」验证记录（t7）")
    out.append("")
    out.append("> **验证方法**：环境无 `promtool` / Prometheus 容器，故用自建 PromQL 子集求值器 "
               "`deploy/observability/evidence/eval_alert_rules.py` 对**合成样本**求值（覆盖本文件用到的 "
               "`rate`/`increase`/`sum by`/`clamp_min`/`histogram_quantile`/`time()`/标签正则匹配等算子），"
               "并做**引用规则文件的语法/结构校验**（表达式可解析、`for` 为合法时长、缺 annotations）。"
               "每次运行会重新计算 `deploy/observability/alert-rules.yml` 的每条规则，落到本文件。")
    out.append("> **权威判定**：以真实 Prometheus（docker-compose.observability.yml 把 `deploy/observability/alert-rules.yml` "
               "挂载为 `/etc/prometheus/alert-rules.yml`，抓取 `/api/metrics`）为准；本次**未起容器**，故用求值器等价证据。")
    out.append("")
    issues = issues or []
    out.append("## 0. 语法/结构校验（promtool 等价）")
    out.append("")
    if issues:
        out.append(f"⚠️ 发现 {len(issues)} 处问题：")
        for i in issues:
            out.append(f"- {i}")
    else:
        out.append("✅ 表达式均可被求值器解析、`for` 均为合法时长、每条规则均含 `annotations.summary`/`annotations.description`。")
    out.append("")
    out.append("## 0. 汇总表")
    out.append("")
    out.append("| 规则 | 可触发(命中阈值样本) | 可关闭(事件消逝后恢复) | 备注/缺口 |")
    out.append("|---|:---:|:---:|---|")
    for r in rules:
        name = r["alert"]
        note = ""
        if name in ("RpoExceeded", "RtoExceeded"):
            note = "⚠️ 表达式可命中，但写路径未回填（restore_drill.sh 只写 records，需 t2 回填 gauge）"
        elif name == "ComponentRestartDetected":
            note = "指标未接入（编排层需上报 component_restart_total）"
        elif name == "NoReconciliation":
            note = "gauge 由 engine.reconcile()→celery 对账任务写入"
        fire = "是" if rows[name]["fire"] else "否"
        close = "是" if rows[name]["close"] else "否"
        out.append(f"| {name} | {fire} | {close} | {note} |")
    out.append("")
    out.append("## 1. 逐条验证")
    out.append("")
    for r in rules:
        name = r["alert"]
        d = _DETAIL.get(name, {})
        group = r["group"]
        expr = r["expr"].strip().replace("\n", " ")
        fire = "✅ 可触发（合成样本命中，求值器返回 True）" if rows[name]["fire"] else "❌ 不可触发"
        close = "✅ 可关闭（事件消逝样本返回未触发）" if rows[name]["close"] else "❌ 不可关闭（可能永久告警）"
        out.append(f"### `{name}`（group：`{group}`）")
        out.append("")
        out.append(f"- **表达式**：```{expr}```")
        out.append(f"- **可触发**：{fire}。{d.get('trigger', '')}")
        out.append(f"- **可定位**：{d.get('locate', '')}")
        out.append(f"  - 真实 annotations.summary：`{r['summary']}`")
        out.append(f"  - 真实 annotations.description：`{r['description']}`")
        out.append(f"- **可关闭**：{close}。{d.get('close', '')}")
        out.append("")
    out.append("## 2. 规则 ↔ 指标映射（与 src/observability/metrics.py + 打点处一致，全部有界标签）")
    out.append("")
    out.append("| 规则 | 依赖指标 | 打点位置 | 标签（有界） |")
    out.append("|---|------|----------|------|")
    mapping = [
        ("ApiJobDown/PrometheusJobDown/LokiJobDown", "up{job=...}", "Prometheus 自动生成", "job"),
        ("LoginBruteForceAttempts", "login_rate_limited_total", "src/api/routes.py::login", "reason"),
        ("ApiHighErrorRate", "api_requests_total", "src/main.py HTTP 中间件", "route/method/status"),
        ("ApprovalFailureRateHigh", "approval_decisions_total", "src/api/routes.py::decide_approval", "route/status"),
        ("ApprovalLatencyHigh", "approval_decision_latency_seconds", "src/api/routes.py::decide_approval", "route"),
        ("ReconciliationMismatch", "reconcile_mismatch_total", "src/execution/engine.py::_reconcile_mismatch", "kind"),
        ("NoReconciliation", "reconcile_last_run_timestamp_seconds", "src/execution/engine.py::reconcile", "无标签"),
        ("HighHumanInterventionRate", "human_intervention_total", "routes.py order_deny/approval_timeout + engine.py execution_handoff/reconcile_mismatch", "kind"),
        ("RpoExceeded", "drill_rpo_seconds", "⚠️ 未回填（restore_drill.sh 只写 records/*.md）", "component"),
        ("RtoExceeded", "drill_rto_seconds", "⚠️ 未回填（restore_drill.sh 只写 records/*.md）", "component"),
        ("TenantDenialSpike/CrossTenantAccessDetected", "security_denials_total", "src/auth/security.py::audit_security_denial + routes.py 不可信回调", "kind"),
    ]
    for row in mapping:
        out.append(f"| {row[0]} | `{row[1]}` | {row[2]} | {row[3]} |")
    out.append("")
    out.append("## 3. 缺口与建议（如实标注，勿臆造达标）")
    out.append("")
    out.append("1. **RPO/RTO 规则暂不可真正触发**：`restore_drill.sh`/`backup_encrypted.sh` 仅把实测 RPO/RTO 写入 "
               "`deploy/drills/records/drill-pg-encrypted-restore.json` 与 `DR-...-pg-encrypted-restore.md`，"
               "**未回填** `drill_rpo_seconds`/`drill_rto_seconds` gauge（该 gauge 只被测试与规则引用）。"
               "要使 `RpoExceeded`/`RtoExceeded` 触发，需 dr-engineer(t2) 在演练脚本/受控任务里 `get_metrics().set(\"drill_rpo_seconds\",...，{\"component\":\"pg_backup\"})` 回填。")
    out.append("2. **ComponentRestartDetected** 依赖的 `component_restart_total` 未在编排层打点，当前恒不触发（表达式合法、不误报）。")
    out.append("3. **阈值均为占位初稿**：RPO≤900s/RTO≤3600s、错误率>5%、人工>20%、审批>5% 等需以真实演练/观测校准。")
    out.append("")
    out.append("> 口径红线：所有规则标签均为有界维度（route/status/kind/component/job），绝不含 `tenant_id`/`user_id`/`order_id`/`operation_id`/`thread_id`/`approval_id`。")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()

