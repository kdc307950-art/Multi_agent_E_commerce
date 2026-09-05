import sqlite3
db = r"D:\software\PythonProject1\PythonProject\Multi_agent_E_commerce\data\verify-sandbox\sandbox_gateway.db"
c = sqlite3.connect(db)
rows = c.execute("SELECT tenant_id, idempotency_key, external_txn_id, status, amount FROM gateway_txns ORDER BY tenant_id, idempotency_key").fetchall()
print("gateway_txns total =", len(rows))
for r in rows:
    print("  ", r)
print("A/rt-key-aaa count =", c.execute("SELECT COUNT(*) FROM gateway_txns WHERE tenant_id='TENANT-A' AND idempotency_key='rt-key-aaa'").fetchone()[0])
print("A/rt-key-shared count =", c.execute("SELECT COUNT(*) FROM gateway_txns WHERE tenant_id='TENANT-A' AND idempotency_key='rt-key-shared'").fetchone()[0])
print("B/rt-key-shared count =", c.execute("SELECT COUNT(*) FROM gateway_txns WHERE tenant_id='TENANT-B' AND idempotency_key='rt-key-shared'").fetchone()[0])
print("reversals =", c.execute("SELECT COUNT(*) FROM gateway_reversals").fetchone()[0])
c.close()
