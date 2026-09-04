#!/usr/bin/env python3
"""drill_metric_exporter.py —— 把实测 RPO/RTO 回填为 Prometheus gauge（drill_rpo_seconds / drill_rto_seconds）。

背景（t8）：deploy/observability/alert-rules.yml 已定义
  RpoExceeded: drill_rpo_seconds{component="pg_backup"} > 900
  RtoExceeded: drill_rto_seconds{component="pg_backup"} > 3600
但灾备脚本（restore_drill.sh / backup_encrypted.sh / verify_dr_compose.sh）只把 RPO/RTO 写入
deploy/drills/records/*.md|*.json，**未回填 gauge** → 两条规则恒不触发。

本脚本提供**状态文件**（Prometheus 文本采集的持久数据源），供 DR 脚本在实测后回填；再由
src/api/routes.py 的 `/api/metrics` 在渲染时合并进 api 进程内 registry（Prometheus 现有
scrape `api:8000/api/metrics` 即可采集到 drill_rpo_seconds/drill_rto_seconds）。

标签只用**有界**维度 component="pg_backup"，绝不含 tenant_id/user_id 等高基数/敏感标签。

用法：
  # 回填（DR 脚本在实测后调用；未提供的指标保留原值）
  python deploy/scripts/drill_metric_exporter.py write --rpo 0.877 --rto 0.093 \
      --state deploy/drills/metrics/drill_gauges.json --backup langgraph-dr-verify.dump.enc
  # 渲染 Prometheus 文本（可写为 .prom textfile，或供验证）
  python deploy/scripts/drill_metric_exporter.py render --state deploy/drills/metrics/drill_gauges.json
  # 校验阈值（RPO<=900, RTO<=3600）
  python deploy/scripts/drill_metric_exporter.py verify --state deploy/drills/metrics/drill_gauges.json

环境变量 DRILL_GAUGE_STATE 可覆盖默认状态文件路径。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

COMPONENT = "pg_backup"
RPO_THRESHOLD = 900.0      # 与 alert-rules.yml RpoExceeded 对齐（RPO<=15min）
RTO_THRESHOLD = 3600.0     # 与 alert-rules.yml RtoExceeded 对齐（RTO<=60min）

# 默认状态文件：相对本文件（deploy/scripts/）定位到 deploy/drills/metrics/drill_gauges.json
_DEFAULT_STATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "drills", "metrics", "drill_gauges.json")


def _state_path(cli_state: str | None) -> str:
    return cli_state or os.environ.get("DRILL_GAUGE_STATE") or _DEFAULT_STATE


def _load(state: str) -> dict:
    try:
        with open(state, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"component": COMPONENT}


def _save(state: str, data: dict) -> None:
    os.makedirs(os.path.dirname(state), exist_ok=True)
    with open(state, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    # 状态文件权限 600（含灾备内部数据，非敏感但遵循最小暴露）
    try:
        os.chmod(state, 0o600)
    except OSError:
        pass


def _prom_text(data: dict) -> str:
    """把状态渲染成 Prometheus 文本（与 expr 对齐的指标名与标签）。"""
    lines: list[str] = []
    if "rpo_seconds" in data:
        v = float(data["rpo_seconds"])
        lines.append("# TYPE drill_rpo_seconds gauge")
        lines.append(f'drill_rpo_seconds{{component="{COMPONENT}"}} {v:g}')
    if "rto_seconds" in data:
        v = float(data["rto_seconds"])
        lines.append("# TYPE drill_rto_seconds gauge")
        lines.append(f'drill_rto_seconds{{component="{COMPONENT}"}} {v:g}')
    return "\n".join(lines) + ("\n" if lines else "")


def _write(state: str, rpo: float | None, rto: float | None, backup: str | None) -> dict:
    data = _load(state)
    data["component"] = COMPONENT
    # 未提供的指标保留原值；提供则更新（幂等，覆盖为最新实测）
    if rpo is not None:
        data["rpo_seconds"] = float(rpo)
    if rto is not None:
        data["rto_seconds"] = float(rto)
    if backup:
        data["backup_file"] = backup
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(state, data)
    # 打印渲染文本，便于脚本/日志留痕（不打印任何密钥）
    print(_prom_text(data), end="")
    return data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="回填/渲染灾备 RPO-RTO gauge")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="回填 RPO/RTO 到状态文件")
    w.add_argument("--rpo", type=float, default=None, help="实测 RPO（秒）")
    w.add_argument("--rto", type=float, default=None, help="实测 RTO（秒）")
    w.add_argument("--backup", default=None, help="归档文件名（仅记录）")
    w.add_argument("--state", default=None, help="状态文件路径")

    r = sub.add_parser("render", help="渲染 Prometheus 文本")
    r.add_argument("--state", default=None)

    v = sub.add_parser("verify", help="校验是否超阈值")
    v.add_argument("--state", default=None)

    args = p.parse_args(argv)
    state = _state_path(getattr(args, "state", None))

    if args.cmd == "write":
        _write(state, args.rpo, args.rto, args.backup)
        return 0

    if args.cmd == "render":
        data = _load(state)
        print(_prom_text(data), end="")
        return 0

    if args.cmd == "verify":
        data = _load(state)
        rpo = data.get("rpo_seconds")
        rto = data.get("rto_seconds")
        ok_rpo = rpo is not None and float(rpo) <= RPO_THRESHOLD
        ok_rto = rto is not None and float(rto) <= RTO_THRESHOLD
        print(f"rpo_seconds={rpo} (<= {RPO_THRESHOLD}: {'PASS' if ok_rpo else 'FAIL'})")
        print(f"rto_seconds={rto} (<= {RTO_THRESHOLD}: {'PASS' if ok_rto else 'FAIL'})")
        return 0 if (ok_rpo and ok_rto) else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
