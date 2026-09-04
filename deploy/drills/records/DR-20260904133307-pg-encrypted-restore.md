# 预发布演练记录 — 加密备份恢复（DR-20260904133307）

- 演练编号：DR-20260904133307
- 环境：preview（PostgreSQL，DB=langgraph；备份角色 backup_role 仅只读；恢复用迁移/owner 角色）
- 应用 git 引用：3268a1c
- 备份点快照：2026-09-04T13:33:06+0800（epoch=1788499986）

| # | 演练项 | 结果 | 证据 |
|---|--------|------|------|
| E1 | 加密归档（aes-256-cbc + PBKDF2） | PASS | backup_bytes=3568 |
| E2 | SHA-256 校验和 | PASS | langgraph-dr-verify.dump.enc.sha256 |
| E3 | 归档完整性（pg_restore --list） | PASS | 可解析 |
| E4 | 解密恢复（RTO） | FAIL | RTO=.028514217s |
| E5 | 快照点后变更排除（marker） | FAIL | marker_in_restore=? |
| E6 | 采样数据保留（TENANT-A/B） | FAIL | A=0 B=0 |

## RPO / RTO 实测
- RPO（实测数据丢失窗口）：**.490789032s**；RPO 上界（备份周期最坏情况）：900s（阈值 ≤900s）
- RTO（恢复耗时实测）：**.028514217s**（阈值 ≤3600s）

## 关键证据
- backup_file=langgraph-dr-verify.dump.enc；restore_db=langgraph_restore_test
- 密钥仅由环境变量 BACKUP_ENC_KEY 注入，未打印/写日志；备份角色 backup_role 仅只读。

## 结论（如实，勿臆造）
- 是否达到 RPO≤15min / RTO≤60min：复核（实测值见上）
- 签名：drill-runner / security-auditor
