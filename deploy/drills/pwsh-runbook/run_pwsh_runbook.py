#!/usr/bin/env python3
# run_pwsh_runbook.py —— 可读的 python 封装，统一驱动 pwsh-runbook 下的各演练脚本。
#
# 它本身不做演练逻辑，只是把「在哪个脚本、传什么参数、看退出码」封装成简单命令，方便团队成员
# 用一个入口驱动全部 PowerShell 演练（D1/D2/回滚/API 驱动）。全部逻辑复用对应 .ps1 / .py。
#
# 用法（仓库根 + 已启动 after-sales-preview 栈）：
#   python deploy/drills/pwsh-runbook/run_pwsh_runbook.py d1
#   python deploy/drills/pwsh-runbook/run_pwsh_runbook.py d2 [--rpo-seconds 900]
#   python deploy/drills/pwsh-runbook/run_pwsh_runbook.py rollback --backup deploy/backups/<f>.dump [--git-ref <ref>]
#   python deploy/drills/pwsh-runbook/run_pwsh_runbook.py api approval|sse|concurrent|reconcile
#   python deploy/drills/pwsh-runbook/run_pwsh_runbook.py all
#   、如需传其它环境：先 export PREVIEW_TOKEN / PREVIEW_BASE / DOCKER / ENV_FILE / COMPOSE_FILE。
#
# 退出码：透传被驱动的脚本；0=该步通过；1=断言失败；2=BLOCKED（前置未满足，如 api 缺 token/事件）；
# 3=参数错误。绝不伪造 PASS。

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def _pwsh():
    return shutil.which("pwsh") or shutil.which("powershell")


def run_ps1(name, args):
    pwsh = _pwsh()
    if not pwsh:
        print("[BLOCKED] 未找到 pwsh/powershell，无法驱动 PowerShell 演练脚本。")
        return 2
    cmd = [pwsh, "-NoProfile", "-File", os.path.join(HERE, name)] + args
    print("[run] " + " ".join(cmd))
    return subprocess.call(cmd)


def run_py(name, args):
    py = sys.executable
    cmd = [py, os.path.join(HERE, name)] + args
    print("[run] " + " ".join(cmd))
    return subprocess.call(cmd)


def usage():
    print(__doc__)
    return 3


def main():
    argv = sys.argv[1:]
    if not argv:
        return usage()

    cmd = argv[0]

    if cmd == "d1":
        return run_ps1("run_restart_services.ps1", [])

    if cmd == "d2":
        # 可选 --rpo-seconds <N>
        extra = []
        if "--rpo-seconds" in argv:
            idx = argv.index("--rpo-seconds")
            if idx + 1 < len(argv):
                extra = ["-RpoSeconds", argv[idx + 1]]
        return run_ps1("run_backup_restore.ps1", extra)

    if cmd == "rollback":
        extra = []
        if "--backup" in argv:
            idx = argv.index("--backup")
            if idx + 1 < len(argv):
                extra += ["-BackupFile", argv[idx + 1]]
        if "--git-ref" in argv:
            idx = argv.index("--git-ref")
            if idx + 1 < len(argv):
                extra += ["-GitRef", argv[idx + 1]]
        if "-BackupFile" not in extra:
            print("[参数错误] rollback 需要 --backup <dump 文件>。")
            return 3
        return run_ps1("run_rollback.ps1", extra)

    if cmd == "api":
        if len(argv) < 2 or argv[1] not in ("approval", "sse", "concurrent", "reconcile"):
            print("[参数错误] api 后需跟 approval|sse|concurrent|reconcile。")
            return 3
        return run_py("run_api_drills.py", [argv[1]])

    if cmd == "all":
        return run_ps1("run_all.ps1", [])

    return usage()


if __name__ == "__main__":
    sys.exit(main())
