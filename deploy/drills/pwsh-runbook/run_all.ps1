# run_all.ps1 —— preview 演练编排器（PowerShell 实现）。运行 D1/D2，并在 PREVIEW_TOKEN 就绪时运行 D3-D6。
#
# 说明：
#   - 本脚本用 独立 pwsh 子进程 运行每个子演练（pwsh -File），因此子脚本里的 `exit N`
#     只影响该子进程，不会中断本编排器；通过 $LASTEXITCODE 汇总结论。
#   - 回滚（run_rollback.ps1）是故障驱动的独立运行手册步骤，**不**纳入每日例行编排，避免误伤。
#   - 各子演练失败即计入 FAIL（fail-closed 闸门）；全部 PASS 才返回 0。
#   - API 演练（D3-D6）依赖 PREVIEW_TOKEN + 可达 LLM/mock；缺 token 时如实标注 BLOCKED，不伪造结果。

[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

New-Item -ItemType Directory -Force -Path $script:RecordsDir | Out-Null
$ts = Get-Date -Format 'yyyyMMddHHmmss'

function Invoke-RunbookStep {
    param([string]$Script, [string[]]$ScriptArgs = @())
    $file = Join-Path $PSScriptRoot $Script
    Write-Step ("运行：{0} {1}" -f (Split-Path -Leaf $file), ($ScriptArgs -join ' '))
    & pwsh -NoProfile -File $file @ScriptArgs
    return $LASTEXITCODE
}

$results = [ordered]@{}

# D1
$code1 = Invoke-RunbookStep 'run_restart_services.ps1'
$results['D1_restart_services'] = $code1
# D2
$code2 = Invoke-RunbookStep 'run_backup_restore.ps1'
$results['D2_pg_backup_restore'] = $code2

# D3-D6：需要 PREVIEW_TOKEN，否则标注前置未满足
$apiScenarios = @('approval', 'sse', 'concurrent', 'reconcile')
if ($env:PREVIEW_TOKEN) {
    foreach ($s in $apiScenarios) {
        $c = Invoke-RunbookStep 'run_api_drills.ps1' @($s)
        $results["D_api_$s"] = $c
    }
} else {
    foreach ($s in $apiScenarios) {
        Write-Warn ("跳演 D_api_{0}：缺少 PREVIEW_TOKEN（前置未满足）。" -f $s)
        $results["D_api_$s"] = 2   # BLOCKED
    }
}

$pass = 0; $fail = 0; $blocked = 0
foreach ($k in $results.Keys) {
    $c = [int]$results[$k]
    if ($c -eq 0) { $pass++ } elseif ($c -eq 2) { $blocked++ } elseif ($c -eq 3) { $blocked++ } else { $fail++ }
}

$summary = [ordered]@{
    scenario  = 'preview_drills'
    timestamp = $ts
    passed    = $pass
    failed    = $fail
    blocked   = $blocked
    steps     = $results
}
$jsonPath = Write-JsonRecord -FileName "preview-drills-$ts.json" -Data $summary

$mdPath = Join-Path $script:RecordsDir "preview-drills-$ts.md"
$mdLines = New-Object System.Collections.Generic.List[string]
[void]$mdLines.Add("# 预发布演练记录 $ts")
[void]$mdLines.Add("")
[void]$mdLines.Add("- 应用 git 引用：<实跑时填>")
[void]$mdLines.Add("- 环境：preview（after-sales-preview）")
[void]$mdLines.Add("")
[void]$mdLines.Add("| # | 演练项 | 结果 |")
[void]$mdLines.Add("|---|--------|------|")
foreach ($k in $results.Keys) {
    $c = [int]$results[$k]
    $label = switch ($c) { 0 { 'PASS' } 2 { 'BLOCKED' } 3 { 'BLOCKED' } default { 'FAIL' } }
    [void]$mdLines.Add("| $k | $($label) |")
}
[void]$mdLines.Add("")
[void]$mdLines.Add("## 汇总")
[void]$mdLines.Add("- PASS=$pass FAIL=$fail BLOCKED=$blocked")
[void]$mdLines.Add("- 说明：D_api_* 的 BLOCKED 表示缺少 PREVIEW_TOKEN 或未得到预期事件（LLM/mock 未就绪），非伪造结果。")
[void]$mdLines.Add("- 回滚演练见 run_rollback.ps1（故障驱动，独立执行）。")
$mdLines | Set-Content -Encoding UTF8 -Path $mdPath

Write-Step ("汇总：PASS={0} FAIL={1} BLOCKED={2}" -f $pass, $fail, $blocked)
Write-Host "记录: $jsonPath"
Write-Host "记录: $mdPath"
if ($fail -eq 0 -and $blocked -eq 0) {
    Write-Ok "全部演练通过"
    exit 0
}
if ($fail -eq 0) {
    Write-Warn ("存在 BLOCKED（{0}），无法判定完全达标；请补齐前置（PREVIEW_TOKEN / LLM 或 mock）后重跑。" -f $blocked)
    exit 2
}
Write-Fail "存在失败演练"
exit 1
