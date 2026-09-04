# run_api_drills.ps1 —— D3/D4/D5/D6 preview API 驱动演练（PowerShell 封装，转发到 run_api_drills.py）。
#
# 前置：需要 PREVIEW_TOKEN（上线白名单租户 admin/approver 的有效 JWT）+ 可达 LLM 端点 或
#       EXECUTION_PROVIDER=mock 驱动 chat 流。若缺 PREVIEW_TOKEN，本脚本直接判定为
#       "前置未满足"（退出码 3），**不伪造任何 PAS**。
#
# 用法（仓库根 + 已启动 after-sales-preview 栈）：
#   $env:PREVIEW_TOKEN='<jwt>'
#   pwsh deploy/drills/pwsh-runbook/run_api_drills.ps1 approval|sse|concurrent|reconcile
#
# 退出码透传 python：0=PASS；1=FAIL；2=BLOCKED(前置未满足)；3=参数/前置缺失。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('approval', 'sse', 'concurrent', 'reconcile')]
    [string]$Scenario
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $env:PREVIEW_TOKEN) {
    Write-Warn "前置未满足：缺少 PREVIEW_TOKEN（上线白名单租户 admin/approver 的有效 JWT）。"
    Write-Warn "另需可达 LLM 端点或 EXECUTION_PROVIDER=mock 驱动 chat 流，否则无法产生预期事件。"
    Write-Warn "已标注前置并跳过实跑（未伪造结果）。"
    exit 3
}

$py = Join-Path $PSScriptRoot 'run_api_drills.py'
& python $py $Scenario
exit $LASTEXITCODE
