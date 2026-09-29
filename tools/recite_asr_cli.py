#!/usr/bin/env python3
"""Parakeet 转写的 CLI 外壳 —— 只在**子进程**里跑，不 import 进 jobscan 的 web 进程。

    python3 tools/recite_asr_cli.py <raw.pcm> [<raw2.pcm> ...]   # int16 / 16k / mono
    → stdout 一行 JSON: {"ok": true, "text": "...", "engine": "parakeet"}

为什么隔离：sdh/asr_parakeet.py 自己会拉起 .venv-parakeet 里的常驻模型子进程（NeMo +
torch，冷启动 ~60s、常驻几个 GB）。把它 import 进 uvicorn 进程等于把 jobscan 的 web 服务
和一个重模型的生命周期绑在一起，模型崩了 web 跟着崩。这里每次转写起一个短命进程，退出即
回收 —— 慢一点，但录音永远不会因为 ASR 而丢。

只依赖标准库，所以 jobscan 的 .venv 和系统 python3 都能跑。
"""
import json
import os
import sys

SDH = os.environ.get("SDH_DIR", "/path/to/interview/sdh")


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(json.dumps({"ok": False, "error": "no input"}))
        return 2
    sys.path.insert(0, SDH)
    try:
        import asr_parakeet as ap
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"import failed: {e}"}))
        return 3
    if not ap.available():
        print(json.dumps({"ok": False, "error": "parakeet not installed"}))
        return 4
    ok, err = ap.start(timeout=float(os.environ.get("PARAKEET_TIMEOUT", "240")))
    if not ok:
        print(json.dumps({"ok": False, "error": f"start failed: {err}"}))
        return 5
    parts = []
    try:
        for p in paths:
            with open(p, "rb") as f:
                pcm = f.read()
            t = ap.transcribe(pcm)
            if t is None:
                print(json.dumps({"ok": False, "error": ap.last_error() or "transcribe failed"}))
                return 6
            parts.append(t.strip())
    finally:
        ap.stop()
    print(json.dumps({"ok": True, "text": " ".join(x for x in parts if x),
                      "engine": "parakeet"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
