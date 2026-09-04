#!/usr/bin/env python3
"""生成登录凭据的 PHC 哈希表（不再使用无盐 SHA-256）。

用途（在部署/密钥管理时生成 `AUTH_LOGIN_CREDENTIALS` 的 JSON）：
    python scripts/hash_login_credentials.py --algorithm argon2id < input.json

产出形如：
    {"TENANT-A:USER-001": "$argon2id$v=19$m=65536,t=3,p=4$..."}

安全红线：
- **明文仅作为一时输入**（stdin / --creds-env / --creds-file / getpass 交互），绝不进入产物。
- 命令行**不回显、不落日志**任何口令；交互式输入走 `getpass`（无回显）。
- 产物（stdout 或 --output 文件）只含 `<tenant>:<user>` → PHC 哈希，绝不含明文。
- 默认 argon2id；如需 bcrypt 请用 `--algorithm bcrypt`（注意 bcrypt 只取前 72 字节）。

把产出的 JSON 注入环境变量 `AUTH_LOGIN_CREDENTIALS`（服务端 `src/config.py` 的
`auth_login_credentials`），并把算法名写入 `AUTH_CREDENTIAL_HASH`（默认 argon2id）。

输入格式（JSON 优先，其次行格式）：
- JSON 对象：`{"<tenant_id>:<user_id>": "<plaintext>", ...}`
- 行格式  ：每行 `<tenant_id>:<user_id>=<plaintext>`（# 开头为注释，忽略空行）

选项：
    --algorithm {argon2id|bcrypt}   哈希算法（默认 argon2id）
    --output PATH                   把结果 JSON 写入 PATH（默认输出到 stdout）
    --creds-env NAME                从环境变量 NAME 读取明文 JSON（避免走 stdin/文件）
    --creds-file PATH               从文件读取明文（JSON 或行格式；临时明文文件用后请删除）
    --user <tenant>:<user>          交互式用 getpass 输入单条明文口令（无回显）
"""

import argparse
import getpass
import json
import os
import re
import sys

# 仅允许受支持的算法；完全自托管依赖 argon2-cffi / bcrypt（本地 PyPI）。
_ALGORITHMS = ("argon2id", "bcrypt")
# bcrypt 仅处理前 72 字节；超出部分会被静默截断，故超过 72 字节时告警。
_BCRYPT_MAX_BYTES = 72
_KEY_RE = re.compile(r"^[^:\s=]+:[^=\s]+$")  # 形如 tenant:user


def _hash_one(password: str, algo: str) -> str:
    if algo == "argon2id":
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)
    if algo == "bcrypt":
        import bcrypt

        raw = password.encode("utf-8")
        if len(raw) > _BCRYPT_MAX_BYTES:
            print("[警告] 口令超过 72 字节，bcrypt 仅校验前 72 字节。", file=sys.stderr)
        return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")
    raise ValueError("未知算法：%s" % algo)


def _parse_input(text: str) -> dict[str, str]:
    """把明文输入解析为 `{tenant:user: plaintext}`。JSON 优先，否则按行格式。"""
    text = text.strip()
    if not text:
        return {}
    # JSON 对象优先。
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except Exception as exc:
            raise SystemExit("[参数错误] 无法解析 JSON 输入：%s" % exc)
        if not isinstance(obj, dict):
            raise SystemExit("[参数错误] JSON 输入必须是对象 `{\"<tenant>:<user>\": \"<明文>\"}`。")
        out = {}
        for k, v in obj.items():
            key = str(k).strip()
            if not _KEY_RE.fullmatch(key):
                raise SystemExit("[参数错误] 非法键：%r（应为 <tenant>:<user>）" % key)
            out[key] = str(v)
        return out
    # 行格式 `tenant:user=password`。
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if not _KEY_RE.fullmatch(key):
            raise SystemExit("[参数错误] 非法行键：%r（应为 <tenant>:<user>）" % key)
        out[key] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成登录凭据 PHC 哈希表（Argon2id/bcrypt）")
    parser.add_argument("--algorithm", choices=_ALGORITHMS, default="argon2id",
                        help="哈希算法（默认 argon2id）")
    parser.add_argument("--output", default="", help="把结果 JSON 写入该文件（默认 stdout）")
    parser.add_argument("--creds-env", default="", help="从环境变量读取明文 JSON")
    parser.add_argument("--creds-file", default="", help="从文件读取明文（JSON 或行格式）")
    parser.add_argument("--user", default="", help="交互式用 getpass 输入单条明文口令")
    args = parser.parse_args(argv)

    pairs: dict[str, str] = {}

    if args.creds_env:
        env_val = os.environ.get(args.creds_env, "")
        if not env_val:
            print("[参数错误] 环境变量 %s 为空。" % args.creds_env, file=sys.stderr)
            return 2
        pairs = _parse_input(env_val)
    elif args.creds_file:
        if not os.path.exists(args.creds_file):
            print("[参数错误] 找不到文件：%s" % args.creds_file, file=sys.stderr)
            return 2
        with open(args.creds_file, encoding="utf-8") as f:
            pairs = _parse_input(f.read())
    elif args.user:
        if not _KEY_RE.fullmatch(args.user):
            print("[参数错误] --user 应为 <tenant>:<user>。", file=sys.stderr)
            return 2
        # getpass：无回显，不落日志。
        pw = getpass.getpass("请输入 %s 的明文口令：" % args.user)
        pairs = {args.user: pw}
    else:
        # 从 stdin 读取。若 stdin 是终端则提示；否则直接读。
        if sys.stdin.isatty():
            print("[提示] 从 stdin 输入明文（JSON 或每行 tenant:user=password），"
                  "Ctrl+D 结束。", file=sys.stderr)
        pairs = _parse_input(sys.stdin.read())

    if not pairs:
        print("[参数错误] 未提供任何凭据输入。", file=sys.stderr)
        return 2

    result: dict[str, str] = {}
    for key in pairs:
        result[key] = _hash_one(pairs[key], args.algorithm)

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print("已写入 %d 条 PHC 哈希：%s" % (len(result), args.output), file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
