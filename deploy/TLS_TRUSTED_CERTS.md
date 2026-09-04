# 正式域名 + 受信 CA TLS 接入机制（production · after-sales-prod）

> **定位**：本文件说明生产环境（`after-sales-prod`）如何把当前**自签名** TLS 替换为**受信 CA** 证书链，含工具（`gen_certs.sh` / `verify_tls.sh`）、接入步骤、以及**正式域名与 CA 供应商作为外部输入**的填入点。
> **配套**：`deploy/scripts/gen_certs.sh`（证书生成/导入/Let's Encrypt）、`deploy/scripts/verify_tls.sh`（受信 TLS 验收）、`deploy/nginx/prod.conf`、`docker-compose.prod.yml`、`deploy/OPS_RUNBOOK.md`。
> **生产红线**：正式生产必须使用**受信 CA 证书链**（非自签）。当前仓库默认自签（`CN=preview.local`）→ **BLOCKED_EXTERNAL**，须由业务方提供【正式域名 + 受信 CA 供应商】后方可解锁。

---

## 一、现状核对结论（task item 2 · 已完成）

已逐项核对 `deploy/nginx/prod.conf`（生产 nginx 配置），结论：**结构已符合生产受信 TLS 要求**：

| 核对点 | prod.conf 现状 | 结论 |
|--------|----------------|:---:|
| 80 端口仅保留 /healthz（供容器 healthcheck） | `location = /healthz { return 200 "ok\n"; }` | ✅ 符合 |
| 80 端口其余一律 301 → https | `location / { return 301 https://$host$request_uri; }` | ✅ 符合 |
| 443 端口终结 TLS | `listen 443 ssl;` + `http2 on;` + `ssl_certificate(_key)` + `ssl_protocols TLSv1.2 TLSv1.3` | ✅ 符合 |
| 443 同源代理 /api（浏览器同源访问，API 关 CORS） | `location /api/ { proxy_pass http://api:8000; ... }` | ✅ 符合 |
| 公网不暴露指标 | `location = /api/metrics { return 404; }` | ✅ 符合 |
| 安全响应头（HSTS/X-Content-Type/X-Frame-Options/Referrer-Policy） | `add_header Strict-Transport-Security ...` 等 | ✅ 符合 |

> **唯一待改项**：`prod.conf` 第 29 行 `server_name admin.after-sales.example;` 为**占位域名**，须替换为正式域名（见 §三）。

---

## 二、当前 TLS 状态（自签 → BLOCKED）

- 现有证书：`deploy/secrets/certs/server.crt` / `server.key`，实测为**自签名**：
  - Subject = Issuer = `CN=preview.local`（自签）
  - 有效期至 **2027-09-04**
  - 无受信 CA 链
- nginx 的 `ssl_certificate` 指向容器 `/etc/nginx/certs/server.crt`，`docker-compose.prod.yml` 将 `./deploy/secrets/certs:/etc/nginx/certs:ro` 挂载。故**只要替换 `deploy/secrets/certs/` 下的 `server.crt`（全链）+ `server.key`，重启 nginx 即生效**。
- **受信 TLS 就绪前，生产不可对外暴露 8080/8843**（当前 443 用自签）。

---

## 三、外部输入（业务方提供 · 边界4）

以下为**本机/团队内无法闭环、必须由外部提供**的输入，缺一即无法达成受信 TLS：

| 外部输入 | 说明/要求 | 填入点 |
|----------|----------|--------|
| **① 正式域名** | 生产对外域名（如 `admin.example.com`），须已完成 DNS 解析（A/AAAA 指向本机公网 IP），且该域名将承载前端 + `/api` 同源。 | ① nginx `prod.conf` `server_name`；② 证书 SAN；③ 前端同源基准 |
| **② CA 供应商（三选一）** | 受信 CA：公共 CA（Let's Encrypt / DigiCert / Sectigo 等）/ 企业内网 CA（用内部根签发）/ 已采购的商业证书。 | 证书 `Subject/Issuer` 为受信 CA，非自签；fullchain 含中间链 |

> **由用户提供 / 需用户决策**（本任务不代签、不代选）：
> 1. 生产域名用哪个？
> 2. CA 供应商选哪种（公共 CA / Let's Encrypt / 企业内网 CA / 商业证书）？
> 3. 证书由业务方**现成提供**（fullchain+privkey 文件），还是**由本机自动签发**（Let's Encrypt 走 ACME）？

---

## 四、接入步骤（证书落地 + 替换 deploy/secrets/certs）

### 场景 A：业务方提供现成受信证书（fullchain + privkey）

```bash
# 1) 导入受信 CA 全链 + 私钥 → deploy/secrets/certs/server.{crt,key}
#    fullchain 需含 leaf + 中间链；工具会自动备份旧证书、校验链/SAN/私钥配对。
bash deploy/scripts/gen_certs.sh import /path/to/fullchain.pem /path/to/privkey.pem --domain <正式域名>

# 2) 查看落地结果
bash deploy/scripts/gen_certs.sh status

# 3) 把 nginx prod.conf 的 server_name 设为正式域名（手动编辑 deploy/nginx/prod.conf）
#    或按需用 sed 一键替换：sed -i "s/admin\.after-sales\.example/<正式域名>/" deploy/nginx/prod.conf

# 4) 重启 nginx 使新证书生效（生产栈）
docker compose --env-file deploy/.env.production -f docker-compose.prod.yml restart nginx

# 5) 受信 TLS 验收
bash deploy/scripts/verify_tls.sh \
  --host 127.0.0.1 --https-port 8843 --http-port 8080 \
  --expect-domain <正式域名> --cacert /path/to/受信根.pem
```

### 场景 B：Let's Encrypt 自动签发（需 AC59 通路 + 公网可解析域名）

```bash
# 用 certbot（宿主机或 docker 容器）签发并落地。需要：
#   - 域名已解析到本机；- 80/443 对外可达（HTTP-01）或 DNS 控制（DNS-01）；- 注册邮箱。
bash deploy/scripts/gen_certs.sh letsencrypt <正式域名> --email ops@example.com
# 生成后脚本自动 import 到 deploy/secrets/certs，再按场景 A 的步骤 3/4/5 完成。
```

### 场景 C：企业内网 CA —— 在【内部 CA】侧签发后，按场景 A 的 import 落地。

---

## 五、域名 / CN-SAN 应填项

| 项 | 应填 | 说明 |
|----|------|------|
| `prod.conf` `server_name` | **正式域名**（替换 `admin.after-sales.example` 占位） | nginx SNI/HTTPS 虚拟主机名 |
| 证书 **Subject CN** | 正式域名（或设为正式域名） | 很多客户端回退用 CN；建议 CN=SAN 主域名 |
| 证书 **SAN（subjectAltName）** | 至少含正式域名（`DNS:<正式域名>`），建议追加 `DNS:www.<域名>` 若需要 | **核心**：现代浏览器只认 SAN，CN 不再被信任 |
| 证书有效期 | 覆盖验收/7 天观察期（≥ 观察期） | 避免观察期内过期 |
| 私钥 | 与证书配对的 RSA/ECDSA 私钥 | `gen_certs.sh import` 会校验模数一致 |

> 注：前端构建以 `NEXT_PUBLIC_API_BASE_URL=/api`（同源），故**一个域名**同时承载前端与 `/api`，无需多域；若用多子域，需额外处理 CORS/同源（当前 `ENABLE_CORS=false`）。

---

## 六、验证工具（verify_tls.sh）说明

`deploy/scripts/verify_tls.sh` 逐项校验：
- **[A]** nginx prod.conf 语义：80 仅 healthz/301、443 终结 TLS、同源代理 /api、/api/metrics 内网化；
- **[B]** 运行时：80 `/healthz`=200、80 `/`与`/api/healthz`=301、443 `/api/healthz`=200；
- **[C]** 证书链：leaf 有效期、非自签、链验证（`--cacert` 受信根或 `--require-trusted` 系统信任库）、**SAN 含正式域名**、server_name 一致性。

> 受信链必须提供 `--cacert <受信根.pem>`（或 `--require-trusted` 走系统信任库），否则仅做「有效期内+非自签」并标注「链信任未验证」。任一关键项失败即非零退出（可作放量前闸门）。

---

## 七、与《代理宪法》对齐

- 完全自托管：证书与 ACME 仅经项目方控制边界（自托管 certbot 容器 / 内部 CA）；不自接第三方托管。
- 对外暴露最小化：受信 TLS 就绪前不对外暴露 8080/8843；`/api/metrics` 保持内网化。
- 受信与否用**工具验证**（verify_tls.sh），不凭"看起来是 https"。

---

*版本 v0.1（rollout t4）· 现状核对 + 接入机制 + 外部输入标注。正式域名与 CA 供应商为外部输入（业务方提供）。*
