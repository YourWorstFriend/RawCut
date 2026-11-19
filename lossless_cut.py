#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lossless_cut.py —— 基于 FFmpeg 的「不改变码率」视频剪辑内核（库 + 命令行）

核心思路：剪辑不等于重新编码。用 -c copy 直接把压缩好的数据包按时间戳搬运，
解码器/编码器完全不参与，所以画质、码率、色彩、HDR 全部 1:1 保留。

两种模式：
  copy      流复制（默认）—— 无损、飞快、码率不变；代价是切点吸附到关键帧。
  accurate  精确到帧 —— 只重编码，输出码率对齐源视频，体积≈原码率×时长。

命令行：
  python lossless_cut.py info  影片.mp4
  python lossless_cut.py cut   影片.mp4 -s 00:10:00 -e 00:12:30
  python lossless_cut.py cut   影片.mp4 -s 00:10:00 -e 00:12:30 --accurate -o 片段.mp4
  python lossless_cut.py keys  影片.mp4
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------- 工具定位

def _find_tool(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    roots = [
        os.path.dirname(os.path.abspath(__file__)),
        r"C:\ffmpeg\bin",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
    ]
    for r in roots:
        cand = os.path.join(r, name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        f"找不到 {name}。请安装 FFmpeg 并把它加入 PATH（Windows 可用: winget install Gyan.FFmpeg）"
    )


def _ffmpeg() -> str:
    return _find_tool("ffmpeg")


def _ffprobe() -> str:
    return _find_tool("ffprobe")


# ---------------------------------------------------------------- 时间字符串

_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(text: str) -> float:
    """'01:23:45.6' / '1:23.5' / '83.5' / '83.5s' -> 秒"""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().rstrip("sS")
    if not s:
        raise ValueError("空的时间值")
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"无法识别的时间格式: {text!r}（可用 83.5 / 1:23.5 / 01:23:45.600）")
    h, mi, sec = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec)


def fmt_time(seconds: float) -> str:
    """秒 -> 'HH:MM:SS.mmm'（FFmpeg 参数用）"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def fmt_clock(seconds: float) -> str:
    """秒 -> 'H:MM:SS.mmm'（给人看的）"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:06.3f}"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} TB"


# ---------------------------------------------------------------- 媒体信息

@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    size: int = 0
    container: str = ""
    fmt_bitrate: int = 0
    vcodec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    vbitrate: int = 0
    acodec: str = ""
    abitrate: int = 0
    has_audio: bool = False
    has_subs: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def total_bitrate(self) -> int:
        return self.fmt_bitrate or (self.vbitrate + self.abitrate)

    def summary(self) -> str:
        v = f"{self.vcodec} {self.width}x{self.height} {self.fps:.3g}fps"
        if self.pix_fmt:
            v += f" {self.pix_fmt}"
        v += f" @ {self.vbitrate/1000:.0f} kbps" if self.vbitrate else ""
        a = f"{self.acodec} @ {self.abitrate/1000:.0f} kbps" if self.has_audio else "无音轨"
        return f"{fmt_clock(self.duration)} · {human_size(self.size)} · {self.container} · 视频 {v} · 音频 {a}"


def _ratio_to_float(s: Optional[str]) -> float:
    try:
        num, den = (s or "0/1").split("/")
        return float(num) / float(den) if float(den) else 0.0
    except Exception:
        return 0.0


def probe(path: str) -> MediaInfo:
    """用 ffprobe 读取时长、码率、编码、分辨率等"""
    cmd = [_ffprobe(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 读取失败: {proc.stderr.strip()[:400]}")
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    info = MediaInfo(path=path, raw=data)
    info.container = (fmt.get("format_name") or "").split(",")[0]
    info.duration = float(fmt.get("duration") or 0.0)
    info.size = int(fmt.get("size") or 0)
    info.fmt_bitrate = int(float(fmt.get("bit_rate") or 0))

    vs = None
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            vs = s
            break
    if vs:
        info.vcodec = vs.get("codec_name") or ""
        info.width = int(vs.get("width") or 0)
        info.height = int(vs.get("height") or 0)
        info.fps = _ratio_to_float(vs.get("avg_frame_rate")) or _ratio_to_float(vs.get("r_frame_rate"))
        info.pix_fmt = vs.get("pix_fmt") or ""
        info.vbitrate = int(float(vs.get("bit_rate") or 0))
        if not info.duration:
            info.duration = float(vs.get("duration") or 0.0)

    for s in streams:
        if s.get("codec_type") == "audio":
            info.has_audio = True
            info.acodec = s.get("codec_name") or ""
            info.abitrate = int(float(s.get("bit_rate") or 0))
            break
    info.has_subs = any(s.get("codec_type") == "subtitle" for s in streams)

    # 容器码率缺失时用 文件大小/时长 兜底
    if not info.fmt_bitrate and info.duration > 0 and info.size:
        info.fmt_bitrate = int(info.size * 8 / info.duration)
    if not info.vbitrate and info.fmt_bitrate:
        info.vbitrate = max(0, info.fmt_bitrate - info.abitrate)
    return info


# ---------------------------------------------------------------- 关键帧

def keyframes(path: str) -> list:
    """返回视频流所有关键帧的时间戳（秒，升序）。只读包头，很快。"""
    cmd = [_ffprobe(), "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"读取关键帧失败: {proc.stderr.strip()[:400]}")
    out = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2 or "K" not in parts[1]:
            continue
        try:
            out.append(float(parts[0]))
        except ValueError:
            continue
    return sorted(out)


def snap_start(kfs: list, t: float) -> float:
    """<= t 的最后一个关键帧；若 t 在第一个关键帧之前，返回第一个关键帧"""
    prev = [k for k in kfs if k <= t + 1e-6]
    if prev:
        return prev[-1]
    return kfs[0] if kfs else 0.0


def snap_end(kfs: list, t: float) -> float:
    """>= t 的下一个关键帧；用于「从关键帧到关键帧」的干净切点"""
    nxt = [k for k in kfs if k >= t - 1e-6]
    return nxt[0] if nxt else t


# ---------------------------------------------------------------- 进程执行

def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)


def _run_with_progress(cmd: list, total: Optional[float],
                       on_progress: Optional[Callable[[float], None]]) -> subprocess.CompletedProcess:
    """跑 ffmpeg，解析 -progress pipe:1 的 out_time_us 汇报进度（0~1）"""
    errfile = tempfile.TemporaryFile(mode="w+b")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW)
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") and total and on_progress:
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    on_progress(min(max(us / 1_000_000 / total, 0.0), 1.0))
        proc.wait()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    errfile.seek(0)
    stderr = errfile.read().decode("utf-8", "replace")
    errfile.close()
    return subprocess.CompletedProcess(cmd, proc.returncode, "", stderr)


# ---------------------------------------------------------------- 剪辑

_COPYABLE_AUDIO = {"aac", "mp3", "opus", "vorbis", "ac3", "eac3", "flac", "alac", "pcm_s16le", "mp2"}


def plan_copy(info: MediaInfo, kfs: list, start: float, end: float) -> tuple:
    """无损剪切的实际切点：起点吸附到关键帧，终点不动"""
    k = snap_start(kfs, start)
    return k, max(0.04, end - k)


def build_copy_cmd(src: str, dst: str, k: float, dur: float,
                   all_streams: bool = False) -> list:
    """拼出无损剪切的 ffmpeg 命令（不执行），方便预览/调试"""
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-ss", fmt_time(k), "-i", src, "-t", f"{dur:.6f}"]
    cmd += ["-map", "0"] if all_streams else ["-map", "0:v:0?", "-map", "0:a?"]
    cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    if dst.lower().endswith((".mp4", ".m4v", ".mov")):
        cmd += ["-movflags", "+faststart"]
    cmd += [dst]
    return cmd


def cut_copy(src: str, dst: str, start: float, end: float,
             info: Optional[MediaInfo] = None, all_streams: bool = False,
             dry_run: bool = False) -> dict:
    """无损剪切（流复制）：画质 / 码率 / 编码参数完全不变。

    返回 dict: 实际起点、时长、体积、命令
    """
    info = info or probe(src)
    kfs = keyframes(src)
    k, dur = plan_copy(info, kfs, start, end)
    cmd = build_copy_cmd(src, dst, k, dur, all_streams)
    if dry_run:
        return {"start": k, "duration": dur, "size": 0, "cmd": cmd, "bitrate": 0}

    proc = _run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg 失败: {proc.stderr.strip()[:600]}")
    size = os.path.getsize(dst) if os.path.exists(dst) else 0
    return {"start": k, "duration": dur, "size": size, "cmd": cmd,
            "bitrate": int(size * 8 / dur) if dur > 0 else 0}


def cut_accurate(src: str, dst: str, start: float, end: float,
                 info: Optional[MediaInfo] = None,
                 on_progress: Optional[Callable[[float], None]] = None,
                 crf: Optional[int] = None,
                 preserve_audio: bool = True,
                 dry_run: bool = False) -> dict:
    """精确到帧的剪切：起点不被吸附，代价是必须重编码。

    默认把输出码率对齐源视频（-b:v = 源视频码率），所以体积 ≈ 原码率 × 新时长，
    既不会莫名膨胀，画质损失也控制在一次转码以内。给 crf 则改用恒定质量模式。
    """
    info = info or probe(src)
    dur = max(0.04, end - start)
    vb = info.vbitrate or max(0, info.total_bitrate - info.abitrate) or 4_000_000

    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-ss", fmt_time(start), "-i", src, "-t", f"{dur:.6f}",
           "-map", "0:v:0", "-map", "0:a?"]
    if crf is not None:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium",
                "-b:v", str(vb), "-maxrate", str(int(vb * 1.5)), "-bufsize", str(vb * 2)]
    cmd += ["-pix_fmt", "yuv420p"]
    if info.has_audio:
        if preserve_audio and info.acodec in _COPYABLE_AUDIO:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", str(max(128_000, info.abitrate or 192_000))]
    if dst.lower().endswith((".mp4", ".m4v", ".mov")):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", dst]

    if dry_run:
        return {"start": start, "duration": dur, "size": 0, "cmd": cmd, "bitrate": 0}
    proc = _run_with_progress(cmd, dur, on_progress)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg 失败: {proc.stderr.strip()[:600]}")
    size = os.path.getsize(dst) if os.path.exists(dst) else 0
    return {"start": start, "duration": dur, "size": size, "cmd": cmd,
            "bitrate": int(size * 8 / dur) if dur > 0 else 0}


def cut(src: str, dst: str, start: float, end: float, accurate: bool = False,
        **kwargs) -> dict:
    """统一入口：accurate=False 走流复制，True 走重编码"""
    info = probe(src)
    if end <= start:
        raise ValueError("出点必须晚于入点")
    if accurate:
        return cut_accurate(src, dst, start, end, info=info, **kwargs)
    return cut_copy(src, dst, start, end, info=info,
                    all_streams=kwargs.get("all_streams", False))


# ---------------------------------------------------------------- 命令行

def _cli(argv: list) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="lossless_cut",
        description="基于 FFmpeg 的无损视频剪切（-c copy，不改变码率）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="查看媒体信息")
    p_info.add_argument("file")

    p_keys = sub.add_parser("keys", help="列出关键帧时间点")
    p_keys.add_argument("file")

    p_cut = sub.add_parser("cut", help="剪切一段")
    p_cut.add_argument("file")
    p_cut.add_argument("-s", "--start", required=True, help="入点，如 00:01:23.500")
    p_cut.add_argument("-e", "--end", required=True, help="出点，如 00:02:45.000")
    p_cut.add_argument("-o", "--output", default=None, help="输出文件")
    p_cut.add_argument("--accurate", action="store_true",
                       help="精确到帧（会重编码，码率对齐源视频）")
    p_cut.add_argument("--crf", type=int, default=None, help="精确模式下改用 CRF 质量")
    p_cut.add_argument("--all-streams", action="store_true",
                       help="连字幕/数据流一起复制（仅无损模式）")
    p_cut.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")

    args = ap.parse_args(argv)

    if args.cmd == "info":
        info = probe(args.file)
        print(f"文件   : {info.path}")
        print(f"概要   : {info.summary()}")
        print(f"时长   : {fmt_clock(info.duration)}  ({info.duration:.3f} s)")
        print(f"体积   : {human_size(info.size)}")
        print(f"总码率 : {info.total_bitrate/1000:.0f} kbps")
        if info.vcodec:
            print(f"视频   : {info.vcodec} {info.width}x{info.height} "
                  f"{info.fps:.4g}fps {info.pix_fmt} {info.vbitrate/1000:.0f} kbps")
        if info.has_audio:
            print(f"音频   : {info.acodec} {info.abitrate/1000:.0f} kbps")
        print(f"关键帧 : {len(keyframes(args.file))} 个")
        return 0

    if args.cmd == "keys":
        for t in keyframes(args.file):
            print(fmt_clock(t))
        return 0

    start = parse_time(args.start)
    end = parse_time(args.end)
    info = probe(args.file)
    if end > info.duration + 0.001:
        print(f"提示：出点 {fmt_clock(end)} 超过时长 {fmt_clock(info.duration)}，已截断")
        end = info.duration

    out = args.output
    if not out:
        stem, ext = os.path.splitext(args.file)
        out = f"{stem}_cut_{int(start)}-{int(end)}{ext or '.mp4'}"

    if not args.accurate:
        k, dur = plan_copy(info, keyframes(args.file), start, end)
        if abs(k - start) > 0.001:
            print(f"无损模式：入点吸附到关键帧 {fmt_clock(k)}（原入点 {fmt_clock(start)}）")

    if args.dry_run:
        if args.accurate:
            res = cut_accurate(args.file, out, start, end, info=info,
                               crf=args.crf, dry_run=True)
        else:
            res = cut_copy(args.file, out, start, end, info=info,
                           all_streams=args.all_streams, dry_run=True)
        print(f"实际范围：{fmt_clock(res['start'])} → {fmt_clock(res['start'] + res['duration'])}"
              f"（{res['duration']:.3f}s）")
        print("命令：")
        print(subprocess.list2cmdline(res["cmd"]))
        return 0

    t0 = time.time()
    if args.accurate:
        res = cut_accurate(args.file, out, start, end, info=info, crf=args.crf)
    else:
        res = cut_copy(args.file, out, start, end, info=info, all_streams=args.all_streams)
    dt = time.time() - t0
    print(f"完成：{out}")
    print(f"  实际范围 : {fmt_clock(res['start'])} → {fmt_clock(res['start']+res['duration'])}"
          f"（时长 {res['duration']:.3f}s，耗时 {dt:.1f}s）")
    print(f"  体积     : {human_size(res['size'])}  平均码率 {res['bitrate']/1000:.0f} kbps")
    print(f"  源体积   : {human_size(info.size)}  源平均码率 {info.total_bitrate/1000:.0f} kbps")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
