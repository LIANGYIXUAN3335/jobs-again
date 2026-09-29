"""念稿练习 · 转写。浏览器录的 webm/mp4 → int16@16k PCM → Parakeet（子进程）→ 文本。

引擎选择的依据是 sdh 在真实面试录音上的实测（201 参考词）：Parakeet TDT-0.6b-v3 WER 17.4% /
漏词 0.0%，faster-whisper small 24.4% / 9.5%，Apple on-device 71.1% / 70.1%。whisper 还有两个
硬伤在真实音频上复现了：hotwords 词表回显、coding 轮复读死循环。所以主路径是 Parakeet。

**转写失败绝不能吃掉一次录音。** 顺序：Parakeet → sdh 的 faster-whisper 兜底 → 空转写 + 一个
`asr_error`，take 照样入库、录音照样在盘上，卡片显示「转写没跑出来，录音已存 [重试评分]」。
丢一次录音的挫败感远大于少一次评分。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import config as cfgmod

SDH_DIR = "/path/to/interview/sdh"
CHUNK_SEC = 45          # >60s 才切；切点交给 ffmpeg，块间重叠 0
MAX_ONE_SHOT = 60


def to_pcm(src: Path, out: Path, start: float | None = None,
           dur: float | None = None) -> bool:
    """任意容器 → int16 / 16k / mono 裸 PCM（Parakeet 只吃这个，没有 word timestamp）。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-ac", "1", "-ar", "16000", "-f", "s16le", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        return r.returncode == 0 and out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def _chunks(duration: float) -> list[tuple[float, float]]:
    if duration <= MAX_ONE_SHOT:
        return [(0.0, duration or 0.0)]
    out, t = [], 0.0
    while t < duration:
        out.append((t, min(CHUNK_SEC, duration - t)))
        t += CHUNK_SEC
    return out


def transcribe(path: Path, duration: float = 0.0) -> dict:
    """→ {"text", "engine", "error"}。任何失败都返回结构完整的 dict，绝不抛。"""
    path = Path(path)
    if not path.exists():
        return {"text": "", "engine": "", "error": "audio missing"}
    tmpdir = Path(tempfile.mkdtemp(prefix="recite-asr-"))
    pcms: list[str] = []
    try:
        for i, (st, du) in enumerate(_chunks(duration)):
            p = tmpdir / f"c{i:02d}.pcm"
            if to_pcm(path, p, start=st if duration > MAX_ONE_SHOT else None,
                      dur=du if duration > MAX_ONE_SHOT else None):
                pcms.append(str(p))
        if not pcms:
            return {"text": "", "engine": "", "error": "ffmpeg decode failed"}
        out = _run_parakeet(pcms)
        if out.get("ok"):
            return {"text": out.get("text", ""), "engine": "parakeet", "error": ""}
        fb = _run_whisper(path)
        if fb.get("ok"):
            return {"text": fb.get("text", ""), "engine": "whisper-fallback",
                    "error": f"parakeet: {out.get('error', '')}"}
        return {"text": "", "engine": "",
                "error": f"parakeet: {out.get('error', '')} / whisper: {fb.get('error', '')}"}
    finally:
        for p in pcms:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass


def _run_parakeet(pcms: list[str]) -> dict:
    cli = cfgmod.ROOT / "tools" / "recite_asr_cli.py"
    env = dict(os.environ, SDH_DIR=SDH_DIR)
    try:
        r = subprocess.run(["python3", str(cli), *pcms], capture_output=True, text=True,
                           timeout=600, env=env)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return {"ok": False, "error": (r.stderr or "no output")[:300]}
    try:
        return json.loads(line[-1])
    except Exception as e:
        return {"ok": False, "error": f"bad cli output: {e}"}


def _run_whisper(path: Path) -> dict:
    """兜底：sdh 的 .venv 里装着 faster-whisper。质量差一档（漏词 9.5%），但有总比没有强。"""
    py = Path(SDH_DIR) / ".venv" / "bin" / "python"
    if not py.exists():
        return {"ok": False, "error": "no sdh venv"}
    code = (
        "import sys,json;from faster_whisper import WhisperModel;"
        "m=WhisperModel('small',device='cpu',compute_type='int8');"
        "segs,_=m.transcribe(sys.argv[1]);"
        "print(json.dumps({'ok':True,'text':' '.join(s.text.strip() for s in segs)}))"
    )
    try:
        r = subprocess.run([str(py), "-c", code, str(path)], capture_output=True,
                           text=True, timeout=900)
        return json.loads((r.stdout or "{}").strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
