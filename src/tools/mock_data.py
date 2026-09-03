"""Mock 业务数据（订单 / 物流 / 知识库）。

完全自托管约束下，Phase 1 使用模拟数据（mock）代替真实电商 API；接入真实 API 属生产目标。
数据按 tenant_id 隔离，保证"两租户同名 order_id / policy"不串租户。
"""
from __future__ import annotations

import re
import time

# --- 知识库（政策文档，租户作用域）---
KNOWLEDGE_BASE: list[dict] = [
    {
        "tenant_id": "TENANT-A",
        "doc_id": "return_policy_A",
        "title": "退货政策（租户A）",
        "content": ("自签收之日起 7 天内，商品未经使用且包装完好可申请退货。"
                    "生鲜、定制类不支持无理由退货。退货需买家先填写退货申请。"),
    },
    {
        "tenant_id": "TENANT-A",
        "doc_id": "refund_policy_A",
        "title": "退款政策（租户A）",
        "content": ("退款在审批通过后原路退回，通常 3-5 个工作日到账。"
                    "仅支持退款到原支付账户，不支持转交他人账户。"),
    },
    {
        "tenant_id": "TENANT-A",
        "doc_id": "shipping_policy_A",
        "title": "物流政策（租户A）",
        "content": ("物流默认发顺丰速运，预计 2-3 天送达。可联系客服查询实时轨迹。"),
    },
    {
        "tenant_id": "TENANT-B",
        "doc_id": "return_policy_B",
        "title": "退货政策（租户B）",
        "content": ("自签收之日起 30 天内支持无理由退货，商品需不影响二次销售。"
                    "退货时需上传退货凭证。"),
    },
]

# 以导入时刻为基准生成"最近"时间戳，使退款/退货资格窗口对测试脚本始终有效。
_NOW = time.time()
_DAY = 86400


def _recent(days: float) -> float:
    return _NOW - days * _DAY


# --- 订单（租户作用域；两租户可存在同名 order_id）---
# 附加字段：created_at/delivered_at（资格窗口）、items[].category（商品类别）用于资格判断。
ORDERS: list[dict] = [
    {
        "tenant_id": "TENANT-A",
        "order_id": "ORD-001",
        "user_id": "USER-001",
        "status": "delivered",
        "total_amount": 299.00,
        "items": [{"name": "无线耳机", "sku": "SKU-001", "price": 299.00, "quantity": 1,
                   "category": "electronics"}],
        "carrier": "SF Express",
        "tracking_no": "SF1234567890",
        "created_at": _recent(10),
        "delivered_at": _recent(2),
    },
    {
        "tenant_id": "TENANT-A",
        "order_id": "ORD-002",
        "user_id": "USER-001",
        "status": "shipped",
        "total_amount": 520.00,
        "items": [{"name": "智能手表", "sku": "SKU-002", "price": 520.00, "quantity": 1,
                   "category": "electronics"}],
        "carrier": "SF Express",
        "tracking_no": "SF2234567890",
        "created_at": _recent(3),
        "delivered_at": None,
    },
    {
        "tenant_id": "TENANT-A",
        "order_id": "ORD-003",
        "user_id": "USER-001",
        "status": "delivered",
        "total_amount": 88.00,
        "items": [{"name": "进口生鲜礼盒", "sku": "SKU-003", "price": 88.00, "quantity": 1,
                   "category": "fresh"}],
        "carrier": "SF Express",
        "tracking_no": "SF3234567890",
        "created_at": _recent(10),
        "delivered_at": _recent(2),
    },
    {
        "tenant_id": "TENANT-A",
        "order_id": "ORD-004",
        "user_id": "USER-001",
        "status": "delivered",
        "total_amount": 399.00,
        "items": [{"name": "羽绒服", "sku": "SKU-004", "price": 399.00, "quantity": 1,
                   "category": "apparel"}],
        "carrier": "SF Express",
        "tracking_no": "SF4234567890",
        "created_at": _recent(80),
        "delivered_at": _recent(70),
    },
    {
        "tenant_id": "TENANT-B",
        "order_id": "ORD-001",
        "user_id": "USER-B1",
        "status": "delivered",
        "total_amount": 159.00,
        "items": [{"name": "蓝牙音箱", "sku": "SKU-B01", "price": 159.00, "quantity": 1,
                   "category": "electronics"}],
        "carrier": "YTO Express",
        "tracking_no": "YT0987654321",
        "created_at": _recent(5),
        "delivered_at": _recent(2),
    },
]

# --- 物流轨迹（租户作用域）---
SHIPPING: list[dict] = [
    {
        "tenant_id": "TENANT-A",
        "order_id": "ORD-001",
        "tracking_no": "SF1234567890",
        "events": [
            {"time": "2026-01-14T09:00:00", "status": "已揽收"},
            {"time": "2026-01-15T14:30:00", "status": "已签收"},
        ],
    },
]


def get_order(tenant_id: str, order_id: str) -> dict | None:
    for o in ORDERS:
        if o["tenant_id"] == tenant_id and o["order_id"] == order_id:
            return o
    return None


def get_shipping(tenant_id: str, order_id: str) -> dict | None:
    for s in SHIPPING:
        if s["tenant_id"] == tenant_id and s["order_id"] == order_id:
            return s
    return None


# 每篇文档的关键词（用于中文关键词检索）
POLICY_KEYWORDS: dict[str, list[str]] = {
    "return_policy_A": ["退货"],
    "refund_policy_A": ["退款"],
    "shipping_policy_A": ["物流", "快递", "运费"],
    "return_policy_B": ["退货"],
}


def search_policy(tenant_id: str, query: str) -> list[dict]:
    """关键词检索该租户的知识库文档。返回带租户过滤的结果（Phase 1 用关键词匹配）。

    中文无空格分词，采用关键词命中匹配；命中数为 0 时不返回该文档（检索为空）。
    """
    import re
    docs = [d for d in KNOWLEDGE_BASE if d["tenant_id"] == tenant_id]
    q = re.sub(r"[\s，。、,:：?!？]", "", query)
    if not q:
        return docs
    scored = []
    for d in docs:
        kw = POLICY_KEYWORDS.get(d["doc_id"], [])
        hits = sum(1 for k in kw if k in q)
        if hits > 0:
            scored.append((hits, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored]
