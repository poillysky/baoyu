#!/usr/bin/env python3
"""原生录制探针 - fork隔离执行器。原生SDK崩溃只死子进程, 父进程/后端不受影响。"""
import os, sys, json, subprocess

# SDK 4.10 环境（裸脚本验证通过的环境）；旧 .venv-native312 是 2.0.8，API 不同，不可用
PYBIN = os.environ.get("NATIVE_PYBIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".venv-native2410", "bin", "python")
HERE = os.path.dirname(os.path.abspath(__file__))

def run_probe(script, args, timeout=60):
    """在独立子进程跑原生探针, 返回 (returncode, stdout, stderr)。崩了只影响子进程。"""
    try:
        p = subprocess.run([PYBIN, os.path.join(HERE, script)] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=timeout, cwd=HERE)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return -9, (e.stdout or ""), "TIMEOUT"

if __name__ == "__main__":
    script = sys.argv[1] if len(sys.argv) > 1 else "_native_probe4.py"
    args = sys.argv[2:]
    rc, out, err = run_probe(script, args)
    print("=== CHILD rc=%s ===" % rc)
    # 只打印关键行, 过滤噪声
    for ln in out.splitlines():
        if any(k in ln for k in ("RESULT", "CONNECTED", "USER_JOINED", "ENC ", "RAW ", "track", "subscribe", "DONE")):
            print(ln)
    if rc not in (0,):
        print("[[child crashed or non-zero rc=%s, isolated - backend safe]]" % rc)
