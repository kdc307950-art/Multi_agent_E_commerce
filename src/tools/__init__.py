"""工具包：带租户校验的电商能力适配器 + CrewAI/MCP 集成。

所有业务节点/子智能体经 EcommerceAdapter 访问订单/物流/资格，禁止绕过。
当前数据源为 mock（完全自托管），可替换为项目自管电商 API。
CrewAI/MCP 集成默认关闭（crewai_enabled=False），走内置确定性路径。
"""
from src.tools.adapter import (
    AdapterError,
    EcommerceAdapter,
    EligibilityResult,
    OrderRecord,
    build_adapter,
)
from src.tools.crewai_adapter import (
    BUSINESS_TOOL_SCHEMAS,
    CrewAIIntegrationError,
    CrewAIToolRouter,
    MockCrewAIBackend,
    build_crewai_router,
)

__all__ = [
    "AdapterError",
    "EcommerceAdapter",
    "EligibilityResult",
    "OrderRecord",
    "build_adapter",
    "BUSINESS_TOOL_SCHEMAS",
    "CrewAIIntegrationError",
    "CrewAIToolRouter",
    "MockCrewAIBackend",
    "build_crewai_router",
]
