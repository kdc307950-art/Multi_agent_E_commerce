"""候选模型评测包：测试集 + runner。

`evaluate_model(llm)` 对给定模型执行六类通用任务 + 写操作专项评测，返回 `ModelEvaluation`。
只有 `write_op_pass=True`（且模型名在审批链门控正确 fail-closed）的 model id 才允许进入
`HIGH_CONFIDENCE_MODELS`（见 src.llm.capability）。
"""
from __future__ import annotations

from src.llm.eval.cases import ALL_CASES, WRITE_OP_CASES
from src.llm.eval.runner import CaseResult, ModelEvaluation, evaluate_model

__all__ = ["ALL_CASES", "WRITE_OP_CASES", "CaseResult", "ModelEvaluation", "evaluate_model"]
