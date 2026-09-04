# common.ps1 —— preview 演练 runbook PowerShell 公共助手（D1/D2/回滚/API 驱动共用）。
#
# 说明：
#   - 本文件由 run_backup_restore.ps1 / run_restart_services.ps1 / run_rollback.ps1 /
#     run_api_drills.ps1 / run_all.ps1 通过 `. (Join-Path $PSScriptRoot 'common.ps1')` 引入。
#   - 建议用 PowerShell 7+（pwsh）。若用 Windows PowerShell 5.1（无 -SkipCertificateCheck /
#     对 UTF-8 读码有差异），请显式设置 $env:DOCKER、$env:PREVIEW_BASE、$env:PREVIEW_TOKEN，
#     并确保经 pwsh 或 py 驱动；下文 Test-Healthz 已内置 curl.exe 兜底。
#   - 复用姿势与 deploy/scripts/common.sh 一致：
#       docker compose --env-file <ENV_FILE> -f <COMPOSE_FILE> <args...>
#   - 安全：绝不把 PostgreSQL/Redis 口令等密钥打印到 stdout；口令仅由 --env-file 注入，
#     不在命令行出现。仅解析非敏感的 POSTGRES_USER / POSTGRES_DB。

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- 路径解析（基于本文件位置 deploy/drills/pwsh-runbook/） ----
$script:PwshRunbookDir = $PSScriptRoot
$script:DrillsDir      = Split-Path -Parent $script:PwshRunbookDir   # deploy/drills
$script:DeployDir      = Split-Path -Parent $script:DrillsDir       # deploy
$script:RepoRoot       = Split-Path -Parent $script:DeployDir       # 仓库根
$script:BackupsDir     = Join-Path $script:DeployDir 'backups'
$script:RecordsDir     = Join-Path $script:DrillsDir 'records'

# ---- 可覆盖路径/环境 ----
$script:DockerCli = if ($env:DOCKER) { $env:DOCKER } else {
    'C:\Users\孔德草\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
}
$script:ComposeFile = if ($env:COMPOSE_FILE) { $env:COMPOSE_FILE } else {
    Join-Path $script:RepoRoot 'docker-compose.preview.yml'
}
$script:EnvFile = if ($env:ENV_FILE) { $env:ENV_FILE } else {
    Join-Path $script:DeployDir '.env.preview'
}
$script:PreviewBase = if ($env:PREVIEW_BASE) { $env:PREVIEW_BASE } else { 'https://127.0.0.1' }

# ---- 日志 ----
function Write-Step([string]$s) {
    Write-Host ("`n[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $s) -ForegroundColor Cyan
}
function Write-Ok([string]$s)   { Write-Host ("  [OK]   {0}" -f $s) -ForegroundColor Green }
function Write-Fail([string]$s) { Write-Host ("  [FAIL] {0}" -f $s) -ForegroundColor Red }
function Write-Warn([string]$s) { Write-Host ("  [WARN] {0}" -f $s) -ForegroundColor Yellow }
function Write-Stderr($r) {
    if ($r -and $r.Stderr -and ([string]::IsNullOrWhiteSpace($r.Stderr) -eq $false)) {
        Write-Host ("    (stderr) {0}" -f $r.Stderr.Trim()) -ForegroundColor DarkGray
    }
}

# ---- 前置断言 ----
function Assert-Docker {
    $p = $script:DockerCli
    $found = if ([System.IO.Path]::IsPathRooted($p) -or $p.Contains('\') -or $p.Contains('/')) {
        Test-Path $p
    } else {
        $null -ne (Get-Command $p -ErrorAction SilentlyContinue)
    }
    if (-not $found) {
        Write-Fail ("未找到 Docker CLI：{0}。请安装并启动 Docker 引擎，或用 `$env:DOCKER` 覆盖为完整路径。" -f $p)
        throw "Docker CLI not found: $p"
    }
}

function Assert-EnvFile {
    if (-not (Test-Path $script:EnvFile)) {
        Write-Fail ("找不到密钥文件 {0}。请先完成 t1 生成 deploy/.env.preview，再执行本演练。" -f $script:EnvFile)
        throw "Env file missing: $($script:EnvFile)"
    }
}

# ---- 从 .env 读取非敏感键值（兼容 KEY=VALUE / 带引号/注释；找不到返回空串） ----
function Get-EnvValue {
    param([Parameter(Mandatory = $true)][string]$Key)
    if (-not (Test-Path $script:EnvFile)) { return '' }
    $prefix = "$Key="
    foreach ($line in Get-Content $script:EnvFile) {
        $t = $line.Trim()
        if ($t.StartsWith($prefix)) {
            $v = $t.Substring($prefix.Length).Trim()
            $v = $v.Trim('"').Trim("'")
            return $v
        }
    }
    return ''
}

# ---- 命令行参数引用（Windows CommandLineToArgvW 语义） ----
function Quote-CliArg([string]$s) {
    if ([string]::IsNullOrEmpty($s)) { return '""' }
    if ($s -notmatch '[\s"]') { return $s }
    return ('"' + ($s -replace '"', '\"') + '"')
}

# ---- 运行 docker compose（二进制安全：可把 stdout 落到文件、把文件喂给 stdin） ----
function Invoke-ComposeProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$ComposeArgs,
        [string]$StdInFile,    # 若设置：把该文件字节写入命令行 stdin（如 pg_restore）
        [string]$StdOutFile,   # 若设置：把 stdout 原样写入文件（二进制安全，如 pg_dump -Fc）
        [string]$WorkDir = $script:RepoRoot
    )

    $fullArgs = @('compose', '--env-file', $script:EnvFile, '-f', $script:ComposeFile) + $ComposeArgs
    $argLine  = ($fullArgs | ForEach-Object { Quote-CliArg $_ }) -join ' '

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName         = $script:DockerCli
    $psi.Arguments        = $argLine
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute  = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.RedirectStandardInput  = $true   # 关闭即等于 /dev/null；防交互挂起

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()

    if ($StdInFile) {
        $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $StdInFile))
        $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $p.StandardInput.Flush()
    }
    $p.StandardInput.Close()

    if ($StdOutFile) {
        $fs = [System.IO.File]::Create($StdOutFile)
        $outTask = $p.StandardOutput.BaseStream.CopyToAsync($fs)
    } else {
        $outTask = $p.StandardOutput.ReadToEndAsync()
    }
    $errTask = $p.StandardError.ReadToEndAsync()

    $p.WaitForExit()
    $stderrText = $errTask.GetAwaiter().GetResult()

    if ($StdOutFile) {
        [void]$outTask.GetAwaiter().GetResult()
        [void]$fs.Flush()
        [void]$fs.Close()
        $stdoutText = $null
    } else {
        $stdoutText = $outTask.GetAwaiter().GetResult()
    }

    return [pscustomobject]@{ ExitCode = $p.ExitCode; Stdout = $stdoutText; Stderr = $stderrText }
}

# ---- 备份恢复：把 dump 拷入容器后按文件路径 pg_restore ----
# 规避 Windows 下把二进制经 `docker compose exec` 的 stdin 传输时发生 CRLF/编码转换，破坏 pg_dump 自定义归档。
function Restore-DumpToDatabase {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BackupFile,
        [Parameter(Mandatory = $true)][string]$DBUser,
        [Parameter(Mandatory = $true)][string]$TargetDb,
        [string[]]$ExtraArgs = @()
    )
    $remote = "/tmp/drill-restore-{0}.dump" -f ([guid]::NewGuid().ToString('N'))
    $p = Invoke-ComposeProcess -ComposeArgs @('ps', '-q', 'postgres')
    $cid = (($p.Stdout -split "\r?\n") | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
    if ([string]::IsNullOrWhiteSpace($cid)) {
        Write-Fail "找不到 postgres 容器，无法恢复。"
        return [pscustomobject]@{ ExitCode = 1; Stderr = 'no postgres container'; Stdout = $null; Elapsed = 0.0 }
    }
    $copy = & $script:DockerCli cp $BackupFile ("{0}:{1}" -f $cid, $remote) 2>&1
    if ($LASTEXITCODE -ne 0) {
        $msg = ($copy -join ' ')
        Write-Fail ("复制备份到容器失败：{0}" -f $msg)
        return [pscustomobject]@{ ExitCode = 1; Stderr = $msg; Stdout = $null; Elapsed = 0.0 }
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $restoreArgs = @('exec', '-T', 'postgres', 'pg_restore', '-U', $DBUser, '-d', $TargetDb) + $ExtraArgs + @($remote)
    $r = Invoke-ComposeProcess -ComposeArgs $restoreArgs
    $sw.Stop()
    Invoke-ComposeProcess -ComposeArgs @('exec', '-T', 'postgres', 'rm', '-f', $remote) | Out-Null
    return [pscustomobject]@{ ExitCode = $r.ExitCode; Stderr = $r.Stderr; Stdout = $r.Stdout; Elapsed = $sw.Elapsed.TotalSeconds }
}

# ---- HTTPS 同源健康探测：优先 Invoke-WebRequest -SkipCertificateCheck，再 curl.exe 兜底 ----
function Test-Healthz {
    param([string]$Path = '/api/healthz')
    $url = "$($script:PreviewBase)$Path"

    $iwr = Get-Command Invoke-WebRequest -ErrorAction SilentlyContinue
    if ($iwr -and $iwr.Parameters.ContainsKey('SkipCertificateCheck')) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -SkipCertificateCheck -TimeoutSec 8
            return ($r.StatusCode -eq 200)
        } catch {
            # 证书/网络/HTTP 非 2xx：转为回退，交由 curl.exe 重测，避免误判。
        }
    }
    try {
        $code = & curl.exe -kso NUL -w '%{http_code}' --max-time 8 $url
        return ($code -eq '200')
    } catch {
        return $false
    }
}

function Wait-Healthz {
    param([string]$Path = '/api/healthz', [int]$MaxAttempts = 30, [int]$IntervalSec = 2)
    for ($i = 0; $i -lt $MaxAttempts; $i++) {
        if (Test-Healthz $Path) { return $true }
        Start-Sleep -Seconds $IntervalSec
    }
    return (Test-Healthz $Path)
}

# ---- 记录写入 ----
function Write-JsonRecord {
    param([string]$FileName, [object]$Data)
    New-Item -ItemType Directory -Force -Path $script:RecordsDir | Out-Null
    $path = Join-Path $script:RecordsDir $FileName
    $Data | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -Path $path
    return $path
}
