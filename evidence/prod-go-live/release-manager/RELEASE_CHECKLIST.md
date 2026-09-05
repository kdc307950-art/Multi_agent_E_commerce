# RELEASE_CHECKLIST — RC3 生产发布清单（冻结版）

> **产出国角色**: release-manager（发布经理）· rc3-prod-readiness 团队 · 任务 t4 (attempt 664564c8-1626-4021-b613-3b908545fe40)
> **目标分支**: `codex/prod-readiness`（自 `release/v1.0.0-rc3`=3ccab5c 创建）；**`main` 未改动**
> **生成时间**: 2026-09-05 (+08:00)
> **依据**: `evidence/prod-go-live/release-manager/BASELINE_LOCK.md`、`evidence/prod-go-live/deploy-engineer/IMAGE_DIGESTS.json`、`DEPLOY_BASELINE.md`、`MIGRATE_VERIFY.md`、`README.md`、`PROJECT_STATUS.md`、`GO_NO_GO.md`、`ROLLBACK_PAUSE_TAKEOVER.md`

> **核心论断（已在 T0.1 绑定，见 BASELINE_LOCK.md）**: `release/v1.0.0-rc3` = annotated tag（对象 `d1d2867`）→ commit `3ccab5c` == `HEAD`；工作区 clean；远端 rc3 tag 因 SSH/权限失败 exit 128 不可读（证据缺口，需受控环境补测）。

---

## §0 分支与冻结规则（本清单第一优先）

### 0.1 分支状态
- 新分支 **`codex/prod-readiness`** 已从 rc3 commit **`3ccab5c`** 创建并 checkout；HEAD=`3ccab5c`。
- **`main` 保持不变**：实测 `git rev-parse main` == `3ccab5c`（与创建前完全一致，未被改动）。

### 0.2 🔒 生产修改冻结规则（🔴 强制，作为发布红线）
> **生产修改禁止直接上 `main`; 一律进 `codex/prod-readiness`。**

具体条款（release-manager 定稿）：
1. **`main` 冻结**: 任何**生产相关改动**（`src/`、`deploy/`、`docker-compose.prod.yml`、`frontend/`、`infrastructure/*`、迁移、发布证据）**不得直接提交到 `main`**。
2. **唯一合入通道 = `codex/prod-readiness`**: 所有生产改动先落在 `codex/prod-readiness` 分支，经发布经理/评审定稿由**受控合并**（PR/review + 证据留痕）进入。
3. `main` 仅保留**与生产无关**的内容与历史发布 tag 锚点；**发布 tag（`release/v1.0.0-rc*` / `baseline-prod-*`）不可移动/删除**。
4. 违反者：变更拒绝合并并记审计；发布经理复核后方可合入。
5. 回滚/热修一律从 `codex/prod-readiness` 发起，不在 `main` 上直接改。

---

## §1 代码 commit

| 项 | 值 | 说明 |
|---|---|---|
| 发布候选（HEAD） | **`3ccab5c`** | `git rev-parse HEAD`；== `release/v1.0.0-rc3^{commit}` == `main` == `origin/main` |
| 运行面（镜像代码）基线 | **`cde30fb`** | rc3 为 docs/tests/gitignore-only 收口，**未改动 src/deploy 运行面**；图片代码基线沿用 rc2 即 `cde30fb`（IMAGE_DIGESTS.json release_baseline.commit） |
| 迁移修复 commit | **`696444a`** | `fix(migrations): shipping_events 复合外键修复`（全新 prod-like 库 clean migrate exit 0） |
| 接受运行面 + 部署配置 | `8ddca48` | 140 文件; `cd743d3` 补 `BUSINESS_DATA_BACKEND=postgres` |
| **tag 对象锚（定稿）** | rc1=`2120c4b`→`cd743d3`；rc2=`290b830`→`fa7c9a3`；rc3=`d1d2867`→`3ccab5c`(==HEAD) | 均为 annotated tag 对象；`bca4861` 为 rc2 谱系早期 commit 祖先，非 rc2 锚 |

### 基线 commit 链（线性，至 HEAD）
`7941246`(功能基线, baseline-prod-1) → `ad168ef`/`8be2d97`/`03819a8`/`3268a1c`(可复现构建) → `696444a`(迁移复合外键修复) → `8ddca48`(接受运行面+部署配置) → `cd743d3`(BUSINESS_DATA_BACKEND=postgres, **rc1 锚**) → `cde30fb`(T1/T7 发布基线证据+生产栈健康, 运行面基线) → `59e37f2`(rc2 定稿历史) → `bca4861`(rc2 谱系早期"阶段一 rc2 基线收口" **commit, 祖先；非 rc2 锚**) → `fa7c9a3`(**rc2 tag 目标 commit**) → `602243f`(rc3 基线收口) → `0a3fff6`(rc3 测试加固) → **`3ccab5c`**(rc3 基线口径修正 = HEAD = rc3 tag 目标)

---

## §2 镜像 digest（工作树构建 · 非字节级复现）

| 服务 | repo digest | 构建上下文 / 基础镜像 |
|---|---|---|
| api / worker / migrate | `after-sales-prod-api@sha256:5d39f030697e1b00ef83fb22a3358ddd2f923be6752a082d9f31d20f4039f293` | 仓库根 `.` / Dockerfile；base `python:3.12-slim@sha256:78387bc...` 钉定 |
| frontend | `after-sales-prod-frontend@sha256:12c35ff738c4b29fba0562c7677d086ab15516cbcaf6a2b53f77dc22b2886cad` | `./frontend` / `frontend/Dockerfile`；base `node:20-alpine@sha256:fb4cd12c...` 钉定 |

> ⚠️ **诚实边界**: 二者均用于**工作树构建、代码==基线（含迁移修复 696444a）、非 `git archive` clean-context 字节级复现产物**。`docker build` 的 stdout exit 1 仅为 BuildKit 进度写入 stderr 被 PowerShell 误读（镜像实际成功命名并 unpack）。**字节级冷构建 = 部署期执行项 / BLOCKED-需 Docker engine 可连接**（见 GO_NO_GO G2）。放量前须在具备 engine 的受控环境跑 `git archive release/v1.0.0-rc3 | tar -x` + `compose build --no-cache --provenance=false --sbom=false` 后刷新本表。

---

## §3 迁移版本

| 项 | 值 | 说明 |
|---|---|---|
| schema 版本 | **无数字 schema 版本号** | 业务 schema 全为 `CREATE TABLE IF NOT EXISTS` 幂等建表 |
| 迁移定义 commit | **`696444a`** | 基线链祖先；含 `shipping_events` 复合外键修复 |
| checkpoint 驱动 | **`langgraph-checkpoint-postgres==3.1.2`** | `requirements-lock.txt:36` 钉定；官方 `checkpoint_migrations` 表结构由该依赖迁移机制维护（非项目代码） |
| 全新库 clean migrate | ✅ exit 0 | `MIGRATE_VERIFY.md`：全新 prod-like 库 `migrate_cli` 17 表/复合 FK 正确/RLS FORCE 生效/无 InvalidForeignKey |

---

## §4 配置版本

| 项 | 值 | 说明 |
|---|---|---|
| `deploy_config_version` | **`0.1.0`** | `src/config.py:37` `deploy_config_version: str = "0.1.0"` |
| 三处一致性 | ✅ | `src/config.py` == `DEPLOY_CONFIG_VERSION`（`deploy/.env.production`）== compose 默认 `${DEPLOY_CONFIG_VERSION:-0.1.0}`（GO_NO_GO §四#4） |

---

## §5 测试报告

| 项 | 值 |
|---|---|
| 全量 pytest | **390 passed / 34 skipped**（424 collected, 53.83s, EXIT=0；**发布基线环境实测，见 `evidence/prod-go-live/test-runner/pytest_full_rerun.log`**） |
| 34 skipped 构成 | **33 项 PostgreSQL 数据面**（无 `DATABASE_URL`）+ **1 项 CrewAI 真实调用链**（`test_hardening_acceptance.py:384`） |
| 修正说明 | 先前 `320 passed, 34 skipped, 70 errors` 均为受限沙箱清理 `tmp_path` 的 `PermissionError [WinError 5]` 环境问题（70 errors 均环境问题、0 真失败；发布基线环境无此限制），故修正为 **390/34** |

> 来源: `README.md` §测试、`PROJECT_STATUS.md` §边界1；与 `image` 构建/`go` 判定无冲突（边界1 单元测试口径）。

---

## §6 回滚版本

| 项 | 值 |
|---|---|
| 回滚 git ref | **`release/v1.0.0-rc2`**（上一发布候选/已知良好版本） |
| 回滚机制 | `bash deploy/scripts/rollback.sh <db-dump> <git-ref>`；回滚 = 恢复快照(RPO) + 应用版本回退 |
| 实跑记录 | `deploy/drills/records/DR-20260904030354-rollback.md`（t6 实跑通过）；前条件先做加密备份 |
| rc2 锚定 | ✅ **已定稿（release-manager 2026-09-05 采纳，经队长 git 复测确认）**：`release/v1.0.0-rc2` = annotated tag 对象 `290b830` → commit `fa7c9a3`（rc2 文档收口尖端）；`bca4861` 为 rc2 谱系早期"阶段一 rc2 基线收口" commit（祖先），**不作为 rc2 锚**。回滚前请以实际 tag 对象 `290b830` 为准核对（详见 `BASELINE_LOCK.md` §4） |
| 注意 | rc3 运行面 == rc2（docs/tests/gitignore-only），应用版本回退到 rc2 **不影响运行面**；回滚主要为数据快照恢复 + 版本指针回退 |

> 回滚红线（`GO_NO_GO`/`ROLLBACK_PAUSE_TAKEOVER` §1.4）：回滚**必须实机执行并逐项复验**，不得把"设计好的回滚步骤"当"已回滚"实证。

---

## §7 分离变量与校验动作（放量前；替代旧"统一不变量"）

**分离变量（RC3 口径 · 定稿）**：**不再假设四者相等**。`release-tag.commit(rc3) = 3ccab5c`（发布证据/收口提交）；`runtime-surface baseline = cde30fb`（src/deploy 运行面；rc2/rc3 未改，== `IMAGE_DIGESTS.release_baseline.commit`）；`migrations.schema source = 696444a`（migrations.py）；`config_version = 0.1.0`。**tag 所在 commit 与运行面基线允许不同**（rc3 为 docs/tests/gitignore-only 收口）。

**校验动作**（放量前，须分别核对、不可假设四者相等，见 GO_NO_GO §四）：
1. `git rev-parse release/v1.0.0-rc3^{commit}` == `3ccab5c`（已在 T0.1 验证 ✅）。
2. `IMAGE_DIGESTS.release_baseline.commit` == `cde30fb`（运行面基线）。
3. 迁移 `696444a` 的 `migrations.py` 构成镜像内 schema；digest 与 tag 一一对应。
4. `deploy_config_version` == `0.1.0` 三处一致。
5. 全新库 migrate exit 0 + 全容器 healthy 配套记录（生产栈运行时 RLS/告警复验 = 部署期执行项）。
6. **字节级 clean-context 重建 & digest 刷新**（需 Docker engine 可连接）。

---

## §8 结论

- 分支 `codex/prod-readiness`（自 `3ccab5c`）已建立；`main` 未改动。
- 发布清单已按 T0.2 六要素（代码 commit / 镜像 digest / 迁移版本 / 配置版本 / 测试报告 / 回滚版本）如实记录。
- 🔴 生产修改冻结规则已写入 §0.2：**生产修改禁止直接上 `main`, 一律进 `codex/prod-readiness`**。
- **未**移动/删除任何 tag 或改写 `main`。

- 产出行: `release-manager`（t4）· 发布清单（冻结版）
