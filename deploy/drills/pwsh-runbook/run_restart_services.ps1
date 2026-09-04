# run_restart_services.ps1 —— [D1] API / worker / Redis 重启韧性演练（PowerShell 实现，免 bash）。
#
# 逐一重启 api / worker / redis，并校验：
#   - API 重启后 /api/healthz 恢复 200；
#   - worker 重启后仍在运行（compose ps --status running > 0）；
#   - Redis 重启后 PostgreSQL 仍就绪（数据面在 PG，Redis 仅承载锁/队列/缓存，非可信数据源）。
# 结果写 deploy/drills/records/drill-restart-services.json（+ 一份 Markdown 记录）。
#
# 用法（仓库根 + 已启动 after-sales-preview 栈）：
#   pwsh deploy/drills/pwsh-runbook/run_restart_services.ps1
#
# 说明：HTTPS 健康探测用 Invoke-WebRequest -SkipCertificateCheck（PowerShell 7+），
#       失败时以 curl.exe -k 兜底。不打印任何口令。

[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Docker
Assert-EnvFile
New-Item -ItemType Directory -Force -Path $script:RecordsDir | Out-Null

$pass = 0; $fail = 0
$apiHealth = $false; $workerRunning = $false; $redisPgReady = $false

Write-Step "D1: API 重启"
$r = Invoke-ComposeProcess -ComposeArgs @('restart', 'api')
Write-Stderr $r
if (Wait-Healthz '/api/healthz' -MaxAttempts 30 -IntervalSec 2) {
    $apiHealth = $true; $pass++
    Write-Ok "api 重启后 /api/healthz 200"
} else {
    $fail++
    Write-Fail "api 重启后未恢复（/api/healthz 非 200）"
}

Write-Step "D1: worker 重启"
$r = Invoke-ComposeProcess -ComposeArgs @('restart', 'worker')
Write-Stderr $r
Start-Sleep -Seconds 5
$w = Invoke-ComposeProcess -ComposeArgs @('ps', '-q', '--status', 'running', 'worker')
if ([string]::IsNullOrWhiteSpace($w.Stdout) -eq $false) {
    $workerRunning = $true; $pass++
    Write-Ok "worker 重启后运行中"
} else {
    $fail++
    Write-Fail "worker 未运行"
}

Write-Step "D1: Redis 重启（数据面在 PostgreSQL，Redis 非可信存储）"
$r = Invoke-ComposeProcess -ComposeArgs @('restart', 'redis')
Write-Stderr $r
Start-Sleep -Seconds 3
$pgUser = Get-EnvValue 'POSTGRES_USER'; if ([string]::IsNullOrEmpty($pgUser)) { $pgUser = 'migrator' }
$pgDb   = Get-EnvValue 'POSTGRES_DB';   if ([string]::IsNullOrEmpty($pgDb))   { $pgDb   = 'langgraph' }
$pg = Invoke-ComposeProcess -ComposeArgs @('exec','-T','postgres','pg_isready','-U',$pgUser,'-d',$pgDb,'-h','localhost')
Write-Stderr $pg
if ($pg.ExitCode -eq 0) {
    $redisPgReady = $true; $pass++
    Write-Ok "Redis 重启后 PostgreSQL 仍就绪（数据面未受影响）"
} else {
    $fail++
    Write-Fail "Redis 重启后数据面异常（pg_isready 非 0）"
}

$data = [ordered]@{
    scenario             = 'D1_restart_services'
    pass                 = $pass
    fail                 = $fail
    api_healthz_200      = $apiHealth
    worker_running       = $workerRunning
    redis_restart_pg_ready = $redisPgReady
    preview_base         = $script:PreviewBase
    timestamp            = (Get-Date -Format 'o')
}
$jsonPath = Write-JsonRecord -FileName 'drill-restart-services.json' -Data $data

$ts = Get-Date -Format 'yyyyMMdd-HHmm'
$mdPath = Join-Path $script:RecordsDir "DR-$ts-api-worker-redis-restart.md"
@(
    "# 预发布演练记录 — API / worker / Redis 重启（DR-$ts）",
    "",
    "- 演练编号：DR-$ts",
    "- 环境：preview（after-sales-preview）",
    "- 应用 git 引用：<实跑时填>",
    "- 执行人 / 复核人：______ / ______",
    "",
    "| # | 演练项 | 证据ID | 结果 | 备注 |",
    "|---|--------|--------|------|------|",
    "| D1 | API 重启 | api_healthz_200=$apiHealth | $($apiHealth) | 重启后 /api/healthz 200 |",
    "| D1 | worker 重启 | worker_running=$workerRunning | $($workerRunning) | 重启后仍在运行 |",
    "| D1 | Redis 重启 | redis_restart_pg_ready=$redisPgReady | $($redisPgReady) | PG 数据面仍就绪 |",
    "",
    "## 结论",
    "- 是否达到预发布验收：$($fail -eq 0)",
    "- 签名：______"
) | Set-Content -Encoding UTF8 -Path $mdPath

Write-Host "记录: $jsonPath"
Write-Host "记录: $mdPath"
if ($fail -eq 0) {
    Write-Ok ("D1 全部通过（PASS={0}）" -f $pass)
    exit 0
} else {
    Write-Fail ("D1 有失败（fail={0}）" -f $fail)
    exit 1
}
