#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ffeditor_core.py —— 多片段视频剪辑内核（FFmpeg 驱动，零第三方依赖）

能力：
  剪切（入点/出点）· 片段排序（移动）· 定格 · 倒放 · 旋转 · 镜像
  画面放大/缩小 · 插入图片 · 插入其他视频 · 音量放大/缩小

渲染策略：每个片段先按统一画布（分辨率/帧率/像素格式/音频格式）单独编码，
再用 concat 分离器无损拼接。这样每个片段可以带完全不同的滤镜，内存占用可控，
进度可以按片段汇报，而且最终只产生一次有损编码。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
APP_NAME = "FFEditor"
SAMPLE_RATE = 48000
CHANNELS = 2


# ---------------------------------------------------------------- 路径 / 工具

def resource_dir() -> str:
    return getattr(sys, "_MEIPASS", "")


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CFG_FILE = os.path.join(CFG_DIR, "config.json")
MEDIA_CACHE = os.path.join(CFG_DIR, "proxy_cache")
_CACHE_DIR_OK = [None]


def cache_dir() -> str:
    """片段缓存目录：优先 %APPDATA%，不可写就退到系统临时目录"""
    if _CACHE_DIR_OK[0] is not None:
        return _CACHE_DIR_OK[0]
    for d in (MEDIA_CACHE, os.path.join(tempfile.gettempdir(), "FFEditorCache")):
        try:
            os.makedirs(d, exist_ok=True)
            probe_file = os.path.join(d, ".wtest")
            with open(probe_file, "w") as f:
                f.write("x")
            os.remove(probe_file)
            _CACHE_DIR_OK[0] = d
            return d
        except Exception:
            continue
    _CACHE_DIR_OK[0] = ""
    return ""


def load_config() -> dict:
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class ToolMissing(RuntimeError):
    def __init__(self, name: str):
        super().__init__(
            f"找不到 {name}。请安装 FFmpeg 并保证它在 PATH 中，\n"
            f"或把 ffmpeg.exe / ffprobe.exe 放到本程序同目录，\n"
            f"也可以在程序里手动指定 FFmpeg 所在文件夹。"
        )
        self.name = name


_TOOLS: dict = {"ffmpeg": "", "ffprobe": ""}


def _candidates(name: str) -> Optional[str]:
    exe = name + (".exe" if os.name == "nt" else "")
    dirs = []
    env = os.environ.get(name.upper())
    if env:
        dirs.append(env)
    cfg = load_config().get("ffmpeg_dir")
    if cfg:
        dirs.append(cfg)
    if resource_dir():
        dirs.append(resource_dir())
    dirs.append(app_dir())
    for d in dirs:
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    p = shutil.which(name) or shutil.which(exe)
    if p:
        return p
    for d in (
        r"C:\ffmpeg\bin",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        os.path.expandvars(r"%PROGRAMDATA%\chocolatey\bin"),
        os.path.expanduser(r"~\scoop\shims"),
    ):
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return None


def tool(name: str) -> str:
    if not _TOOLS.get(name):
        p = _candidates(name)
        if not p:
            raise ToolMissing(name)
        _TOOLS[name] = p
    return _TOOLS[name]


def set_tool_dir(folder: str) -> None:
    """用户在界面里手动指定 FFmpeg 目录"""
    _TOOLS["ffmpeg"] = _TOOLS["ffprobe"] = ""
    cfg = load_config()
    cfg["ffmpeg_dir"] = folder
    save_config(cfg)


def tools_ok() -> bool:
    try:
        tool("ffmpeg")
        tool("ffprobe")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 时间 / 尺寸

_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(text) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().rstrip("sS")
    if not s:
        raise ValueError("空的时间值")
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"无法识别的时间: {text!r}（可用 83.5 / 1:23.5 / 01:23:45.600）")
    h, mi, sec = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec)


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def fmt_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:06.3f}"


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _even(v: float, lo: int = 2) -> int:
    v = int(round(v))
    return max(lo, v - (v % 2))


# ---------------------------------------------------------------- 媒体信息

@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    vbitrate: int = 0
    pix_fmt: str = ""
    acodec: str = ""
    abitrate: int = 0
    has_audio: bool = False
    has_video: bool = False

    @property
    def is_image(self) -> bool:
        return self.has_video and self.duration <= 0.1

    def summary(self) -> str:
        if not self.has_video:
            return f"{self.acodec} 音频 · {fmt_clock(self.duration)}"
        s = f"{self.width}×{self.height} · {self.fps:.3g}fps · {fmt_clock(self.duration)}"
        if self.vcodec:
            s += f" · {self.vcodec}"
        s += " · 有音轨" if self.has_audio else " · 无音轨"
        return s


_PROBE_CACHE: dict = {}


def _ratio(s: Optional[str]) -> float:
    try:
        a, b = (s or "0/1").split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        return 0.0


def probe(path: str) -> MediaInfo:
    key = os.path.abspath(path)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    cmd = [tool("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"无法读取 {os.path.basename(path)}：{proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    info = MediaInfo(path=path)
    info.duration = float(fmt.get("duration") or 0.0)
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            info.has_video = True
            info.vcodec = s.get("codec_name") or ""
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.fps = _ratio(s.get("avg_frame_rate")) or _ratio(s.get("r_frame_rate")) or 25.0
            info.pix_fmt = s.get("pix_fmt") or ""
            info.vbitrate = int(float(s.get("bit_rate") or 0))
            if not info.duration:
                info.duration = float(s.get("duration") or 0.0)
        elif s.get("codec_type") == "audio":
            info.has_audio = True
            info.acodec = s.get("codec_name") or ""
            info.abitrate = int(float(s.get("bit_rate") or 0))
    if info.is_image:
        info.duration = 0.0
        info.has_audio = False
    _PROBE_CACHE[key] = info
    return info


def keyframes(path: str) -> list:
    cmd = [tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=CREATE_NO_WINDOW)
    out = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                out.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(out)


# ---------------------------------------------------------------- 片段 / 工程

@dataclass
class Clip:
    kind: str = "video"          # video | image
    path: str = ""
    start: float = 0.0           # 入点（秒）
    duration: float = 5.0
    reverse: bool = False        # 倒放
    rotate: int = 0              # 0 / 90 / 180 / 270（顺时针）
    mirror: str = "none"         # none | h | v | hv
    zoom: float = 1.0            # 1.0 = 原始；>1 放大；<1 缩小
    volume: float = 1.0          # 1.0 = 原始；2.0 = 200%；0 = 静音

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def has_effects(self) -> bool:
        return (self.reverse or self.rotate % 360 != 0 or self.mirror != "none"
                or abs(self.zoom - 1) > 1e-3 or abs(self.volume - 1) > 1e-3)

    def effects_text(self) -> str:
        t = []
        if self.reverse:
            t.append("倒放")
        if self.rotate % 360:
            t.append(f"旋转{self.rotate % 360}°")
        if self.mirror != "none":
            t.append({"h": "镜像↔", "v": "镜像↕", "hv": "镜像↔↕"}.get(self.mirror, ""))
        if abs(self.zoom - 1) > 1e-3:
            t.append(f"缩放{self.zoom * 100:.0f}%")
        if abs(self.volume - 1) > 1e-3:
            t.append(f"音量{self.volume * 100:.0f}%")
        return " ".join(x for x in t if x)


@dataclass
class Project:
    clips: list = field(default_factory=list)
    width: int = 1280
    height: int = 720
    fps: float = 30.0
    preset: str = "veryfast"
    audio_codec: str = "pcm_s16le"

    @property
    def total_duration(self) -> float:
        return sum(max(0.0, c.duration) for c in self.clips)

    def starts(self) -> list:
        """每个片段在时间线上的起始时间（累积）"""
        out, t = [], 0.0
        for c in self.clips:
            out.append(t)
            t += max(0.0, c.duration)
        return out

    def locate(self, t: float):
        """时间线上的 t 秒落在哪个片段：返回 (索引, 片段内局部时间)，越界返回 (-1, 0)"""
        if not self.clips:
            return -1, 0.0
        t = max(0.0, t)
        acc = 0.0
        for i, c in enumerate(self.clips):
            d = max(0.0, c.duration)
            if t < acc + d or i == len(self.clips) - 1:
                return i, min(max(0.0, t - acc), max(0.0, d))
            acc += d
        return -1, 0.0

    def fit_first(self) -> None:
        """用第一个视频片段决定画布尺寸与帧率"""
        for c in self.clips:
            if c.kind == "video":
                try:
                    info = probe(c.path)
                except Exception:
                    continue
                if info.width and info.height:
                    self.width = _even(info.width)
                    self.height = _even(info.height)
                if info.fps:
                    self.fps = round(min(max(info.fps, 1.0), 60.0), 3)
                return


# ---------------------------------------------------------------- 滤镜链

def video_filter(clip: Clip, w: int, h: int, fps: float, reverse: bool = False) -> str:
    p = ["setpts=PTS-STARTPTS"]
    if reverse:
        p.append("reverse")
    p.append(f"fps={fps:.3f}")
    r = clip.rotate % 360
    if r == 90:
        p.append("transpose=1")
    elif r == 180:
        p.append("transpose=2,transpose=2")
    elif r == 270:
        p.append("transpose=2")
    p.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2")
    p.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    p.append("setsar=1")
    if abs(clip.zoom - 1.0) > 1e-3:
        f = clip.zoom
        p.append(f"scale=trunc(iw*{f:.4f}/2)*2:trunc(ih*{f:.4f}/2)*2")
        if f > 1:
            p.append(f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2")
        else:
            p.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    if clip.mirror in ("h", "hv"):
        p.append("hflip")
    if clip.mirror in ("v", "hv"):
        p.append("vflip")
    p.append("format=yuv420p")
    return ",".join(p)


def audio_filter(clip: Clip, reverse: bool = False) -> str:
    p = ["asetpts=PTS-STARTPTS"]
    if reverse:
        p.append("areverse")
    if abs(clip.volume - 1.0) > 1e-3:
        p.append(f"volume={clip.volume:.4f}")
    p.append(f"aresample={SAMPLE_RATE}")
    p.append(f"aformat=sample_fmts=fltp:channel_layouts=stereo")
    return ",".join(p)


def encode_args(project: Project, quality) -> list:
    """quality = ("crf", 20) 或 ("bitrate", 6000000)

    片段统一封装成 MKV + PCM 音频：PCM 没有编码器预延迟（AAC priming），
    拼接时不会在每个接缝处多出 21ms 的静音，最终再统一压一次 AAC。
    """
    gop = max(1, int(round(project.fps * 2)))
    args = ["-c:v", "libx264", "-preset", project.preset, "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop),
            "-sc_threshold", "0"]
    if project.audio_codec == "aac":
        args += ["-c:a", "aac", "-b:a", "128k", "-ar", str(SAMPLE_RATE),
                 "-ac", str(CHANNELS)]
    else:
        args += ["-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS)]
    if quality[0] == "bitrate":
        b = int(quality[1])
        args += ["-b:v", str(b), "-maxrate", str(int(b * 1.5)), "-bufsize", str(b * 2)]
    else:
        args += ["-crf", str(int(quality[1]))]
    return args


# ---------------------------------------------------------------- 进程执行

def _run(cmd: list, total: Optional[float] = None,
         cb: Optional[Callable[[float], None]] = None) -> subprocess.CompletedProcess:
    errfile = tempfile.TemporaryFile(mode="w+b")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW)
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                if cb and total and line.startswith("out_time_us="):
                    try:
                        us = int(line.strip().split("=", 1)[1])
                    except ValueError:
                        continue
                    cb(min(max(us / 1_000_000 / total, 0.0), 1.0))
        proc.wait()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    errfile.seek(0)
    err = errfile.read().decode("utf-8", "replace")
    errfile.close()
    return subprocess.CompletedProcess(cmd, proc.returncode, "", err)


def _check(proc: subprocess.CompletedProcess) -> None:
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip()
        raise RuntimeError("FFmpeg 执行失败：\n" + (msg[-1200:] if msg else "未知错误"))


# ---------------------------------------------------------------- 单片段渲染

def _render_video(project: Project, clip: Clip, out: str, quality,
                  start: float, dur: float, reverse: bool,
                  cb: Optional[Callable[[float], None]] = None) -> None:
    info = probe(clip.path)
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", clip.path]
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}"]

    vf = video_filter(clip, project.width, project.height, project.fps, reverse=reverse)
    af = audio_filter(clip, reverse=reverse)
    fc = f"[0:v]{vf}[v]"
    if info.has_audio:
        fc += f";[0:a]{af}[a]"
    else:
        fc += f";[1:a]{af}[a]"

    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    cmd += encode_args(project, quality)
    cmd += ["-progress", "pipe:1", "-nostats", out]
    _check(_run(cmd, dur, cb))


def _render_image(project: Project, clip: Clip, out: str, quality,
                  cb: Optional[Callable[[float], None]] = None) -> None:
    dur = max(0.04, clip.duration)
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-loop", "1", "-framerate", f"{project.fps:.3f}", "-t", f"{dur:.3f}",
           "-i", clip.path,
           "-f", "lavfi", "-t", f"{dur:.3f}",
           "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}"]
    vf = video_filter(clip, project.width, project.height, project.fps, reverse=False)
    fc = f"[0:v]{vf}[v];[1:a]asetpts=PTS-STARTPTS[a]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    cmd += encode_args(project, quality)
    cmd += ["-progress", "pipe:1", "-nostats", out]
    _check(_run(cmd, dur, cb))


def _write_concat_list(paths: list, listfile: str) -> None:
    with open(listfile, "w", encoding="utf-8", newline="\n") as f:
        for p in paths:
            f.write("file '" + os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") + "'\n")


def _concat_copy(paths: list, out: str) -> None:
    """中间拼接（片段参数一致，直接复制流）"""
    if len(paths) == 1:
        shutil.copyfile(paths[0], out)
        return
    listfile = out + ".concat.txt"
    _write_concat_list(paths, listfile)
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", out]
    try:
        _check(_run(cmd))
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass


def _concat_final(paths: list, out: str, copy_audio: bool = False) -> None:
    """最终拼接：视频直接复制（零质量损失），音频统一编码一次 AAC"""
    listfile = out + ".concat.txt"
    _write_concat_list(paths, listfile)
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-f", "concat", "-safe", "0", "-i", listfile, "-c:v", "copy"]
    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS)]
    cmd += ["-movflags", "+faststart", out]
    try:
        _check(_run(cmd))
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass


def render_segment(project: Project, clip: Clip, out: str, quality,
                   tmpdir: str,
                   cb: Optional[Callable[[float], None]] = None) -> None:
    """把一个片段渲染成与工程画布完全一致的 mp4"""
    if clip.kind == "image":
        _render_image(project, clip, out, quality, cb)
        return
    if not clip.reverse:
        _render_video(project, clip, out, quality, clip.start, clip.duration, False, cb)
        return

    # 倒放：分块倒序渲染再拼接，避免 reverse 滤镜一次性缓存整段画面
    chunk = 1.0
    n = max(1, int(math.ceil(clip.duration / chunk - 1e-6)))
    parts = []
    for k in range(n):
        s = clip.start + k * chunk
        d = min(chunk, clip.start + clip.duration - s)
        p = os.path.join(tmpdir, f"rev_{os.path.basename(out)}_{k:03d}.mp4")
        span = d / clip.duration

        def sub_cb(v, k=k, span=span):
            if cb:
                cb(min(1.0, ((n - 1 - k) + v) * span))

        _render_video(project, clip, p, quality, s, d, True, sub_cb)
        parts.append(p)
    parts.reverse()
    _concat_copy(parts, out)
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    if cb:
        cb(1.0)


def _segment_key(project: Project, clip: Clip, quality) -> str:
    """片段渲染结果的缓存键：参数一样就直接复用，避免重复转码"""
    import hashlib
    st = ""
    try:
        st = f"{os.path.getsize(clip.path)}|{int(os.path.getmtime(clip.path))}"
    except OSError:
        pass
    raw = "|".join(str(x) for x in (
        clip.path, st, clip.kind, round(clip.start, 3), round(clip.duration, 3),
        clip.reverse, clip.rotate, clip.mirror, clip.zoom, clip.volume,
        project.width, project.height, round(project.fps, 3),
        project.preset, quality[0], quality[1], project.audio_codec,
    ))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]


def _prune_cache(folder: str, keep: int = 160) -> None:
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.endswith(".mkv")]
        if len(files) <= keep:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - keep]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------- 导出工程

def export_project(project: Project, out: str, quality,
                   on_progress: Optional[Callable[[float], None]] = None,
                   on_log: Optional[Callable[[str], None]] = None,
                   cache_dir: Optional[str] = None) -> dict:
    if not project.clips:
        raise ValueError("时间线是空的，先添加素材")
    total = max(0.001, project.total_duration)
    tmpdir = tempfile.mkdtemp(prefix="ffed_")
    segs = []
    done = 0.0
    try:
        for i, clip in enumerate(project.clips):
            if on_log:
                on_log(f"渲染片段 {i + 1}/{len(project.clips)}：{clip.name}")
            base = done
            d = max(0.001, clip.duration)

            def cb(v, base=base, d=d):
                if on_progress:
                    on_progress(min(1.0, (base + v * d) / total))

            seg = None
            if cache_dir:
                key = _segment_key(project, clip, quality)
                cached = os.path.join(cache_dir, key + ".mkv")
                if os.path.exists(cached) and os.path.getsize(cached) > 1024:
                    seg = cached
                    if on_progress:
                        on_progress(min(1.0, (base + d) / total))
            if seg is None:
                seg = os.path.join(tmpdir, f"seg_{i:03d}.mkv")
                render_segment(project, clip, seg, quality, tmpdir, cb)
                if cache_dir:
                    try:
                        os.makedirs(cache_dir, exist_ok=True)
                        shutil.copyfile(seg, os.path.join(cache_dir, key + ".mkv"))
                    except Exception:
                        pass
            segs.append(seg)
            done += clip.duration
            if on_progress:
                on_progress(min(1.0, done / total))
        if on_log:
            on_log("拼接片段…")
        _concat_final(segs, out, copy_audio=(project.audio_codec != "pcm_s16le"))
        if cache_dir:
            _prune_cache(cache_dir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"path": out, "duration": project.total_duration,
            "size": os.path.getsize(out) if os.path.exists(out) else 0}


# ---------------------------------------------------------------- 代理预览 / 分割

def render_proxy(project: Project, out: str, width: int = 720,
                 on_progress: Optional[Callable[[float], None]] = None,
                 cache_dir: Optional[str] = None) -> dict:
    """渲染低分辨率代理，用于时间线实时播放和拖动预览"""
    if not project.clips:
        raise ValueError("时间线是空的")
    w = _even(min(width, project.width))
    h = _even(w * project.height / max(1, project.width))
    p = Project(clips=project.clips, width=w, height=h,
                fps=project.fps, preset="ultrafast",
                audio_codec="aac")
    return export_project(p, out, ("crf", 30), on_progress=on_progress,
                          cache_dir=cache_dir)


def split_clip(project: Project, index: int, t_global: float) -> int:
    """在时间线 t_global 处把第 index 段切成两段，返回后半段索引（失败返回 -1）"""
    if not (0 <= index < len(project.clips)):
        return -1
    starts = project.starts()
    clip = project.clips[index]
    local = t_global - starts[index]
    if local <= 0.04 or local >= clip.duration - 0.04:
        return -1
    left = replace(clip)
    right = replace(clip)
    left.duration = local
    right.start = clip.start + local
    right.duration = clip.duration - local
    project.clips[index:index + 1] = [left, right]
    return index + 1


# ---------------------------------------------------------------- 无损剪切（单片段纯剪切）

def lossless_trim(src: str, out: str, start: float, end: float) -> dict:
    """单片段、无特效时走流复制：码率与画质完全不变（切点吸附关键帧）"""
    kfs = keyframes(src)
    prev = [k for k in kfs if k <= start + 1e-6]
    k = prev[-1] if prev else (kfs[0] if kfs else 0.0)
    dur = max(0.04, end - k)
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-ss", fmt_time(k), "-i", src, "-t", f"{dur:.6f}",
           "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
           "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out]
    _check(_run(cmd))
    return {"path": out, "start": k, "duration": dur,
            "size": os.path.getsize(out) if os.path.exists(out) else 0}


# ---------------------------------------------------------------- 预览 / 定格

def snapshot(project: Project, clip: Clip, t: float, out_png: str,
             max_width: int = 0) -> str:
    """取 clip 内 t 秒处、带全部特效的一帧，输出 PNG"""
    t = max(0.0, min(t, max(0.0, clip.duration)))
    vf = video_filter(clip, project.width, project.height, project.fps, reverse=False)
    if max_width:
        vf += f",scale='trunc(min({int(max_width)},iw)/2)*2':-2"
    if clip.kind == "image":
        cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
               "-loop", "1", "-i", clip.path, "-frames:v", "1",
               "-vf", vf, "-f", "image2", out_png]
    else:
        eff = clip.duration - t if clip.reverse else t
        eff = clip.start + eff          # 片段内局部时间 → 源文件时间
        pre = max(0.0, eff - 2.0)
        post = eff - pre
        cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
               "-ss", fmt_time(pre), "-i", clip.path,
               "-ss", f"{post:.3f}", "-frames:v", "1",
               "-vf", vf, "-f", "image2", out_png]
    _check(_run(cmd))
    return out_png
