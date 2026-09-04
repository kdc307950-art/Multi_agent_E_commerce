# 灾备密钥管理说明（DR Key Management）

> 配套：`deploy/scripts/backup_encrypted.sh`、`deploy/scripts/restore_drill.sh`、
> `deploy/scripts/preview_backup_scheduler.sh`、`docker-compose.preview.yml`、`OPS_RUNBOOK.md`。
> 定位：**加密备份**是对 PostgreSQL 备份的对称加密；密钥管理是本方案安全性的前提。
> 遵循《代理宪法》第一层安全红线：密钥一律由服务器环境注入，绝不硬编码 / 打印 / 写日志 / 提交仓库。

---

## 1. 密钥清单

| 密钥 / 机密 | 用途 | 注入方式 | 默认/示例 |
|-------------|------|----------|-----------|
| `BACKUP_ENC_KEY` | 备份归档的**对称加密密钥**（aes-256-cbc + PBKDF2）；同时用于解密恢复 | 服务器环境变量（宿主机 `deploy/.env.preview` 或系统 env） | `openssl rand -base64 32`（无默认） |
| `BACKUP_ROLE_PASSWORD` | **最小权限备份角色** `backup_role` 的口令（仅 LOGIN + CONNECT + USAGE + SELECT） | 服务器环境变量 | 随机强口令（无默认） |
| `POSTGRES_PASSWORD` | 迁移角色 `migrator`（owner/DDL）口令；仅用于迁移/恢复重放 | 服务器环境变量 | 已有（无默认） |

> `BACKUP_STASH`：不要把 `BACKUP_ENC_KEY` 与备份归档放在**同一主机**（否则失去"异机加密副本"的防单点意义）；
> 至少把密钥存到密钥管理/受控 vault（自托管 Bitwarden/age 恢复私钥等）或第二个受控位置，并纳入访问审计。

## 2. 密钥生成（一次性，运维执行）

```bash
# 对称加密密钥（备份用；建议 32 字节随机，base64 编码作为 passphrase）
openssl rand -base64 32
# 或在部署机上直接写入 .env.preview（受控，权限 600）：
grep -q '^BACKUP_ENC_KEY=' deploy/.env.preview || \
  echo "BACKUP_ENC_KEY=$(openssl rand -base64 32)" >> deploy/.env.preview
chmod 600 deploy/.env.preview
```

最小权限备份角色口令（随机强口令）同样生成并写入 `deploy/.env.preview`：
```bash
openssl rand -hex 32   # 作为 BACKUP_ROLE_PASSWORD
```

## 3. 注入与失效

- **绝不硬编码**：`{{...}}` 占位符、仓库内明文、日志、`docker inspect`、ps 输出都不能出现明文密钥。
- **check_secrets.sh 强制**：`preview/production` 下 `BACKUP_ENC_KEY` 与 `BACKUP_ROLE_PASSWORD` 缺失/占位即 fail-closed 拒绝部署。
- **轮换**：更新 `BACKUP_ENC_KEY` 后，旧归档将无法解密；轮换须在**窗口内保留旧密钥**（并存于受控 vault），
  并重新对全部在营归档重加密或用新密钥重打一次全量备份。建议最长 90 天轮换一次，且每次轮换触发一次恢复演练。
- **泄露处置**：任一字面量泄露 → 立即轮换，并重打加密备份；如密钥与归档同机残留，需迁移归档到受控异机。

## 4. 明文归档不落盘

`pg_dump` 明文输出**经管道直达 `openssl enc`** 加密，磁盘上只存在 `.dump.enc`（密文）+ `.dump.enc.sha256`（校验和）
+ `.manifest.csv`（元数据）。解密恢复时的临时明文 `.dump` 用完即删，且仅存在于临时目录。

## 5. 校验与证据

- **SHA-256 校验和**：每份 `.dump.enc` 配套 `.dump.enc.sha256`；恢复前须复核（`restore_drill.sh` 验证 `sha256sum`）。
- **密文可解**：`restore_drill.sh` 用 `openssl enc -d` 验证可用同一密钥解密，且密文头部不含明文 `PGDMP` 标记。
- **最小权限可验证**：`backup_role` 仅 CONNECT/SELECT，无任何写/DDL 权限（见 `restore_drill.sh`/迁移 SQL 与 `scripts/` 下的证据采集）。

## 6. 操作红线

1. 备份/恢复脚本与 sidecar 一律用 `backup_role`（只读），**绝不用 owner/超级用户**，除非恢复重放对象（那是在临时库、用迁移角色）。
2. 解密密钥只经 `BACKUP_ENC_KEY` 环境变量注入；脚本绝不 `echo` 它，也不把 URL/password 写入日志。
3. 密钥不进入 `git`；`.env.preview` 已在 `.gitignore`，`deploy/backups/*.enc`、`deploy/drills/records/*.enc` 等产物也应 gitignore（见下）。
4. 恢复演练**必须**实测并记录 RPO/RTO 与采样校验结果，禁止以"同机 pg_dump 当结论"。
