# run_backup_restore.ps1 —— [D2] PostgreSQL 备份/恢复演练（PowerShell 实现，免 bash）。
#
# 流程（与 deploy/drills/drill_pg_backup_restore.sh 对齐）：
#   备份当前库 → 写入一个标记记录 → 从备份恢复到临时库 → 校验标记记录被排除且 TENANT-A 保留
#   → 计算 RPO（备份周期基线 15min，可覆盖）与 RTO（恢复耗时）→ 写 JSON + Markdown 记录。
#
# 用法（在仓库根、具备 Docker + 已启动 after-sales-preview 栈的服务器上执行）：
#   pwsh deploy/drills/pwsh-runbook/run_backup_restore.ps1
#   pwsh deploy/drills/pwsh-runbook/run_backup_restore.ps1 -RpoSeconds 900
#
# 安全：不打印任何口令；仅解析 POSTGRES_USER / POSTGRES_DB（非敏感）。恢复默认输出到
#   deploy/drills/records/drill-pg-backup-<ts>.dump；记录写到 deploy/drills/records/。

[CmdletBinding()]
param(
    [int]$RpoSeconds = 900,          # RPO 基线：15 分钟=900s；若每次实拍备份周期，改为实测值
    [string]$BackupFile              # 可选：显式指定备份文件名（缺省自动命名）
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Docker
Assert-EnvFile

# 确保输出目录存在
New-Item -ItemType Directory -Force -Path $script:RecordsDir | Out-Null
New-Item -ItemType Directory -Force -Path $script:BackupsDir  | Out-Null

$dbUser = Get-EnvValue 'POSTGRES_USER'; if ([string]::IsNullOrEmpty($dbUser)) { $dbUser = 'migrator' }
$dbName = Get-EnvValue 'POSTGRES_DB';   if ([string]::IsNullOrEmpty($dbName))   { $dbName = 'langgraph' }
$restoreDb = 'langgraph_restore_test'
$ts = Get-Date -Format 'yyyyMMddHHmmss'
$backup = if ($BackupFile) { $BackupFile } else { Join-Path $script:RecordsDir "drill-pg-backup-$ts.dump" }

Write-Step "D2: 备份当前库 $dbName"
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','pg_dump','-U',$dbUser,'-Fc',$dbName) -StdOutFile $backup
if ($r.ExitCode -ne 0 -or -not (Test-Path $backup)) {
    Write-Fail ("备份失败：{0}" -f $r.Stderr)
    exit 1
}
$backupBytes = (Get-Item $backup).Length
Write-Ok ("备份完成：{0}（{1} bytes）" -f $backup, $backupBytes)

Write-Step "D2: 写入一个标记记录（模拟快照点之后的变更）"
$marker = 'pg-drill-' + [DateTimeOffset]::Now.ToUnixTimeMilliseconds().ToString()
$ins = "INSERT INTO tenants(id,name,status,created_at) VALUES ('$marker','drill','active', extract(epoch from now())) ON CONFLICT DO NOTHING;"
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d',$dbName,'-v','ON_ERROR_STOP=1','-c',$ins)
Write-Stderr $r
if ($r.ExitCode -ne 0) { Write-Fail "标记写入失败"; exit 1 }
Write-Ok ("已写入标记：{0}" -f $marker)

Write-Step "D2: 从备份恢复到临时库 $restoreDb（计时 RTO）"
# DROP/CREATE DATABASE 不能在同一事务内执行；拆成两个独立 -c（各自 autocommit）。
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d','postgres','-v','ON_ERROR_STOP=1','-c',"DROP DATABASE IF EXISTS $restoreDb;")
Write-Stderr $r
if ($r.ExitCode -ne 0) { Write-Fail "重建临时库失败（DROP）"; exit 1 }
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d','postgres','-v','ON_ERROR_STOP=1','-c',"CREATE DATABASE $restoreDb;")
Write-Stderr $r
if ($r.ExitCode -ne 0) { Write-Fail "重建临时库失败（CREATE）"; exit 1 }

# 用 docker cp + 文件路径恢复（规避 Windows stdin 二进制经 compose exec 的 CRLF 转换损坏归档）。
$restore = Restore-DumpToDatabase -BackupFile $backup -DBUser $dbUser -TargetDb $restoreDb
$rto = $restore.Elapsed
$r = $restore
Write-Stderr $r
if ($r.ExitCode -ne 0) { Write-Fail ("恢复失败：{0}" -f $r.Stderr); exit 1 }
Write-Ok ("恢复耗时 RTO = {0:N3}s" -f $rto)

Write-Step "D2: 校验——标记记录（快照点后变更）不应存在；备份点数据应完整"
$q1 = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d',$restoreDb,'-tAc',"SELECT count(*) FROM tenants WHERE id='$marker';")
$q2 = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d',$restoreDb,'-tAc',"SELECT count(*) FROM tenants WHERE id='TENANT-A';")
$q3 = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d',$restoreDb,'-tAc',"SELECT count(*) FROM tenants WHERE id='TENANT-B';")
$markerInRestore = 0; $tenantAInRestore = 0; $tenantBInRestore = 0
[void][int]::TryParse($q1.Stdout.Trim(), [ref]$markerInRestore)
[void][int]::TryParse($q2.Stdout.Trim(), [ref]$tenantAInRestore)
[void][int]::TryParse($q3.Stdout.Trim(), [ref]$tenantBInRestore)
Write-Ok ("marker_in_restore={0} tenants_preserved(TENANT-A)={1} TENANT-B={2}" -f $markerInRestore, $tenantAInRestore, $tenantBInRestore)

# 清理临时库 + 回滚刚才写入的标记（避免污染 live tenants 表）
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d','postgres','-c',"DROP DATABASE IF EXISTS $restoreDb;")
Write-Stderr $r
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','psql','-U',$dbUser,'-d',$dbName,'-v','ON_ERROR_STOP=1','-c',"DELETE FROM tenants WHERE id='$marker';")
Write-Stderr $r

$restoreOk = ($markerInRestore -eq 0 -and $tenantAInRestore -ge 1 -and $tenantBInRestore -ge 1)

$data = [ordered]@{
    scenario            = 'D2_pg_backup_restore'
    rpo_seconds         = $RpoSeconds
    rto_restore_seconds = [math]::Round($rto, 3)
    restore_ok          = $restoreOk
    marker_excluded     = $markerInRestore
    tenants_preserved   = $tenantAInRestore
    tenants_preserved_b = $tenantBInRestore
    backup_file         = (Split-Path -Leaf $backup)
    backup_bytes        = $backupBytes
    db_name             = $dbName
    restore_db          = $restoreDb
    rpo_basis           = if ($RpoSeconds -eq 900) { 'baseline_15min' } else { 'measured_override' }
    timestamp           = (Get-Date -Format 'o')
}
$jsonPath = Write-JsonRecord -FileName 'drill-pg-backup-restore.json' -Data $data

# 同时写一份可复核的 Markdown 记录（对齐 record_template.md）
$mdPath = Join-Path $script:RecordsDir "DR-$($ts)-pg-backup-restore.md"
$mdLines = @(
    "# 预发布演练记录 — PostgreSQL 备份恢复（DR-$ts）",
    "",
    "- 演练编号：DR-$ts",
    "- 环境：preview（after-sales-preview）",
    "- 应用 git 引用：<实跑时填>",
    "- 执行人 / 复核人：______ / ______",
    "- 开始时间 / 结束时间：______ / ______",
    "",
    "| # | 演练项 | 操作号/证据ID | 结果 | 时长 | 备注 |",
    "|---|--------|--------------|------|------|------|",
    "| D2 | PostgreSQL 备份恢复 | backup=$(Split-Path -Leaf $backup) | $([bool]$restoreOk) | RTO=$(('{0:N3}' -f $rto))s | RPO=$($RpoSeconds)s（基线≤60min RTO） |",
    "",
    "## 关键证据",
    "- marker_excluded=$markerInRestore（应为 0）",
    "- tenants_preserved(TENANT-A)=$tenantAInRestore（应 >=1）",
    "- backup_file=$(Split-Path -Leaf $backup)",
    "",
    "## 结论",
    "- 是否达到预发布验收：$([bool]$restoreOk)",
    "- 签名：______"
)
$mdLines | Set-Content -Encoding UTF8 -Path $mdPath

if ($restoreOk) {
    Write-Ok ("备份可恢复：RPO=$($RpoSeconds)s（≤15min 基线），RTO={0:N3}s（≤60min 基线），快照点变更已排除。" -f $rto)
    Write-Host "记录: $jsonPath"
    Write-Host "记录: $mdPath"
    exit 0
} else {
    Write-Fail ("备份恢复校验失败：marker_in_restore=$markerInRestore, TENANT-A=$tenantAInRestore。")
    Write-Host "记录: $jsonPath"
    exit 1
}
