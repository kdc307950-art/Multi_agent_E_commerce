# run_rollback.ps1 —— 回滚 preview（PowerShell 实现，免 bash）。对应 deploy/scripts/rollback.sh。
#
# 安全优先：数据库向前迁移幂等（CREATE IF NOT EXISTS / 幂等函数），故"回滚"=
#   ① 从变更前备份恢复数据（RPO）；② 应用重建到变更前 git 引用（版本回退）。
# 不提供"部分回滚到中间 schema"，与生产基线"先备份、再变更、恢复靠快照"一致。
#
# 流程：
#   0. 回滚前快照当前状态（pre-rollback-<ts>.dump，可逆性）
#   1. 从指定备份恢复数据库（pg_restore --clean --if-exists）
#   2. 若提供 -GitRef：compose down → git checkout <ref> -- . → compose build --pull → up -d
#      否则跳过应用重建（仅恢复数据）。
#   3. 健康复验（/api/healthz 200）；写 JSON + Markdown 记录。
#
# 用法：
#   pwsh deploy/drills/pwsh-runbook/run_rollback.ps1 -BackupFile deploy/backups/langgraph-<ts>.dump
#   pwsh deploy/drills/pwsh-runbook/run_rollback.ps1 -BackupFile ... -GitRef <commit>
#
# 警告：本脚本会对 preview 数据库做恢复（--clean --if-exists），并可能 compose down/up 重建应用，
#       仅在对故障回滚时调用。不打印任何口令。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupFile,   # 恢复源备份（如 deploy/backups/langgraph-<ts>.dump）
    [string]$GitRef,                                    # 可选：应用回退到的 git 引用/commit
    [switch]$SkipHealthVerify                           # 可选：跳过健康复验（仅用于清理/演练编排）
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Docker
Assert-EnvFile

if (-not (Test-Path $BackupFile)) {
    Write-Fail "备份文件不存在：$BackupFile"
    exit 1
}
New-Item -ItemType Directory -Force -Path $script:BackupsDir  | Out-Null
New-Item -ItemType Directory -Force -Path $script:RecordsDir | Out-Null

$dbUser = Get-EnvValue 'POSTGRES_USER'; if ([string]::IsNullOrEmpty($dbUser)) { $dbUser = 'migrator' }
$dbName = Get-EnvValue 'POSTGRES_DB';   if ([string]::IsNullOrEmpty($dbName))   { $dbName = 'langgraph' }
$ts = Get-Date -Format 'yyyyMMddHHmmss'
$preRoll = Join-Path $script:BackupsDir "pre-rollback-$ts.dump"

Write-Step "0. 回滚前快照当前状态（可逆性）"
$r = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','pg_dump','-U',$dbUser,'-Fc',$dbName) -StdOutFile $preRoll
Write-Stderr $r
if ($r.ExitCode -ne 0 -or -not (Test-Path $preRoll)) {
    Write-Fail "回滚前快照失败"
    exit 1
}
Write-Ok ("已生成回滚前快照：{0}" -f $preRoll)

Write-Step "1. 恢复数据库（从 $BackupFile）"
# 用 docker cp + 文件路径恢复（规避 Windows stdin 二进制转换）；--clean --if-exists 可重复恢复。
$restore = Restore-DumpToDatabase -BackupFile $BackupFile -DBUser $dbUser -TargetDb $dbName -ExtraArgs @('--clean','--if-exists')
$r = $restore
Write-Stderr $r
$restoreOk = ($r.ExitCode -eq 0)
if ($restoreOk) { Write-Ok "数据库恢复完成" } else { Write-Fail ("数据库恢复失败：{0}" -f $r.Stderr) }

$appRebuilt = $false
if ($GitRef) {
    Write-Step "2. 重建应用到 git 引用 $GitRef"
    $d = Invoke-ComposeProcess -ComposeArgs @('down'); Write-Stderr $d
    & git -C $script:RepoRoot checkout "$GitRef" -- . 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Warn "无法检出 $GitRef（请手工切换并重建）；继续执行 build/up。" }
    $b = Invoke-ComposeProcess -ComposeArgs @('build','--pull'); Write-Stderr $b
    $u = Invoke-ComposeProcess -ComposeArgs @('up','-d');       Write-Stderr $u
    $appRebuilt = $true
    Write-Ok "应用已重建到 $GitRef"
} else {
    Write-Warn "未提供 git-ref，跳过应用重建（仅恢复数据）。如需版本回退，请补充 -GitRef。"
}

$healthOk = $null
if (-not $SkipHealthVerify) {
    Write-Step "3. 健康复验"
    $healthOk = (Wait-Healthz '/api/healthz' -MaxAttempts 30 -IntervalSec 2)
    if ($healthOk) { Write-Ok "健康复验通过（/api/healthz 200）" } else { Write-Fail "健康复验失败（/api/healthz 非 200）" }
}

$data = [ordered]@{
    scenario            = 'preview_rollback'
    backup_file         = (Split-Path -Leaf $BackupFile)
    pre_rollback_snapshot = (Split-Path -Leaf $preRoll)
    git_ref             = if ($GitRef) { $GitRef } else { $null }
    app_rebuilt         = $appRebuilt
    db_restore_ok       = $restoreOk
    health_after        = $healthOk
    db_name             = $dbName
    timestamp           = (Get-Date -Format 'o')
}
$jsonPath = Write-JsonRecord -FileName 'rollback-record.json' -Data $data

$gitRefLabel = if ([string]::IsNullOrEmpty($GitRef)) { '<未回退应用>' } else { $GitRef }
$mdPath = Join-Path $script:RecordsDir "DR-$ts-rollback.md"
@(
    "# 预发布演练记录 — 回滚（DR-$ts）",
    "",
    "- 演练编号：DR-$ts",
    "- 环境：preview（after-sales-preview）",
    "- 执行人 / 复核人：______ / ______",
    "- 恢复源：$(Split-Path -Leaf $BackupFile)",
    "- 回滚前快照：$(Split-Path -Leaf $preRoll)",
    "- git 引用：$gitRefLabel",
    "",
    "| 步骤 | 动作 | 结果 |",
    "|------|------|------|",
    "| 0 | 回滚前快照 | $([bool](Test-Path $preRoll)) |",
    "| 1 | 数据库恢复 | $restoreOk |",
    "| 2 | 应用重建 | $appRebuilt |",
    "| 3 | 健康复验 | $($healthOk) |",
    "",
    "## 结论",
    "- 判定：$($restoreOk -and ($healthOk -ne $false))",
    "- 签名：______"
) | Set-Content -Encoding UTF8 -Path $mdPath

Write-Host "记录: $jsonPath"
Write-Host "记录: $mdPath"
if ($restoreOk -and ($healthOk -ne $false)) {
    Write-Ok "回滚完成并复验通过"
    exit 0
} else {
    Write-Fail "回滚未通过复验（db_restore_ok=$restoreOk, health_after=$healthOk）"
    exit 1
}
