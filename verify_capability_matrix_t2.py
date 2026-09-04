# -*- coding: utf-8 -*-
"""t2 独立只读验证：能力矩阵白名单解析 + 受限环境 fail-closed 语义。

不运行会覆写 evidence/llm_candidate_eval.json 的 pytest；本脚本只读它（场景4直接引用真实
证据报告路径），并通过临时目录生成受控报告来测场景5（write_op_pass=false 排除语义）。
运行：PYTHONPATH=<项目根> PYTHONUTF8=1 .venv\\Scripts\\python.exe verify_capability_matrix_t2.py
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src.config import Settings  # noqa: E402
from src.llm.capability import (  # noqa: E402
    DEFAULT_DEV_WRITE_MODELS,
    capability_ok,
    resolve_high_confidence_models,
    write_capable_models,
)
from src.llm import build_llm  # noqa: E402

EVIDENCE_REPORT = os.path.join(ROOT, "evidence", "llm_candidate_eval.json")

results = []


def check(item, expected, actual, extra=""):
    ok = (expected == actual)
    results.append((item, ok, extra))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}")
    print(f"      expected={expected!r}")
    print(f"      actual  ={actual!r}")
    if extra:
        print(f"      {extra}")
    return ok


def bool_check(item, expr, extra=""):
    ok = bool(expr)
    results.append((item, ok, extra))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}   expr={expr!r}" + (f"   ({extra})" if extra else ""))
    return ok


def make_settings(**kw):
    base = dict(env="development")
    base.update(kw)
    # 显式传入，避免任何 .env/环境变量干扰
    return Settings(**base)


def write_temp_report(report_dict):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(report_dict, fh, ensure_ascii=False, indent=2)
    return path


def main():
    print("=== t2: 能力矩阵白名单解析与受限环境 fail-closed 验证 ===")
    print(f"真实评测报告路径: {EVIDENCE_REPORT}")

    # ---- 监听真实证据报告的当前状态（只读） ----
    real_report = {}
    if os.path.exists(EVIDENCE_REPORT):
        with open(EVIDENCE_REPORT, encoding="utf-8") as fh:
            real_report = json.load(fh)
    real_wop = {k: v.get("write_op_pass") for k, v in real_report.items() if isinstance(v, dict)}
    print(f"\n[证据报告] 当前模型 -> write_op_pass: {real_wop}")
    bool_check(
        "证据报告包含 self-hosted-model 且 write_op_pass=true（场景4前置）",
        real_report.get("self-hosted-model", {}).get("write_op_pass") is True,
    )

    # ================= 场景 1 =================
    print("\n--- 场景1: dev + 空 high_confidence_models -> DEFAULT_DEV_WRITE_MODELS ---")
    s1 = make_settings(env="development", high_confidence_models="")
    got1 = resolve_high_confidence_models(s1)
    check("场景1: dev 白名单为空 -> DEFAULT_DEV_WRITE_MODELS",
          DEFAULT_DEV_WRITE_MODELS, got1,
          f"默认集={sorted(DEFAULT_DEV_WRITE_MODELS)}")
    bool_check("场景1: capability_ok('gpt-4')=True", capability_ok("gpt-4", s1) is True)
    bool_check("场景1: capability_ok('self-hosted-model')=False(不在默认集)",
               capability_ok("self-hosted-model", s1) is False)

    # ================= 场景 2 =================
    print("\n--- 场景2: preview + 空白名单 -> frozenset() (fail-closed) ---")
    s2 = make_settings(env="preview", high_confidence_models="")
    got2 = resolve_high_confidence_models(s2)
    check("场景2: preview 空白名单 -> frozenset()", frozenset(), got2)
    bool_check("场景2: capability_ok('any-model')=False", capability_ok("any-model", s2) is False)
    bool_check("场景2: write_capable_models 为空", write_capable_models(s2) == set())

    # ================= 场景 3 =================
    print("\n--- 场景3: preview + 白名单含X但无评测报告 -> frozenset() (fail-closed) ---")
    s3 = make_settings(env="preview", high_confidence_models="qwen2.5-max",
                       llm_eval_report_path="")  # 无报告
    got3 = resolve_high_confidence_models(s3)
    check("场景3: preview 白名单含X + 无报告 -> frozenset()", frozenset(), got3)
    bool_check("场景3: capability_ok('qwen2.5-max')=False", capability_ok("qwen2.5-max", s3) is False)
    # 报告路径指向不存在的文件也算"无报告"
    s3b = make_settings(env="preview", high_confidence_models="qwen2.5-max",
                        llm_eval_report_path=os.path.join(ROOT, "nope_missing.json"))
    bool_check("场景3b: 报告路径不存在 -> frozenset()",
               resolve_high_confidence_models(s3b) == frozenset())

    # ================= 场景 4 =================
    print("\n--- 场景4: preview + 白名单含 self-hosted-model + 真实证据报告 write_op_pass=true ---")
    s4 = make_settings(env="preview", high_confidence_models="self-hosted-model",
                       llm_eval_report_path=EVIDENCE_REPORT)
    got4 = resolve_high_confidence_models(s4)
    check("场景4: 生效白名单 = {self-hosted-model}", frozenset({"self-hosted-model"}), got4)
    bool_check("场景4: capability_ok('self-hosted-model')=True",
               capability_ok("self-hosted-model", s4) is True)
    bool_check("场景4: capability_ok('self-hosted-model-or-any-other')=False",
               capability_ok("self-hosted-model-or-any-other", s4) is False)
    bool_check("场景4: capability_ok('qwen2.5-max')=False(不在白名单)",
               capability_ok("qwen2.5-max", s4) is False)
    bool_check("场景4: write_capable_models == {'self-hosted-model'}",
               write_capable_models(s4) == {"self-hosted-model"})

    # 用受控临时报告复检场景4（不依赖并发变化的真实证据文件）
    tmp4 = write_temp_report({"self-hosted-model": {"write_op_pass": True}})
    s4b = make_settings(env="preview", high_confidence_models="self-hosted-model",
                        llm_eval_report_path=tmp4)
    check("场景4b(受控报告): 生效白名单 = {self-hosted-model}",
          frozenset({"self-hosted-model"}), resolve_high_confidence_models(s4b))

    # ================= 场景 5 =================
    print("\n--- 场景5: 评测报告 write_op_pass=false 且该模型在白名单 -> 不进入生效白名单 ---")
    tmp5 = write_temp_report({
        "self-hosted-model": {"write_op_pass": False},
        "good-model": {"write_op_pass": True},
    })
    # 5a: 受限环境（preview）—— 失败模型被排除
    s5 = make_settings(env="preview", high_confidence_models="self-hosted-model,good-model",
                       llm_eval_report_path=tmp5)
    got5 = resolve_high_confidence_models(s5)
    # good-model write_op_pass=true 应保留；self-hosted-model 失败应排除
    check("场景5a(preview): 生效白名单 = {good-model}", frozenset({"good-model"}), got5)
    bool_check("场景5a: capability_ok('self-hosted-model')=False(被排除)",
               capability_ok("self-hosted-model", s5) is False)
    bool_check("场景5a: capability_ok('good-model')=True",
               capability_ok("good-model", s5) is True)
    # 5b: 受限环境 + 白名单仅失败模型 -> 无任何可写模型
    s5b = make_settings(env="preview", high_confidence_models="self-hosted-model",
                        llm_eval_report_path=tmp5)
    check("场景5b(preview): 全失败模型 -> frozenset()", frozenset(),
          resolve_high_confidence_models(s5b))
    bool_check("场景5b: capability_ok('self-hosted-model')=False",
               capability_ok("self-hosted-model", s5b) is False)

    # 5c: 开发环境显式白名单 + 报告写失败 —— 观察实际行为（设计红线一致性）
    s5dev = make_settings(env="development", high_confidence_models="self-hosted-model",
                          llm_eval_report_path=tmp5)
    got5dev = resolve_high_confidence_models(s5dev)
    print("\n[差异观察] 开发环境显式白名单 + 报告 write_op_pass=false 的实际行为:")
    print(f"      resolve_high_confidence_models = {sorted(got5dev)!r}")
    print(f"      capability_ok('self-hosted-model') = {capability_ok('self-hosted-model', s5dev)}")
    # 记录为观察项（不判 PASS/FAIL，交由队长结合设计意图裁定）

    # 5d: 开发环境 + 白名单含good-model + 报告good=true —— 交集仍生效
    s5dev2 = make_settings(env="development", high_confidence_models="self-hosted-model,good-model",
                           llm_eval_report_path=tmp5)
    got5dev2 = resolve_high_confidence_models(s5dev2)
    print(f"\n[差异观察] dev 白名单={sorted({'self-hosted-model','good-model'})} 实际生效={sorted(got5dev2)!r}")

    # ================= 场景 6 =================
    print("\n--- 场景6: 端到端——非白名单模型 capability_ok=False（写操作应被拒绝） ---")
    # 用真实报告 + dev 环境，self-hosted-model 不在 dev 默认白名单里
    s6 = make_settings(env="development", high_confidence_models="",
                       llm_eval_report_path=EVIDENCE_REPORT)
    bool_check("场景6(dev默认): capability_ok('self-hosted-model')=False",
               capability_ok("self-hosted-model", s6) is False)
    # build_llm 注入的实例也应拒绝该模型写操作（BaseLLM.capability_ok）
    llm6 = build_llm(s6)
    # llm6.model 为 'self-hosted-model'（config 默认），在 dev 默认白名单外 -> capability_ok 属性=False
    bool_check("场景6: llm6.model='self-hosted-model' 且不在 dev 默认白名单",
               llm6.model == "self-hosted-model" and llm6.capability_ok is False)
    bool_check("场景6: build_llm(dev默认).is_write_capable('self-hosted-model')=False",
               llm6.is_write_capable("self-hosted-model") is False)
    bool_check("场景6: build_llm(dev默认).is_write_capable('gpt-4')=True",
               llm6.is_write_capable("gpt-4") is True)
    # 受限环境 build_llm 注入空白名单应拒绝一切
    s6r = make_settings(env="preview", high_confidence_models="",
                        llm_eval_report_path=EVIDENCE_REPORT)
    llm6r = build_llm(s6r)
    bool_check("场景6: preview build_llm.is_write_capable('gpt-4')=False",
               llm6r.is_write_capable("gpt-4") is False)
    bool_check("场景6: preview build_llm.is_write_capable('self-hosted-model')=False",
               llm6r.is_write_capable("self-hosted-model") is False)

    # ---- 总结 ----
    print("\n=== 结果汇总 ===")
    nd = sum(1 for _, ok, _ in results if ok)
    nf = len(results) - nd
    for item, ok, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {item}")
    print(f"\n总计: {len(results)} 项断言; 通过 {nd}, 失败 {nf}")
    return 1 if nf else 0


if __name__ == "__main__":
    raise SystemExit(main())
