#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FF 剪辑 —— 界面按「剪映专业版」布局复刻，快捷键按 Adobe Premiere

  ┌ 顶栏：logo / 项目名 ────────────────────────── 快捷键 · 导出 ┐
  │ 素材库(左)   │      播放器(中)      │  属性面板(右)          │
  │ 媒体 音频…   │  预览 + 播放控制条    │ 画面 音频 变速 动画 调节 │
  ├──────────────┴──────────────────────┴───────────────────────┤
  │ 时间轴工具条：撤销 重做 分割 删除          吸附 缩放 适应       │
  │ 轨道头 │ 标尺 / 主轨 / 文本 / 音频 + 播放头                    │
  └──────────────────────────────────────────────────────────────┘

播放：低分辨率代理 + ffmpeg -re 实时解码 → Pillow 送窗口；声音用 winsound。
"""
from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffeditor_core as core

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False

# ---------------------------------------------------------------- 配色（剪映深色）
BG = "#17171a"
PANEL = "#1f1f23"
PANEL2 = "#242424"
HEAD = "#1a1a1a"
TRACK = "#202020"
TRACK_EMPTY = "#1d1d1d"
FG = "#e8e8ea"
DIM = "#8d8d96"
ACCENT = "#00d6d6"
SEL = "#ffffff"
CLIP_V = "#0e6e6e"
CLIP_I = "#5a4a8a"
PLAYHEAD = "#ffffff"
RULER = "#6d6d78"

UI_FAMILY = "Microsoft YaHei UI"
UI_FAMILY_BOLD = "Microsoft YaHei UI"

FONT = (UI_FAMILY, 10)
FONT_SM = (UI_FAMILY, 9)
FONT_XS = (UI_FAMILY, 8)
FONT_B = (UI_FAMILY_BOLD, 10)
FONT_MONO = ("Consolas", 10)     # 时间码这类读数用等宽，数字不会跳


def resolve_ui_font(root):
    """优先用 MiSans（装了就用，没装退回微软雅黑）"""
    global UI_FAMILY, UI_FAMILY_BOLD, FONT, FONT_SM, FONT_XS, FONT_B
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    fam = None
    for cand in ("MiSans", "MiSans Normal", "MiSans VF"):
        if cand in fams:
            fam = cand
            break
    if fam is None:
        for f in sorted(fams):
            if f.startswith("MiSans"):
                fam = f
                break
    UI_FAMILY = fam or "Microsoft YaHei UI"
    UI_FAMILY_BOLD = "MiSans Semibold" if "MiSans Semibold" in fams else UI_FAMILY
    FONT = (UI_FAMILY, 10)
    FONT_SM = (UI_FAMILY, 9)
    FONT_XS = (UI_FAMILY, 8)
    FONT_B = (UI_FAMILY_BOLD, 10)
    return UI_FAMILY

ROT_CHOICES = ["不旋转", "顺时针 90°", "180°", "逆时针 90°"]
ROT_MAP = {"不旋转": 0, "顺时针 90°": 90, "180°": 180, "逆时针 90°": 270}
ROT_RMAP = {v: k for k, v in ROT_MAP.items()}
MIR_CHOICES = ["无", "水平", "垂直", "水平+垂直"]
MIR_MAP = {"无": "none", "水平": "h", "垂直": "v", "水平+垂直": "hv"}
MIR_RMAP = {v: k for k, v in MIR_MAP.items()}
RES_CHOICES = ["跟随首个片段", "1920×1080", "1280×720", "854×480"]
RES_MAP = {"1920×1080": (1920, 1080), "1280×720": (1280, 720), "854×480": (854, 480)}
QUALITY_CHOICES = ["高画质", "标准", "小体积", "对齐源码率"]
QUALITY_MAP = {"高画质": ("crf", 18), "标准": ("crf", 20), "小体积": ("crf", 24)}

LEFT_TABS = ["媒体", "音频", "文本", "贴纸", "特效", "转场",
             "字幕", "智能包装", "调色", "模板", "数字人", "插件"]
RIGHT_TABS = ["画面", "音频", "变速", "动画", "调节"]

HELP_TEXT = """空格        播放 / 暂停
← / →       后退 / 前进 1 帧（Shift = 5 帧）
Home / End  跳到开头 / 结尾
I / O       入点 / 出点设到播放头
C           分割（PR 剃刀；剪映是 Ctrl+B）
Delete      删除选中片段
Ctrl+Z      撤销          Ctrl+Shift+Z  重做
+ / -       时间轴放大 / 缩小
\\           时间轴适应窗口
S           吸附开 / 关
Ctrl+M      导出
Ctrl+I      导入素材
↑ / ↓       选中上一个 / 下一个片段
Esc         取消选中
F1          本窗口

时间轴：
  拖片段本体      调整顺序
  拖片段两端      修剪入点 / 出点
  点标尺/空白     移动播放头
  滚轮            左右平移
  Ctrl+滚轮       以鼠标为中心缩放
  右键片段        分割 / 定格 / 倒放 / 镜像 / 旋转 / 删除"""


def _log_exc(tag=""):
    """把异常写到 %APPDATA%\\FFEditor\\crash.log，方便无控制台时排查"""
    try:
        os.makedirs(core.CFG_DIR, exist_ok=True)
        with open(os.path.join(core.CFG_DIR, "crash.log"), "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {tag}\n")
            traceback.print_exc(file=f)
    except Exception:
        pass


def _install_crash_log():
    def hook(t, v, tb):
        try:
            os.makedirs(core.CFG_DIR, exist_ok=True)
            with open(os.path.join(core.CFG_DIR, "crash.log"), "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] uncaught\n")
                traceback.print_exception(t, v, tb, file=f)
        except Exception:
            pass
    sys.excepthook = hook
    try:
        threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


def dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def tc(seconds: float, fps: float = 30.0) -> str:
    fps = max(1.0, fps)
    seconds = max(0.0, seconds)
    total_frames = int(round(seconds * fps))
    f = total_frames % int(round(fps))
    s = total_frames // int(round(fps))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}:{f:02d}"


class Player:
    """ffmpeg -re 从代理实时解码，帧回调到 UI"""

    def __init__(self, on_frame, on_end):
        self.on_frame = on_frame
        self.on_end = on_end
        self.proc = None
        self.thread = None
        self.stop_flag = False
        self.frames = 0
        self.frame_bytes = 0
        self.fps = 30.0

    def start(self, proxy, start_t, fps, size, duration=None):
        self.stop()
        w, h = size
        self.frame_bytes = w * h * 3
        self.fps = max(1.0, fps)
        self.frames = 0
        self.stop_flag = False
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
        cmd = [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
               "-ss", f"{max(0.0, start_t):.3f}", "-re", "-i", proxy]
        if duration:
            cmd += ["-t", f"{max(0.04, duration):.3f}"]
        cmd += ["-an", "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=core.CREATE_NO_WINDOW)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        while not self.stop_flag:
            try:
                buf = proc.stdout.read(self.frame_bytes)
            except Exception:
                break
            if not buf or len(buf) < self.frame_bytes:
                break
            self.frames += 1
            self.on_frame(buf, self.frames)
        self.on_end()

    def stop(self):
        self.stop_flag = True
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.tk.call("tk", "scaling", 96.0 * self.s / 72.0)
        resolve_ui_font(root)
        root.title("FF 剪辑")
        try:
            root.overrideredirect(True)          # 自绘标题栏，去掉系统白条
        except Exception:
            pass
        self._maxed = False
        self._restore_geo = ""
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        win_w = min(int(1280 * self.s), max(900, sw - 60))
        win_h = min(int(820 * self.s), max(620, sh - 110))
        root.geometry(f"+{max(0, (sw - win_w) // 2)}+{max(0, (sh - win_h) // 2 - 40)}")
        root.geometry(f"{win_w}x{win_h}")
        root.minsize(min(int(1080 * self.s), win_w), min(int(680 * self.s), win_h))
        root.configure(bg=BG)

        self.project = core.Project()
        self.tmp = tempfile.mkdtemp(prefix="ffed_ui_")
        self.selected = -1
        self.playhead = 0.0
        self.pps = 60.0
        self.scroll_t = 0.0
        self.snap = True
        self.playing = False
        self._fit_pending = True

        self.media = []           # [{path, kind, dur, info}]
        self._thumbs = {}
        self._strips = {}
        self.media_sel = -1

        self._img = None
        self._frame_lock = threading.Lock()
        self._frame_buf = None
        self._frame_no = 0
        self._preview_gen = 0
        self._preview_job = None
        self._proxy_job = None
        self._proxy_busy = False
        self._proxy_dirty = True
        self._deferred_proxy = False
        self._pending_play = False
        self._proxy_path = os.path.join(self.tmp, "proxy.mp4")
        self._wav_path = os.path.join(self.tmp, "preview.wav")
        self._audio = False
        self._audio_ready = False
        self._undo = []
        self._redo = []
        self._drag = None
        self._busy = False
        self._loading = False
        self._play_size = (640, 360)
        self._play_start = 0.0
        self.left_tab = "媒体"
        self._q: queue.Queue = queue.Queue()
        self._drop_queue = []

        self.var_res = tk.StringVar(value="跟随首个片段")
        self.var_quality = tk.StringVar(value="标准")
        self.var_status = tk.StringVar(value="就绪")
        self.var_tc = tk.StringVar(value="00:00:00:00 / 00:00:00:00")
        self.var_play = tk.StringVar(value="▶")
        self.var_proxy = tk.StringVar(value="预览：未生成")

        self.v_start = tk.StringVar(value="0:00:00.000")
        self.v_end = tk.StringVar(value="0:00:05.000")
        self.v_dur = tk.StringVar(value="5.00")
        self.v_rev = tk.BooleanVar(value=False)
        self.v_rot = tk.StringVar(value="不旋转")
        self.v_mir = tk.StringVar(value="无")
        self.v_zoom = tk.StringVar(value="100")
        self.v_vol = tk.StringVar(value="100")

        self.player = Player(self._on_player_frame, self._on_player_end)
        self._build_ui()
        self._apply_taskbar_style()
        self.root.after(60, self._make_grips)
        self.root.bind("<Map>", self._on_map_root)
        self._bind_keys()
        self.root.after(300, self._enable_file_drop)
        self.root.after(30, self._drain)
        self.root.after(120, self._tick_playhead)
        self.root.after(200, self._refresh_all)

    # ==================================================== 界面
    def _build_ui(self):
        s = self.s
        self._style()
        outer = ttk.Frame(self.root, padding=0)
        outer.pack(fill="both", expand=True)

        self._build_topbar(outer)

        # 可拖拽分栏：上（素材 / 预览 / 属性） 下（时间轴）
        vp = ttk.PanedWindow(outer, orient="vertical")
        vp.pack(fill="both", expand=True, padx=int(6 * s), pady=(int(6 * s), 0))
        top = ttk.Frame(vp)
        bottom = ttk.Frame(vp)
        vp.add(top, weight=4)
        vp.add(bottom, weight=2)

        hp = ttk.PanedWindow(top, orient="horizontal")
        hp.pack(fill="both", expand=True)
        f_left = ttk.Frame(hp)
        f_mid = ttk.Frame(hp)
        f_right = ttk.Frame(hp)
        hp.add(f_left, weight=0)
        hp.add(f_mid, weight=1)
        hp.add(f_right, weight=0)

        self._build_left(f_left)
        self._build_preview(f_mid)
        self._build_right(f_right)
        self._build_timeline(bottom)
        self.root.after(200, lambda: self._init_sashes(vp, hp))

        status = ttk.Frame(outer)
        status.pack(fill="x", padx=int(8 * s), pady=(int(2 * s), int(4 * s)))
        ttk.Label(status, textvariable=self.var_status, style="Dim.TLabel").pack(side="left")
        self.pb = ttk.Progressbar(status, mode="determinate", maximum=100,
                                  length=int(180 * s))
        self.pb.pack(side="right")

        self.root.bind_class("TButton", "<ButtonRelease-1>", self._focus_timeline, add="+")

    def _init_sashes(self, vp, hp):
        try:
            h = vp.winfo_height() or 800
            w = hp.winfo_width() or 1200
            vp.sashpos(0, int(h * 0.70))          # 预览占大头
            hp.sashpos(0, int(300 * self.s))
            hp.sashpos(1, max(int(300 * self.s), w - int(300 * self.s)))
        except Exception:
            pass

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=FONT)
        style.configure("Dim.TLabel", background=BG, foreground=DIM, font=FONT_SM)
        style.configure("TButton", font=FONT_SM, padding=(9, 4),
                        background="#2a2a2c", foreground=FG, borderwidth=0,
                        relief="flat", focusthickness=0)
        style.map("TButton",
                  background=[("active", "#343438"), ("pressed", "#3d3d42"),
                              ("disabled", "#232325")],
                  foreground=[("disabled", "#565660")])
        style.configure("Small.TButton", font=FONT_XS, padding=(6, 3),
                        background="#2a2a2c", foreground=FG, borderwidth=0)
        style.map("Small.TButton",
                  background=[("active", "#343438"), ("pressed", "#3d3d42")])
        style.configure("Accent.TButton", font=FONT_B, padding=(18, 6),
                        background=ACCENT, foreground="#08302f", borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", "#31e8e8"), ("pressed", "#00b4b4")],
                  foreground=[("!disabled", "#08302f")])
        style.configure("Tab.TButton", font=FONT_SM, padding=(6, 3))
        style.configure("TabOn.TButton", font=FONT_SM, padding=(6, 3), foreground=ACCENT)
        style.configure("TCheckbutton", background=BG, foreground=FG, font=FONT_SM)
        style.map("TCheckbutton", background=[("active", BG)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", "#3a3a3e")])
        style.configure("TCombobox", font=FONT_SM, fieldbackground="#2a2a2c",
                        background="#2a2a2c", foreground=FG, arrowcolor=FG,
                        bordercolor="#3a3a3e", lightcolor="#2a2a2c",
                        darkcolor="#2a2a2c", padding=3)
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#2a2a2c"), ("!readonly", "#2a2a2c")],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", "#2a2a2c")],
                  selectforeground=[("readonly", FG)])
        style.configure("TSpinbox", font=FONT_SM, fieldbackground="#2a2a2c",
                        background="#2a2a2c", foreground=FG, arrowcolor=FG,
                        bordercolor="#3a3a3e", padding=3)
        style.configure("TEntry", fieldbackground="#2a2a2c", foreground=FG,
                        insertcolor=FG, bordercolor="#3a3a3e")
        style.configure("TPanedwindow", background="#0f0f11")
        style.configure("Sash", sashthickness=int(7 * self.s), gripcount=0,
                        background="#34343a")
        style.configure("TScrollbar", background="#2a2a2c", troughcolor="#141416",
                        arrowcolor="#8a8a92", bordercolor="#141416")
        style.configure("TProgressbar", background=ACCENT)
        style.configure("TNotebook", background=PANEL, borderwidth=0, tabmargins=(0, 2, 0, 0))
        style.configure("TNotebook.Tab", font=FONT_SM, padding=(12, 5),
                        background=PANEL2, foreground=DIM, borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL)],
                  foreground=[("selected", ACCENT)])
        style.configure("Treeview", font=FONT_SM, rowheight=int(38 * self.s),
                        background=PANEL, fieldbackground=PANEL, foreground=FG,
                        borderwidth=0)
        style.map("Treeview", background=[("selected", "#33475e")])

    # ---------------- 顶栏（仿剪映：logo / 菜单 … 标题 … 分享 导出 ／ 窗口键）
    def _build_topbar(self, outer):
        s = self.s
        bar = tk.Frame(outer, bg="#181818", height=int(38 * s))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.topbar = bar

        logo = tk.Canvas(bar, width=int(22 * s), height=int(22 * s), bg="#181818",
                         highlightthickness=0, cursor="fleur")
        logo.pack(side="left", padx=(int(12 * s), int(6 * s)), pady=int(8 * s))
        logo.create_rectangle(1, 1, 21, 21, fill=ACCENT, outline="")
        logo.create_polygon(8, 6, 8, 16, 16, 11, fill="#101010", outline="")

        name = tk.Label(bar, text="FF剪辑", bg="#181818", fg=FG, cursor="fleur",
                        font=(UI_FAMILY_BOLD, 11))
        name.pack(side="left")
        menu = tk.Label(bar, text="菜单", bg="#181818", fg=DIM, font=FONT_SM,
                        cursor="fleur", padx=int(10 * s))
        menu.pack(side="left", padx=(int(8 * s), 0))

        self.lbl_project = tk.Label(bar, text="未命名项目", bg="#181818", fg=FG,
                                    font=FONT_SM, cursor="fleur")
        self.lbl_project.place(relx=0.5, rely=0.5, anchor="center")

        # 窗口键：先 pack 的靠最右
        self._win_btn(bar, "✕", self._close, "#c42b1c")
        self._win_btn(bar, "□", self._toggle_max)
        self._win_btn(bar, "－", self._minimize)

        ttk.Button(bar, text="导出", style="Accent.TButton",
                   command=self.export).pack(side="right", padx=(4, int(10 * s)),
                                             pady=int(5 * s))
        ttk.Button(bar, text="快捷键", command=self.show_help).pack(side="right", padx=4)

        for w in (bar, logo, name, menu, self.lbl_project):
            w.bind("<ButtonPress-1>", self._win_drag_start)
            w.bind("<B1-Motion>", self._win_drag)
            w.bind("<Double-1>", lambda e: self._toggle_max())

    def _win_btn(self, bar, text, cmd, hover="#333338"):
        s = self.s
        b = tk.Label(bar, text=text, bg="#181818", fg="#cfcfd6",
                     font=(UI_FAMILY, 10), padx=int(12 * s), cursor="hand2")
        b.pack(side="right", fill="y")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=hover, fg="#ffffff"))
        b.bind("<Leave>", lambda e: b.configure(bg="#181818", fg="#cfcfd6"))
        return b

    # ---------------- 自绘窗口：拖动 / 缩放 / 最大化
    def _work_area(self):
        """当前窗口所在那块显示器的可用区域（多屏时按窗口所在屏算）"""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.GetParent.restype = ctypes.c_void_p
            u.GetParent.argtypes = [ctypes.c_void_p]
            u.MonitorFromWindow.restype = ctypes.c_void_p
            u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", ctypes.c_ulong)]

            hwnd = u.GetParent(self.root.winfo_id())
            hmon = u.MonitorFromWindow(hwnd, 2)     # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if hmon and u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                if r.right > r.left and r.bottom > r.top:
                    return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _win_drag_start(self, e):
        if self._maxed:
            return
        self._drag_off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _win_drag(self, e):
        off = getattr(self, "_drag_off", None)
        if not off or self._maxed:
            return
        self.root.geometry(f"+{e.x_root - off[0]}+{e.y_root - off[1]}")

    def _toggle_max(self):
        if self._maxed:
            if self._restore_geo:
                self.root.geometry(self._restore_geo)
            self._maxed = False
        else:
            self._restore_geo = self.root.geometry()
            x, y, w, h = self._work_area()
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self._maxed = True

    def _minimize(self):
        try:
            self.root.overrideredirect(False)
            self.root.iconify()
            self.root.after(400, self._reapply_override)
        except Exception:
            pass

    def _reapply_override(self):
        try:
            if self.root.state() == "normal":
                self.root.overrideredirect(True)
        except Exception:
            pass

    def _on_map_root(self, e):
        if e.widget is self.root:
            self.root.after(80, self._reapply_override)

    def _close(self):
        try:
            self.player.stop()
        except Exception:
            pass
        self.root.destroy()

    def _apply_taskbar_style(self):
        """无边框窗口也要有任务栏图标 / Alt+Tab"""
        if os.name != "nt":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            u.GetParent.restype = ctypes.c_void_p
            u.GetParent.argtypes = [ctypes.c_void_p]
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            hwnd = u.GetParent(self.root.winfo_id())
            GWL_EXSTYLE, WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = -20, 0x40000, 0x80
            st = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                             (st | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _make_grips(self):
        """八方向拖拽改窗口大小"""
        s = self.s
        t = max(3, int(4 * s))
        c = max(8, int(10 * s))
        specs = [
            ("n", dict(x=0, y=0, relwidth=1, height=t), "size_ns"),
            ("s", dict(x=0, rely=1.0, y=-t, relwidth=1, height=t), "size_ns"),
            ("w", dict(x=0, y=0, width=t, relheight=1), "size_we"),
            ("e", dict(relx=1.0, x=-t, y=0, width=t, relheight=1), "size_we"),
            ("nw", dict(x=0, y=0, width=c, height=c), "size_nw_se"),
            ("ne", dict(relx=1.0, x=-c, y=0, width=c, height=c), "size_ne_sw"),
            ("sw", dict(x=0, rely=1.0, y=-c, width=c, height=c), "size_ne_sw"),
            ("se", dict(relx=1.0, x=-c, rely=1.0, y=-c, width=c, height=c), "size_nw_se"),
        ]
        self._grips = []
        for edge, kw, cur in specs:
            f = tk.Frame(self.root, bg="#181818", cursor=cur)
            f.place(**kw)
            f.bind("<ButtonPress-1>", lambda e, ed=edge: self._rz_start(e, ed))
            f.bind("<B1-Motion>", self._rz_move)
            f.lift()
            self._grips.append(f)

    def _rz_start(self, e, edge):
        if self._maxed:
            return
        self._rz = {"edge": edge, "x": e.x_root, "y": e.y_root,
                    "gx": self.root.winfo_x(), "gy": self.root.winfo_y(),
                    "gw": self.root.winfo_width(), "gh": self.root.winfo_height()}

    def _rz_move(self, e):
        r = getattr(self, "_rz", None)
        if not r:
            return
        dx = e.x_root - r["x"]
        dy = e.y_root - r["y"]
        x, y, w, h = r["gx"], r["gy"], r["gw"], r["gh"]
        minw = int(900 * self.s)
        minh = int(600 * self.s)
        if "w" in r["edge"]:
            nw = max(minw, w - dx)
            x = x + (w - nw)
            w = nw
        if "e" in r["edge"]:
            w = max(minw, w + dx)
        if "n" in r["edge"]:
            nh = max(minh, h - dy)
            y = y + (h - nh)
            h = nh
        if "s" in r["edge"]:
            h = max(minh, h + dy)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---------------- 左：素材库（导入 + 搜索 + 素材网格）
    def _build_left(self, parent):
        s = self.s
        left = tk.Frame(parent, bg=PANEL)
        left.pack(fill="both", expand=True)

        row = tk.Frame(left, bg=PANEL)
        row.pack(fill="x", padx=int(10 * s), pady=(int(8 * s), int(4 * s)))
        imp = tk.Label(row, text="＋ 导入", bg=PANEL, fg=ACCENT, font=FONT_SM,
                       cursor="hand2")
        imp.pack(side="left")
        imp.bind("<Button-1>", lambda e: self.import_media())
        tk.Label(row, text="搜索", bg=PANEL, fg="#5f5f68", font=FONT_XS).pack(
            side="left", padx=(int(10 * s), int(4 * s)))
        self.var_search = tk.StringVar()
        ent = tk.Entry(row, textvariable=self.var_search, bg="#2b2b2b", fg=FG,
                       insertbackground=FG, relief="flat", font=FONT_XS,
                       highlightthickness=0)
        ent.pack(side="left", fill="x", expand=True, ipady=int(3 * s))
        self.var_search.trace_add("write", lambda *a: self._refresh_media_grid())

        self.left_body = tk.Frame(left, bg=PANEL)
        self.left_body.pack(fill="both", expand=True, pady=(int(4 * s), 0))

        self.page_media = tk.Frame(self.left_body, bg=PANEL)
        self.page_media.pack(fill="both", expand=True)
        self.media_canvas = tk.Canvas(self.page_media, bg=PANEL, highlightthickness=0)
        self.media_canvas.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self.page_media, orient="vertical",
                           command=self.media_canvas.yview)
        sb.pack(side="right", fill="y")
        self.media_canvas.configure(yscrollcommand=sb.set)
        self.media_inner = tk.Frame(self.media_canvas, bg=PANEL)
        self._media_win = self.media_canvas.create_window((0, 0), window=self.media_inner,
                                                         anchor="nw")
        self.media_inner.bind("<Configure>", lambda e: self.media_canvas.configure(
            scrollregion=self.media_canvas.bbox("all")))
        self.media_canvas.bind("<Configure>", lambda e: self.media_canvas.itemconfigure(
            self._media_win, width=e.width))
        self.media_canvas.bind("<MouseWheel>", lambda e: self.media_canvas.yview_scroll(
            int(-e.delta / 120), "units"))

        self.page_other = tk.Frame(self.left_body, bg=PANEL)
        self.other_label = tk.Label(self.page_other, text="", bg=PANEL, fg=DIM,
                                    font=FONT_SM)
        self.other_label.pack(expand=True)

    def _set_left_tab(self, name):
        """保留接口：现在左栏只有「媒体」，其他页签已按要求移除"""
        self.left_tab = name

    # ---------------- 资源管理器拖入（WM_DROPFILES）
    def _enable_file_drop(self):
        if os.name != "nt":
            return
        self._drop_queue = getattr(self, "_drop_queue", [])
        try:
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)

            user32.GetAncestor.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                 ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_uint, ctypes.c_size_t,
                                               ctypes.c_ssize_t]
            shell32.DragAcceptFiles.restype = None
            shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                               ctypes.c_void_p, ctypes.c_uint]
            shell32.DragFinish.restype = None
            shell32.DragFinish.argtypes = [ctypes.c_void_p]

            hwnd = user32.GetAncestor(self.root.winfo_id(), 2)   # GA_ROOT
            if not hwnd:
                return
            shell32.DragAcceptFiles(ctypes.c_void_p(hwnd), True)

            WM_DROPFILES = 0x0233
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                         ctypes.c_uint, ctypes.c_size_t,
                                         ctypes.c_ssize_t)

            def handler(h, msg, wp, lp):
                # 注意：这里绝不调用 Tk，只把路径塞进队列，由主循环消费
                if msg == WM_DROPFILES:
                    try:
                        hdrop = ctypes.c_void_p(wp)
                        n = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                        for i in range(n):
                            ln = shell32.DragQueryFileW(hdrop, i, None, 0)
                            if ln <= 0:
                                continue
                            buf = ctypes.create_unicode_buffer(ln + 1)
                            shell32.DragQueryFileW(hdrop, i,
                                                   ctypes.cast(buf, ctypes.c_void_p),
                                                   ln + 1)
                            self._drop_queue.append(buf.value)
                        shell32.DragFinish(hdrop)
                    except Exception:
                        _log_exc("wm_dropfiles")
                    return 0
                old = self._old_proc
                if old:
                    return user32.CallWindowProcW(ctypes.c_void_p(old),
                                                  ctypes.c_void_p(h), msg, wp, lp)
                return 0

            self._wndproc = WNDPROC(handler)
            self._old_proc = 0
            ptr = ctypes.cast(self._wndproc, ctypes.c_void_p)
            old = user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), -4, ptr)
            self._old_proc = old if old else 0
        except Exception:
            self._old_proc = 0
            _log_exc("drop_install")

    # ---------------- 素材网格
    def _refresh_media_grid(self):
        for w in self.media_inner.winfo_children():
            w.destroy()
        self._media_cards = {}
        kw = self.var_search.get().strip().lower()
        s = self.s
        cols = 2
        idx = 0
        for i, m in enumerate(self.media):
            name = os.path.basename(m["path"])
            if kw and kw not in name.lower():
                continue
            r, c = divmod(idx, cols)
            idx += 1
            card = tk.Frame(self.media_inner, bg=PANEL, cursor="hand2",
                            highlightthickness=2, highlightbackground=PANEL,
                            highlightcolor=PANEL)
            card.grid(row=r, column=c, padx=int(6 * s), pady=int(6 * s), sticky="nw")
            self._media_cards[i] = card
            self.media_inner.columnconfigure(c, weight=1)
            thumb = self._thumbs.get(i)
            if thumb is not None:
                lb = tk.Label(card, image=thumb, bg=PANEL, bd=0)
            else:
                lb = tk.Label(card, text="素材", bg="#2b2b2b", fg=DIM, width=12, height=5)
            lb.pack()
            lb.image = thumb
            cap = tk.Frame(card, bg=PANEL)
            cap.pack(fill="x")
            nm = name if len(name) <= 13 else name[:12] + "…"
            tk.Label(cap, text=nm, bg=PANEL, fg=FG,
                     font=(UI_FAMILY, max(6, int(7 * s)))).pack(side="left")
            if m["kind"] == "video":
                dur = f"{int(m['dur'] // 60):02d}:{int(m['dur'] % 60):02d}"
            else:
                dur = "图片"
            tk.Label(cap, text=dur, bg=PANEL, fg=DIM,
                     font=(UI_FAMILY, max(6, int(7 * s)))).pack(side="right")
            for w in (card, lb):
                w.bind("<ButtonPress-1>", lambda e, k=i: self._media_press(e, k))
                w.bind("<B1-Motion>", self._media_motion)
                w.bind("<ButtonRelease-1>", self._media_release)
                w.bind("<Double-1>", lambda e, k=i: self._add_media(k))
        if idx == 0:
            tk.Label(self.media_inner, text="还没有素材\n点上面「＋ 导入」", bg=PANEL,
                     fg=DIM, font=FONT_SM).grid(row=0, column=0, padx=20, pady=30)
        self._highlight_media()
        self._sync_draft()

    def _highlight_media(self):
        sel = getattr(self, "media_sel", -1)
        for i, card in getattr(self, "_media_cards", {}).items():
            try:
                card.configure(highlightbackground=(ACCENT if i == sel else PANEL))
            except Exception:
                pass

    def _pick_media(self, i):
        self.media_sel = i
        self._highlight_media()
        self._sync_draft()
        self.var_status.set(f"已选中：{os.path.basename(self.media[i]['path'])}（双击或拖到时间轴）")

    def _media_press(self, e, i):
        self._press_pt = (i, e.x_root, e.y_root)
        self._drag_media = None
        self._pick_media(i)

    def _media_motion(self, e):
        pt = getattr(self, "_press_pt", None)
        if not pt:
            return
        i, x0, y0 = pt
        if abs(e.x_root - x0) + abs(e.y_root - y0) > 8:
            self._drag_media = i
            self.var_status.set(f"拖动中：{os.path.basename(self.media[i]['path'])} → 放到时间轴")

    def _media_release(self, e):
        idx = getattr(self, "_drag_media", None)
        self._drag_media = None
        self._press_pt = None
        if idx is None:
            return
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        if w is not self.timeline:
            self.var_status.set("松开在时间轴以外，未添加")
            return
        x = e.x_root - self.timeline.winfo_rootx()
        t = self._x_to_time(x)
        starts = self.project.starts()
        at = len(self.project.clips)
        for i, c in enumerate(self.project.clips):
            if t < starts[i] + c.duration / 2:
                at = i
                break
        self._add_media_at(idx, at)

    def _add_media(self, i):
        self._add_media_at(i, len(self.project.clips))

    def _add_media_at(self, i, at):
        if not (0 <= i < len(self.media)):
            return
        m = self.media[i]
        self.push_undo()
        self.project.clips.insert(max(0, min(at, len(self.project.clips))),
                                  core.Clip(kind=m["kind"], path=m["path"],
                                            start=0.0, duration=m["dur"]))
        if len(self.project.clips) == 1:
            self.project.fit_first()
            self._fit_pending = True
        self._invalidate_proxy()
        sel = max(0, min(at, len(self.project.clips) - 1))
        self._select(sel)
        self.redraw_timeline()
        self._update_tc()

    # ---------------- 中：播放器
    def _build_preview(self, parent):
        s = self.s
        mid = ttk.Frame(parent)
        mid.pack(fill="both", expand=True, padx=int(6 * s))

        tk.Label(mid, text="播放器 · 时间线01", bg="#1a1a1a", fg=DIM, font=FONT_XS,
                 anchor="w", padx=int(10 * s)).pack(side="top", fill="x")

        # 控制条先占位（pack 顺序决定谁先被压缩），画布再吃掉剩余空间
        trans = tk.Frame(mid, bg="#161616", height=int(30 * s))
        trans.pack(side="bottom", fill="x")
        trans.pack_propagate(False)

        self.canvas = tk.Canvas(mid, bg="#000000", highlightthickness=0,
                                width=int(560 * s), height=int(300 * s))
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", self._on_preview_click)

        tk.Label(trans, textvariable=self.var_tc, bg="#161616", fg="#a8a8b0",
                 font=("Consolas", max(7, int(8 * s)))).pack(side="left",
                                                            padx=(int(8 * s), 0))
        ttk.Label(trans, textvariable=self.var_proxy, style="Dim.TLabel").pack(
            side="right", padx=(0, int(8 * s)))

        ctrl = tk.Frame(trans, bg="#161616")
        ctrl.pack(side="left", expand=True)
        for text, cmd in (("｜◀", self.goto_start), ("◀", lambda: self.step(-1)),
                          (None, self.toggle_play), ("▶", lambda: self.step(1)),
                          ("▶｜", self.goto_end)):
            if text is None:
                self.btn_play = self._flat_btn(ctrl, self.var_play, self.toggle_play,
                                               font=(UI_FAMILY, 11))
            else:
                self._flat_btn(ctrl, text, cmd, font=(UI_FAMILY, 11))

    def _flat_btn(self, parent, text_or_var, cmd, font=None):
        """无底色、无边框的按钮（参考剪映播放控制条）"""
        kw = {"text": text_or_var} if isinstance(text_or_var, str) \
            else {"textvariable": text_or_var}
        lbl = tk.Label(parent, bg="#161616", fg="#c8c8d0", font=font or FONT,
                       padx=int(10 * self.s), pady=int(2 * self.s), cursor="hand2",
                       **kw)
        lbl.pack(side="left")
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: lbl.configure(fg="#ffffff"))
        lbl.bind("<Leave>", lambda e: lbl.configure(fg="#c8c8d0"))
        return lbl

    # ---------------- 右：属性
    def _build_right(self, parent):
        s = self.s
        right = tk.Frame(parent, bg=PANEL)
        right.pack(fill="both", expand=True)

        # 草稿参数（未选中片段时显示；有选中的素材就显示该素材的信息）
        self.right_draft = tk.Frame(right, bg=PANEL)
        d = self.right_draft
        tk.Label(d, text="草稿参数", bg=PANEL, fg=FG, font=FONT_B,
                 anchor="w").pack(fill="x", padx=int(12 * s), pady=(int(10 * s), int(6 * s)))
        self._draft_vals = {}
        for k in ("基础名称", "保存位置", "色彩空间", "导入方式", "代理模式",
                  "时间线名称", "比例", "分辨率", "帧率"):
            row = tk.Frame(d, bg=PANEL)
            row.pack(fill="x", padx=int(12 * s), pady=int(3 * s))
            tk.Label(row, text=k, bg=PANEL, fg=DIM, font=FONT_XS, width=8,
                     anchor="w").pack(side="left")
            val = tk.Label(row, text="", bg=PANEL, fg=FG, font=FONT_XS, anchor="w",
                           justify="left")
            val.pack(side="left", fill="x", expand=True)
            self._draft_vals[k] = val
        tk.Label(d, text="选中素材栏里的素材会显示它的参数；\n选中时间线上的片段则切换成 画面 / 音频",
                 bg=PANEL, fg="#5f5f68", font=FONT_XS, justify="left").pack(
            anchor="w", padx=int(12 * s), pady=(int(16 * s), 0))

        self.right_clip = ttk.Frame(right, style="Panel.TFrame")
        self.nb = ttk.Notebook(self.right_clip)
        self.nb.pack(fill="both", expand=True, padx=int(2 * s), pady=int(2 * s))

        page_pic = ttk.Frame(self.nb, style="Panel.TFrame", padding=int(10 * s))
        page_audio = ttk.Frame(self.nb, style="Panel.TFrame", padding=int(10 * s))
        for name, page in (("画面", page_pic), ("音频", page_audio)):
            self.nb.add(page, text=name)

        # 画面
        self.lbl_clip = ttk.Label(page_pic, text="未选中片段", style="Dim.TLabel",
                                  background=PANEL, wraplength=int(270 * s))
        self.lbl_clip.pack(anchor="w", pady=(0, int(8 * s)))

        ttk.Label(page_pic, text="基础", background=PANEL, font=FONT_B).pack(anchor="w")
        zrow = ttk.Frame(page_pic, style="Panel.TFrame")
        zrow.pack(fill="x", pady=(int(6 * s), int(2 * s)))
        ttk.Label(zrow, text="缩放", background=PANEL, width=6).pack(side="left")
        ttk.Spinbox(zrow, textvariable=self.v_zoom, from_=10, to=400, increment=10,
                    width=6, font=FONT_MONO, command=self._apply_props).pack(side="left")
        ttk.Label(zrow, text="%", background=PANEL).pack(side="left")
        self.scale_zoom = ttk.Scale(zrow, from_=10, to=400, value=100,
                                    command=self._on_zoom_scale)
        self.scale_zoom.pack(side="left", fill="x", expand=True, padx=(8, 0))

        rrow = ttk.Frame(page_pic, style="Panel.TFrame")
        rrow.pack(fill="x", pady=int(4 * s))
        ttk.Label(rrow, text="旋转", background=PANEL, width=6).pack(side="left")
        self.cb_rot = ttk.Combobox(rrow, textvariable=self.v_rot, values=ROT_CHOICES,
                                   state="readonly", width=12, font=FONT_SM)
        self.cb_rot.pack(side="left")

        mrow = ttk.Frame(page_pic, style="Panel.TFrame")
        mrow.pack(fill="x", pady=int(4 * s))
        ttk.Label(mrow, text="镜像", background=PANEL, width=6).pack(side="left")
        self.cb_mir = ttk.Combobox(mrow, textvariable=self.v_mir, values=MIR_CHOICES,
                                   state="readonly", width=12, font=FONT_SM)
        self.cb_mir.pack(side="left")

        self.cb_rev = ttk.Checkbutton(page_pic, text="倒放", variable=self.v_rev,
                                      command=self._apply_props)
        self.cb_rev.pack(anchor="w", pady=(int(8 * s), 0))

        ttk.Separator(page_pic).pack(fill="x", pady=int(10 * s))
        ttk.Label(page_pic, text="时间", background=PANEL, font=FONT_B).pack(anchor="w")
        grow = ttk.Frame(page_pic, style="Panel.TFrame")
        grow.pack(fill="x", pady=(int(6 * s), 0))
        grow.columnconfigure(1, weight=1)
        ttk.Label(grow, text="入点", background=PANEL, width=6).grid(row=0, column=0, sticky="w")
        self.e_start = ttk.Entry(grow, textvariable=self.v_start, width=12, font=FONT_MONO)
        self.e_start.grid(row=0, column=1, sticky="ew")
        ttk.Label(grow, text="出点", background=PANEL, width=6).grid(row=1, column=0, sticky="w",
                                                                  pady=(4, 0))
        self.e_end = ttk.Entry(grow, textvariable=self.v_end, width=12, font=FONT_MONO)
        self.e_end.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        ttk.Label(grow, text="时长", background=PANEL, width=6).grid(row=2, column=0, sticky="w",
                                                                  pady=(4, 0))
        self.sp_dur = ttk.Spinbox(grow, textvariable=self.v_dur, from_=0.1, to=3600,
                                  increment=0.5, width=10, font=FONT_MONO,
                                  command=self._on_dur)
        self.sp_dur.grid(row=2, column=1, sticky="w", pady=(4, 0))

        # 音频
        ttk.Label(page_audio, text="音量", background=PANEL, font=FONT_B).pack(anchor="w")
        arow = ttk.Frame(page_audio, style="Panel.TFrame")
        arow.pack(fill="x", pady=(int(6 * s), 0))
        ttk.Spinbox(arow, textvariable=self.v_vol, from_=0, to=300, increment=10,
                    width=6, font=FONT_MONO, command=self._apply_props).pack(side="left")
        ttk.Label(arow, text="%", background=PANEL).pack(side="left")
        self.scale_vol = ttk.Scale(arow, from_=0, to=300, value=100,
                                   command=self._on_vol_scale)
        self.scale_vol.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Label(page_audio, text="提示：导出时生效，预览音量随代理。",
                  style="Dim.TLabel", background=PANEL,
                  wraplength=int(270 * s)).pack(anchor="w", pady=(int(10 * s), 0))

        for w, fn in ((self.e_start, self._on_time_edit), (self.e_end, self._on_time_edit),
                      (self.sp_dur, self._on_dur)):
            w.bind("<Return>", lambda e, f=fn: f())
            w.bind("<FocusOut>", lambda e, f=fn: f())
        for var in (self.v_rot, self.v_mir):
            var.trace_add("write", lambda *a: self._apply_props())
        self._show_right("draft")

    def _show_right(self, view):
        if view == "draft":
            self.right_clip.pack_forget()
            self.right_draft.pack(fill="both", expand=True)
        else:
            self.right_draft.pack_forget()
            self.right_clip.pack(fill="both", expand=True)

    # ---------------- 下：时间轴
    def _build_timeline(self, parent):
        s = self.s
        tools = tk.Frame(parent, bg=BG)
        tools.pack(fill="x", padx=int(8 * s), pady=(int(6 * s), int(2 * s)))

        for text, cmd in (("撤销", self.undo), ("重做", self.redo)):
            ttk.Button(tools, text=text, style="Small.TButton", command=cmd,
                       width=6).pack(side="left", padx=(0, int(4 * s)))
        tk.Frame(tools, bg="#3a3a40", width=1, height=int(18 * s)).pack(
            side="left", padx=int(6 * s))
        for text, cmd in (("分割", self.split_at_playhead), ("删除", self.delete_clip)):
            ttk.Button(tools, text=text, style="Small.TButton", command=cmd,
                       width=6).pack(side="left", padx=(0, int(4 * s)))

        self.var_snap = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="吸附", variable=self.var_snap,
                        command=self.toggle_snap).pack(side="left", padx=int(10 * s))

        ttk.Button(tools, text="适应", style="Small.TButton",
                   command=self.zoom_fit).pack(side="right")
        ttk.Button(tools, text="＋", style="Small.TButton", width=2,
                   command=lambda: self.zoom_by(1.6)).pack(side="right", padx=1)
        ttk.Button(tools, text="－", style="Small.TButton", width=2,
                   command=lambda: self.zoom_by(1 / 1.6)).pack(side="right", padx=1)

        self.timeline = tk.Canvas(parent, bg=BG, highlightthickness=0,
                                  height=int(152 * s), cursor="hand2")
        self.timeline.pack(fill="both", expand=True, padx=int(8 * s))
        self.timeline.bind("<Configure>", lambda e: self.redraw_timeline())
        self.timeline.bind("<Button-1>", self._tl_press)
        self.timeline.bind("<B1-Motion>", self._tl_motion)
        self.timeline.bind("<ButtonRelease-1>", self._tl_release)
        self.timeline.bind("<Double-1>", lambda e: None)
        self.timeline.bind("<Button-3>", self._tl_context)
        self.timeline.bind("<MouseWheel>", self._tl_wheel)
        self.timeline.bind("<Control-MouseWheel>", self._tl_zoom_wheel)

        self.hbar = ttk.Scrollbar(parent, orient="horizontal", command=self._on_hscroll)
        self.hbar.pack(fill="x", padx=int(8 * s))

        self.menu = tk.Menu(self.root, tearoff=0, bg=PANEL2, fg=FG,
                            activebackground=ACCENT, activeforeground="#ffffff",
                            font=FONT_SM)
        self.menu.add_command(label="分割  C", command=self.split_at_playhead)
        self.menu.add_command(label="定格（3 秒）", command=self.insert_freeze)
        self.menu.add_command(label="倒放", command=lambda: self._quick_toggle("reverse"))
        self.menu.add_command(label="镜像（水平）", command=lambda: self._quick_toggle("mirror"))
        self.menu.add_command(label="旋转 90°", command=lambda: self._quick_toggle("rotate"))
        self.menu.add_separator()
        self.menu.add_command(label="删除  Delete", command=self.delete_clip)

    def _focus_timeline(self, _e=None):
        try:
            self.timeline.focus_set()
        except Exception:
            pass

    # ==================================================== 快捷键
    def _bind_keys(self):
        r = self.root

        def key(fn):
            def handler(e):
                try:
                    cls = e.widget.winfo_class()
                except Exception:
                    cls = ""
                if cls in ("Entry", "TEntry", "Text", "TSpinbox", "Spinbox",
                           "TCombobox", "Combobox"):
                    return None
                fn()
                return "break"
            return handler

        def key_ev(fn):
            def handler(e):
                try:
                    cls = e.widget.winfo_class()
                except Exception:
                    cls = ""
                if cls in ("Entry", "TEntry", "Text", "TSpinbox", "Spinbox",
                           "TCombobox", "Combobox"):
                    return None
                fn(e)
                return "break"
            return handler

        r.bind("<space>", key(self.toggle_play))
        r.bind("<Left>", key_ev(lambda e: self.step(-5 if e.state & 1 else -1)))
        r.bind("<Right>", key_ev(lambda e: self.step(5 if e.state & 1 else 1)))
        r.bind("<Home>", key(self.goto_start))
        r.bind("<End>", key(self.goto_end))
        r.bind("<i>", key(lambda: self.mark("in")))
        r.bind("<o>", key(lambda: self.mark("out")))
        r.bind("<c>", key(self.split_at_playhead))
        r.bind("<Delete>", key(self.delete_clip))
        r.bind("<Escape>", key(lambda: self._select(-1)))
        r.bind("<Control-z>", key(self.undo))
        r.bind("<Control-Z>", key(self.redo))
        r.bind("<Control-y>", key(self.redo))
        r.bind("<plus>", key(lambda: self.zoom_by(1.6)))
        r.bind("<equal>", key(lambda: self.zoom_by(1.6)))
        r.bind("<minus>", key(lambda: self.zoom_by(1 / 1.6)))
        r.bind("<backslash>", key(self.zoom_fit))
        r.bind("<s>", key(self.toggle_snap))
        r.bind("<Control-m>", key(self.export))
        r.bind("<Control-i>", key(self.import_media))
        r.bind("<Control-o>", key(self.import_media))
        r.bind("<Up>", key(lambda: self._select(self.selected - 1)))
        r.bind("<Down>", key(lambda: self._select(self.selected + 1)))
        r.bind("<F1>", key(self.show_help))

    # ==================================================== 时间码 / 坐标
    @property
    def fps(self) -> float:
        return max(1.0, self.project.fps)

    @property
    def total(self) -> float:
        return self.project.total_duration

    def _tc(self, t: float) -> str:
        return tc(t, self.fps)

    def _tl_metrics(self):
        s = self.s
        head = int(84 * s)
        ruler = int(22 * s)
        th = int(62 * s)
        sh = int(24 * s)
        return head, ruler, th, sh

    def _time_to_x(self, t: float) -> float:
        head, _, _, _ = self._tl_metrics()
        return head + 4 + (t - self.scroll_t) * self.pps

    def _x_to_time(self, x: float) -> float:
        head, _, _, _ = self._tl_metrics()
        return max(0.0, (x - head - 4) / self.pps + self.scroll_t)

    def _snap_time(self, t: float, exclude: int = -1) -> float:
        if not self.snap or self.pps <= 0:
            return t
        cands = [0.0, self.total]
        starts = self.project.starts()
        for i, c in enumerate(self.project.clips):
            if i == exclude:
                continue
            cands.append(starts[i])
            cands.append(starts[i] + c.duration)
        best, bd = t, 8.0 / self.pps
        for c in cands:
            d = abs(c - t)
            if d < bd:
                best, bd = c, d
        return max(0.0, best)

    # ==================================================== 预览
    def _on_preview_click(self, _e=None):
        try:
            self.canvas.focus_set()
        except Exception:
            pass
        self.toggle_play()

    def _on_canvas_resize(self, _e=None):
        if not self.playing:
            self._schedule_preview(60)

    def _schedule_preview(self, delay=70):
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(delay, self.render_frame)

    def render_frame(self):
        self._preview_job = None
        if self.playing:
            return
        if not self.project.clips:
            self._draw_empty_hint()
            return
        self._preview_gen += 1
        gen = self._preview_gen
        t = self.playhead
        cw = max(320, self.canvas.winfo_width() or 640)
        ch = max(180, self.canvas.winfo_height() or 360)
        ar = self.project.width / max(1, self.project.height)
        mw = int(min(cw, ch * ar, 1600))
        threading.Thread(target=self._render_frame_worker, args=(gen, t, mw),
                         daemon=True).start()

    def _render_frame_worker(self, gen, t, mw):
        try:
            out = os.path.join(self.tmp, f"f_{gen}.png")
            if (self._simple_clip() is None and not self._proxy_dirty
                    and os.path.exists(self._proxy_path)):
                w = core._even(min(1600, mw))
                h = core._even(w * self.project.height / max(1, self.project.width))
                cmd = [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                       "-ss", f"{max(0.0, t):.3f}", "-i", self._proxy_path,
                       "-frames:v", "1", "-vf",
                       f"scale={w}:{h}:force_original_aspect_ratio=decrease", out]
                r = subprocess.run(cmd, capture_output=True,
                                   creationflags=core.CREATE_NO_WINDOW)
                if r.returncode == 0 and os.path.exists(out):
                    self._q.put(("frame_png", (gen, out)))
                    return
            i, local = self.project.locate(t)
            if i < 0:
                return
            core.snapshot(self.project, self.project.clips[i], local, out,
                          max_width=int(min(1600, mw)))
            self._q.put(("frame_png", (gen, out)))
        except Exception as e:
            self._q.put(("status", f"预览失败：{' '.join(str(e).split())[:120]}"))

    def _show_png(self, gen, path):
        if gen != self._preview_gen or not os.path.exists(path):
            return
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            return
        self._img = img
        self.canvas.delete("all")
        cw = self.canvas.winfo_width() or 640
        ch = self.canvas.winfo_height() or 360
        self.canvas.create_image(cw // 2, ch // 2, image=img)

    def _draw_empty_hint(self):
        self.canvas.delete("all")
        cw = max(240, self.canvas.winfo_width())
        ch = max(120, self.canvas.winfo_height())
        self.canvas.create_text(
            cw // 2, ch // 2, fill="#4a4a52", justify="center",
            font=(UI_FAMILY, 11),
            text="导入素材后拖入时间线\n\nCtrl+I 导入　空格播放　C 分割")

    # ---------------- 播放
    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def _simple_clip(self):
        """单片段、无特效时可以直接播源文件，不需要代理"""
        if len(self.project.clips) != 1:
            return None
        c = self.project.clips[0]
        if c.kind != "video" or c.has_effects():
            return None
        return c

    def play(self):
        if not self.project.clips or self._busy:
            return
        if self.playhead >= self.total - 0.02:
            self.playhead = 0.0
        simple = self._simple_clip()
        if simple is None and (self._proxy_dirty or not os.path.exists(self._proxy_path)):
            self._pending_play = True
            self._rebuild_proxy(now=True)
            return
        self._start_playback()

    def _start_playback(self):
        if self.playing:
            return
        cw = max(320, self.canvas.winfo_width() or 640)
        ch = max(180, self.canvas.winfo_height() or 360)
        ar = self.project.width / max(1, self.project.height)
        w, h = cw, int(cw / ar)
        if h > ch:
            h, w = ch, int(ch * ar)
        w = core._even(min(1600, w))
        h = core._even(h)
        self._play_size = (w, h)
        self._play_start = self.playhead
        self.playing = True
        self.var_play.set("■")

        simple = self._simple_clip()
        if simple is not None:
            src = simple.path
            start_t = simple.start + self.playhead
            dur = max(0.05, simple.duration - self.playhead)
            self._prepare_audio(start_t, duration=dur, source=src)
            self.player.start(src, start_t, self.fps, (w, h), duration=dur)
        else:
            self._prepare_audio(self.playhead)
            self.player.start(self._proxy_path, self.playhead, self.fps, (w, h))
        self._start_audio()
        self._draw_frame_loop()

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        self.var_play.set("▶")
        self.player.stop()
        self._audio_stop()
        self._update_tc()
        self._schedule_preview(50)
        if self._proxy_dirty and not self._proxy_busy:
            self._proxy_job = self.root.after(400, lambda: self._rebuild_proxy(now=False))

    def _on_player_frame(self, buf, n):
        with self._frame_lock:
            self._frame_buf = buf
            self._frame_no = n

    def _on_player_end(self):
        self._q.put(("play_end", None))

    def _draw_frame_loop(self):
        if not self.playing:
            return
        buf = None
        n = 0
        with self._frame_lock:
            if self._frame_buf is not None:
                buf = self._frame_buf
                n = self._frame_no
                self._frame_buf = None
        if buf is not None:
            self.playhead = min(self.total, self._play_start + n / self.fps)
            self._display_bytes(buf)
            self._update_tc()
        if self.playing:
            self.root.after(12, self._draw_frame_loop)

    def _display_bytes(self, buf):
        if not HAVE_PIL:
            return
        cw = self.canvas.winfo_width() or 640
        ch = self.canvas.winfo_height() or 360
        w, h = self._play_size
        try:
            img = Image.frombytes("RGB", (w, h), buf)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(cw // 2, ch // 2, image=self._photo)
        except Exception:
            pass

    def _prepare_audio(self, t, duration=None, source=None):
        self._audio_ready = False
        try:
            src = source or self._proxy_path
            if not os.path.exists(src):
                return
            dur = duration if duration is not None else min(120.0, max(0.0, self.total - t))
            dur = min(dur, 120.0)
            if dur < 0.2:
                return
            cmd = [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{t:.3f}", "-t", f"{dur:.3f}", "-i", src,
                   "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", self._wav_path]
            subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=core.CREATE_NO_WINDOW)
            self._audio_ready = os.path.exists(self._wav_path)
        except Exception:
            self._audio_ready = False

    def _start_audio(self):
        if not self._audio_ready:
            return
        try:
            import winsound
            winsound.PlaySound(self._wav_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._audio = True
        except Exception:
            self._audio = False

    def _audio_stop(self):
        if not self._audio:
            return
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        self._audio = False

    def step(self, frames: int):
        if self.playing:
            self.pause()
        self.playhead = max(0.0, min(self.total, self.playhead + frames / self.fps))
        self._after_playhead_move()

    def goto_start(self):
        if self.playing:
            self.pause()
        self.playhead = 0.0
        self._after_playhead_move()

    def goto_end(self):
        if self.playing:
            self.pause()
        self.playhead = max(0.0, self.total)
        self._after_playhead_move()

    def _set_playhead(self, t: float, snap=True):
        t = max(0.0, min(self.total, t))
        if snap:
            t = self._snap_time(t)
        self.playhead = t
        self._after_playhead_move()

    def _after_playhead_move(self):
        self._update_tc()
        self._draw_playhead()
        self._ensure_visible(self.playhead)
        if not self.playing:
            self._schedule_preview(60)

    # ==================================================== 时间轴绘制
    def redraw_timeline(self):
        c = self.timeline
        if not c.winfo_exists():
            return
        w = c.winfo_width() or 1000
        h = c.winfo_height() or 150
        s = self.s
        head, ruler, th, sh = self._tl_metrics()

        if self._fit_pending and w > 100:
            self._fit_pending = False
            if self.total < 0.5:
                self.pps = 60.0
            else:
                self.pps = max(3.0, min(1200.0, (w - head - 24) / self.total))
            self.scroll_t = 0.0

        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill=BG, outline="")
        c.create_rectangle(0, 0, head, h, fill=HEAD, outline="")

        # 标尺：仿剪映（秒 + 帧内 5f/10f 子标签 + 密集标线）
        self._clamp_scroll()
        tick, label, sub = self._ruler_steps()
        end_t = self.scroll_t + (w - head - 24) / self.pps + tick
        t = math.floor(self.scroll_t / tick) * tick
        while t <= end_t:
            x = self._time_to_x(t)
            if head <= x <= w + 1:
                c.create_line(x, ruler - 4, x, ruler, fill="#3c3c44")
            t += tick
        t = math.floor(self.scroll_t / label) * label
        while t <= end_t:
            x = self._time_to_x(t)
            if head - 1 <= x <= w + 44:
                c.create_line(x, ruler - 9, x, ruler, fill=RULER)
                c.create_text(x + 3, 1, anchor="nw", fill=RULER,
                              font=("Consolas", max(7, int(8 * s))),
                              text=self._tick_label(t, label))
                if sub:
                    k = 1
                    while k * sub < label - 1e-9:
                        xs = self._time_to_x(t + k * sub)
                        if head <= xs <= w + 1:
                            c.create_line(xs, ruler - 6, xs, ruler, fill="#4a4a54")
                            c.create_text(xs + 2, 3, anchor="nw", fill="#6a6a74",
                                          font=("Consolas", max(6, int(7 * s))),
                                          text=f"{int(round(k * sub * self.fps))}f")
                        k += 1
            t += label
        c.create_line(head, ruler, w, ruler, fill="#3a3a42")

        # 主轨
        y0 = ruler + 4
        c.create_rectangle(head, y0, w, y0 + th, fill=TRACK, outline="")
        c.create_text(int(8 * s), y0 + th // 2, text="主轨", anchor="w", fill=DIM,
                      font=FONT_SM)

        starts = self.project.starts()
        for i, clip in enumerate(self.project.clips):
            x0 = self._time_to_x(starts[i])
            x1 = self._time_to_x(starts[i] + clip.duration)
            if x1 < head - 10 or x0 > w + 10:
                continue
            sel = (i == self.selected)
            fill = CLIP_V if clip.kind == "video" else CLIP_I
            top, bot = y0 + 2, y0 + th - 2
            hdr = int(18 * s)
            c.create_rectangle(x0, top, x1, bot, fill=fill, outline="")

            strip = self._strips.get(clip.path)
            if strip is not None and x1 - x0 > 8:
                step = strip.width()
                xx = x0 + 1
                while xx + step <= x1 - 1:
                    c.create_image(xx, top + hdr, image=strip, anchor="nw")
                    xx += step

            c.create_rectangle(x0, top, x1, top + hdr, fill="#0a4a4a", outline="")
            name = clip.name
            max_chars = max(3, int((x1 - x0) / (7 * s)))
            if len(name) > max_chars:
                name = name[:max(1, max_chars - 1)] + "…"
            c.create_text(x0 + 5, top + hdr / 2, anchor="w", fill="#ffffff",
                          font=(UI_FAMILY, max(7, int(7 * s))),
                          text=f"{name}  {self._tc(clip.duration)}")
            if clip.effects_text() and x1 - x0 > 150:
                c.create_text(x1 - 6, top + hdr / 2, anchor="e", fill="#ffe9a8",
                              font=(UI_FAMILY, max(6, int(7 * s))),
                              text=clip.effects_text())
            c.create_rectangle(x0, top, x1, bot, outline=SEL if sel else "#0b3d3d",
                               width=2 if sel else 1)
            if sel:
                for hx in (x0, x1):
                    c.create_rectangle(hx - 3, top, hx + 3, bot, fill=SEL, outline="")

        # 文本 / 音频 空轨
        y1 = y0 + th + 4
        c.create_rectangle(head, y1, w, y1 + sh, fill=TRACK_EMPTY, outline="")
        c.create_text(int(8 * s), y1 + sh // 2, text="文本", anchor="w", fill=DIM,
                      font=FONT_XS)
        y2 = y1 + sh + 4
        c.create_rectangle(head, y2, w, y2 + sh, fill=TRACK_EMPTY, outline="")
        c.create_text(int(8 * s), y2 + sh // 2, text="音频", anchor="w", fill=DIM,
                      font=FONT_XS)

        if not self.project.clips:
            c.create_text((head + w) // 2, y0 + th // 2, fill="#4a4a52",
                          text="把素材拖到这里（或双击左侧素材）", font=FONT_SM)

        self._draw_playhead()
        self._update_hbar()

    def _draw_playhead(self):
        c = self.timeline
        c.delete("ph")
        w = c.winfo_width() or 1000
        x = self._time_to_x(self.playhead)
        if -20 <= x <= w + 20:
            c.create_line(x, 0, x, c.winfo_height(), fill=PLAYHEAD, width=1, tags="ph")
            c.create_polygon(x - 5, 0, x + 5, 0, x, 8, fill=PLAYHEAD, outline="", tags="ph")

    def _ruler_steps(self):
        """返回 (最小刻度, 主标签间隔, 帧内子标签间隔或 None)

        仿剪映：主标签是秒（0:03），放大后一秒内每 5 帧标一个「5f/10f/…」，
        再放大时最小刻度到单帧。
        """
        frame = 1.0 / self.fps
        label = 1800
        for st in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800):
            if st * self.pps >= 88:
                label = st
                break
        sub = None
        if label == 1 and (5 * frame) * self.pps >= 72:
            sub = 5 * frame
        if frame * self.pps >= 5:
            tick = frame
        else:
            tick = label
            for div in (10, 5, 2):
                if (label / div) * self.pps >= 9:
                    tick = label / div
                    break
        return tick, max(tick, label), sub

    def _tick_label(self, t, step):
        if step < 1:
            return self._tc(t)[3:]          # MM:SS:FF
        s = int(t)
        return f"{s // 60}:{s % 60:02d}"

    def _clamp_scroll(self):
        head, _, _, _ = self._tl_metrics()
        visible = max(0.0, (self.timeline.winfo_width() - head - 24) / self.pps)
        self.scroll_t = max(0.0, min(self.scroll_t, max(0.0, self.total - visible + 1.0)))

    def _ensure_visible(self, t):
        head, _, _, _ = self._tl_metrics()
        w = self.timeline.winfo_width() or 1000
        x = self._time_to_x(t)
        if x < head + 30:
            self.scroll_t = max(0.0, t - 30 / self.pps)
            self.redraw_timeline()
        elif x > w - 30:
            self.scroll_t = t - (w - head - 30) / self.pps
            self.redraw_timeline()

    def _update_tc(self):
        self.var_tc.set(f"{self._tc(self.playhead)} / {self._tc(self.total)}")

    # ---------------- 缩放 / 滚动
    def zoom_by(self, factor, anchor_t=None):
        anchor = self.playhead if anchor_t is None else anchor_t
        x = self._time_to_x(anchor)
        self.pps = max(3.0, min(1200.0, self.pps * factor))
        head, _, _, _ = self._tl_metrics()
        self.scroll_t = anchor - (x - head - 4) / self.pps
        self._clamp_scroll()
        self.redraw_timeline()
        self._schedule_preview(60)

    def zoom_fit(self):
        head, _, _, _ = self._tl_metrics()
        w = self.timeline.winfo_width() or 1000
        self.pps = max(3.0, min(1200.0, (w - head - 24) / max(1.0, self.total)))
        self.scroll_t = 0.0
        self.redraw_timeline()
        self._schedule_preview(60)

    def _tl_zoom_wheel(self, e):
        self.zoom_by(1.2 if e.delta > 0 else 1 / 1.2, anchor_t=self._x_to_time(e.x))

    def _tl_wheel(self, e):
        delta = e.delta if abs(e.delta) < 120 else (120 if e.delta > 0 else -120)
        self.scroll_t -= (delta / 120.0) * (150 / self.pps)
        self._clamp_scroll()
        self.redraw_timeline()

    def _on_hscroll(self, *args):
        head, _, _, _ = self._tl_metrics()
        if args[0] == "moveto":
            visible = (self.timeline.winfo_width() - head - 24) / self.pps
            self.scroll_t = float(args[1]) * max(self.total, visible)
        elif args[0] == "scroll":
            self.scroll_t += int(args[1]) * (self.timeline.winfo_width() or 800) / self.pps * 0.5
        self._clamp_scroll()
        self.redraw_timeline()

    def _update_hbar(self):
        head, _, _, _ = self._tl_metrics()
        visible = max(0.01, (self.timeline.winfo_width() - head - 24) / self.pps)
        total = max(self.total, visible)
        first = min(1.0, self.scroll_t / total)
        last = min(1.0, (self.scroll_t + visible) / total)
        try:
            self.hbar.set(first, last)
        except Exception:
            pass

    # ---------------- 鼠标
    def _tl_press(self, e):
        if self.playing:
            self.pause()
        self._focus_timeline()
        head, ruler, th, sh = self._tl_metrics()
        y0 = ruler + 4
        if e.y <= ruler:
            self._drag = {"mode": "scrub"}
            self._set_playhead(self._x_to_time(e.x))
            return
        if not (y0 <= e.y <= y0 + th + 4):
            self._drag = {"mode": "scrub"}
            self._set_playhead(self._x_to_time(e.x))
            return
        idx, edge = self._hit_clip(e.x)
        if idx < 0:
            self._select(-1)
            self._drag = {"mode": "scrub"}
            self._set_playhead(self._x_to_time(e.x))
            return
        self._select(idx)
        self._drag = {"mode": "trim_l" if edge == "l" else ("trim_r" if edge == "r" else "move"),
                      "idx": idx, "x0": e.x, "started": False, "target": idx,
                      "clip": core.replace(self.project.clips[idx])}
        self._set_playhead(self._x_to_time(e.x), snap=False)

    def _tl_context(self, e):
        idx, _ = self._hit_clip(e.x)
        if idx >= 0:
            self._select(idx)
            try:
                self.menu.tk_popup(e.x_root, e.y_root)
            finally:
                self.menu.grab_release()

    def _tl_motion(self, e):
        d = self._drag
        if not d:
            return
        if d["mode"] == "scrub":
            self._set_playhead(self._x_to_time(e.x))
            return
        idx = d["idx"]
        clip = self.project.clips[idx]
        base = d["clip"]
        starts = self.project.starts()
        if d["mode"] == "trim_l":
            new_start_t = self._snap_time(self._x_to_time(e.x), exclude=idx)
            delta = new_start_t - starts[idx]
            delta = max(-base.start, min(delta, base.duration - 0.15))
            clip.start = base.start + delta
            clip.duration = base.duration - delta
            self.redraw_timeline()
        elif d["mode"] == "trim_r":
            new_end = self._snap_time(self._x_to_time(e.x), exclude=idx)
            dur = new_end - starts[idx]
            if base.kind == "video":
                try:
                    dur = min(dur, core.probe(base.path).duration - base.start)
                except Exception:
                    pass
            clip.duration = max(0.15, dur)
            self.redraw_timeline()
        elif d["mode"] == "move":
            if not d["started"] and abs(e.x - d["x0"]) > 6:
                d["started"] = True
            if d["started"]:
                d["target"] = self._drop_index(e.x, idx)
                self._draw_drop_marker(d["target"])

    def _tl_release(self, e):
        d = self._drag
        self._drag = None
        if not d:
            return
        if d["mode"] in ("trim_l", "trim_r"):
            self._invalidate_proxy()
            self._sync_props_from_clip()
            self.redraw_timeline()
            self._schedule_preview(60)
        elif d["mode"] == "move" and d.get("started"):
            target = d.get("target", d["idx"])
            if target != d["idx"]:
                self.push_undo()
                clip = self.project.clips.pop(d["idx"])
                self.project.clips.insert(target, clip)
                self.selected = target
                self._invalidate_proxy()
            self.redraw_timeline()
            self._schedule_preview(60)
        self._update_tc()

    def _hit_clip(self, x):
        starts = self.project.starts()
        _, ruler, th, _ = self._tl_metrics()
        for i, clip in enumerate(self.project.clips):
            x0 = self._time_to_x(starts[i])
            x1 = self._time_to_x(starts[i] + clip.duration)
            if x0 - 4 <= x <= x1 + 4:
                if abs(x - x0) <= 6:
                    return i, "l"
                if abs(x - x1) <= 6:
                    return i, "r"
                return i, ""
        return -1, ""

    def _drop_index(self, x, idx):
        starts = self.project.starts()
        t = self._x_to_time(x)
        for i, clip in enumerate(self.project.clips):
            if t < starts[i] + clip.duration / 2:
                return i if i <= idx else i - 1
        return len(self.project.clips) - 1

    def _draw_drop_marker(self, idx):
        c = self.timeline
        c.delete("drop")
        starts = self.project.starts()
        _, ruler, _, _ = self._tl_metrics()
        x = self._time_to_x(starts[idx]) if idx < len(starts) else self._time_to_x(self.total)
        c.create_line(x, ruler, x, c.winfo_height(), fill="#ffd23f", width=2, dash=(4, 3),
                      tags="drop")
        self._draw_playhead()

    # ==================================================== 片段属性
    def _select(self, idx: int):
        idx = max(-1, min(idx, len(self.project.clips) - 1))
        self.selected = idx
        if idx >= 0:
            self._sync_props_from_clip()
            starts = self.project.starts()
            a = starts[idx]
            if not (a <= self.playhead <= a + self.project.clips[idx].duration):
                self.playhead = a
                self._after_playhead_move()
        else:
            self.lbl_clip.configure(text="未选中片段")
        self._show_right("clip" if idx >= 0 else "draft")
        if idx < 0:
            self._sync_draft()
        self.redraw_timeline()
        self._schedule_preview(50)

    def _sync_props_from_clip(self):
        if not (0 <= self.selected < len(self.project.clips)):
            self.lbl_clip.configure(text="未选中片段")
            return
        clip = self.project.clips[self.selected]
        self._loading = True
        try:
            self.lbl_clip.configure(text=clip.name)
            self.v_start.set(core.fmt_clock(clip.start))
            self.v_end.set(core.fmt_clock(clip.start + clip.duration))
            self.v_dur.set(f"{clip.duration:.2f}")
            self.v_rev.set(bool(clip.reverse))
            self.v_rot.set(ROT_RMAP.get(clip.rotate % 360, "不旋转"))
            self.v_mir.set(MIR_RMAP.get(clip.mirror, "无"))
            self.v_zoom.set(f"{clip.zoom * 100:.0f}")
            self.v_vol.set(f"{clip.volume * 100:.0f}")
            self.scale_zoom.set(clip.zoom * 100)
            self.scale_vol.set(clip.volume * 100)
        finally:
            self._loading = False

    def _on_zoom_scale(self, v):
        if self._loading:
            return
        self.v_zoom.set(f"{float(v):.0f}")
        self._apply_props()

    def _on_vol_scale(self, v):
        if self._loading:
            return
        self.v_vol.set(f"{float(v):.0f}")
        self._apply_props()

    def _on_time_edit(self):
        if self._loading or not (0 <= self.selected < len(self.project.clips)):
            return
        clip = self.project.clips[self.selected]
        try:
            a = core.parse_time(self.v_start.get())
            b = core.parse_time(self.v_end.get())
        except Exception:
            return
        if clip.kind == "video":
            try:
                info = core.probe(clip.path)
                a = max(0.0, min(a, info.duration))
                b = max(a + 0.1, min(b, info.duration))
            except Exception:
                pass
        self.push_undo()
        clip.start, clip.duration = a, b - a
        self._loading = True
        try:
            self.v_dur.set(f"{clip.duration:.2f}")
        finally:
            self._loading = False
        self._invalidate_proxy()
        self.redraw_timeline()

    def _on_dur(self):
        if self._loading or not (0 <= self.selected < len(self.project.clips)):
            return
        clip = self.project.clips[self.selected]
        try:
            d = max(0.1, float(self.v_dur.get()))
        except ValueError:
            return
        if clip.kind == "video":
            try:
                d = min(d, core.probe(clip.path).duration - clip.start)
            except Exception:
                pass
        self.push_undo()
        clip.duration = d
        self.v_end.set(core.fmt_clock(clip.start + d))
        self._invalidate_proxy()
        self.redraw_timeline()

    def _apply_props(self):
        if self._loading or not (0 <= self.selected < len(self.project.clips)):
            return
        clip = self.project.clips[self.selected]
        try:
            clip.zoom = max(0.1, min(4.0, float(self.v_zoom.get()) / 100.0))
            clip.volume = max(0.0, min(3.0, float(self.v_vol.get()) / 100.0))
        except ValueError:
            return
        clip.reverse = bool(self.v_rev.get())
        clip.rotate = ROT_MAP.get(self.v_rot.get(), 0)
        clip.mirror = MIR_MAP.get(self.v_mir.get(), "none")
        self._invalidate_proxy()
        self.redraw_timeline()
        self._schedule_preview(80)

    # ==================================================== 素材库
    VID_EXT = (".mp4", ".mkv", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".flv",
               ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp")
    IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff")

    def import_media(self):
        paths = filedialog.askopenfilenames(
            title="导入素材",
            filetypes=[("所有支持的素材",
                        "*.mp4 *.mkv *.mov *.avi *.ts *.m2ts *.webm *.flv *.wmv *.m4v "
                        "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if paths:
            self._import_paths(paths)

    def _import_paths(self, paths):
        """支持文件、文件夹（扫描一层），也用于资源管理器拖入"""
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p):
                try:
                    names = sorted(os.listdir(p))
                except Exception:
                    continue
                cands = [os.path.join(p, n) for n in names]
            else:
                cands = [p]
            for fp in cands:
                if not os.path.isfile(fp):
                    continue
                low = fp.lower()
                is_img = low.endswith(self.IMG_EXT)
                if not (is_img or low.endswith(self.VID_EXT)):
                    continue
                try:
                    info = core.probe(fp)
                except Exception:
                    continue
                kind = "image" if is_img else "video"
                dur = 4.0 if is_img else max(0.2, info.duration)
                self.media.append({"path": fp, "kind": kind, "dur": dur, "info": info})
                self._thumbs[len(self.media) - 1] = self._make_thumb(fp, kind, dur)
                added += 1
        if added:
            self._refresh_media_grid()
            self.var_status.set(f"导入 {added} 个素材，双击或拖到时间轴")
        elif paths:
            self.var_status.set("没有找到可用的视频/图片")

    def _make_thumb(self, path, kind, dur):
        try:
            out = os.path.join(self.tmp, f"th_{abs(hash(path))}.png")
            if kind == "image":
                cmd = [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                       "-i", path, "-frames:v", "1", "-vf",
                       "scale=150:84:force_original_aspect_ratio=decrease,"
                       "pad=150:84:(ow-iw)/2:(oh-ih)/2:color=black", out]
            else:
                ss = min(max(1.0, dur * 0.08), 30.0)
                cmd = [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                       "-ss", f"{ss:.2f}", "-i", path,
                       "-frames:v", "1", "-vf",
                       "scale=150:84:force_original_aspect_ratio=decrease,"
                       "pad=150:84:(ow-iw)/2:(oh-ih)/2:color=black", out]
            subprocess.run(cmd, capture_output=True, timeout=20,
                           creationflags=core.CREATE_NO_WINDOW)
            if os.path.exists(out):
                self._make_strip(path, kind, dur)
                return tk.PhotoImage(file=out)
        except Exception:
            pass
        return None

    def _make_strip(self, path, kind, dur):
        """时间轴胶卷条用的小图"""
        try:
            h = max(20, int(46 * self.s))
            w = max(30, int(h * 16 / 9))
            out = os.path.join(self.tmp, f"strip_{abs(hash(path))}.png")
            ss = [] if kind == "image" else ["-ss", f"{min(max(1.0, dur * 0.08), 30.0):.2f}"]
            cmd = [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"] + ss + [
                "-i", path, "-frames:v", "1", "-vf",
                f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}", out]
            subprocess.run(cmd, capture_output=True, timeout=20,
                           creationflags=core.CREATE_NO_WINDOW)
            if os.path.exists(out):
                self._strips[path] = tk.PhotoImage(file=out)
        except Exception:
            pass

    def open_files(self, paths):
        """命令行 / 自检：导入素材并直接加入时间线"""
        image_ext = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff")
        first = not self.project.clips
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                info = core.probe(p)
            except Exception:
                continue
            kind = "image" if p.lower().endswith(image_ext) else "video"
            dur = 4.0 if kind == "image" else max(0.2, info.duration)
            self.media.append({"path": p, "kind": kind, "dur": dur, "info": info})
            self._thumbs[len(self.media) - 1] = self._make_thumb(p, kind, dur)
            self.project.clips.append(core.Clip(kind=kind, path=p, start=0.0, duration=dur))
        self._refresh_media_grid()
        if first and self.project.clips:
            self.project.fit_first()
            self._fit_pending = True
        self._invalidate_proxy()
        self._select(0)
        self.redraw_timeline()
        self._update_tc()

    def add_selected_media(self):
        if 0 <= getattr(self, "media_sel", -1) < len(self.media):
            self._add_media(self.media_sel)

    def _quick_toggle(self, what):
        if not (0 <= self.selected < len(self.project.clips)):
            return
        clip = self.project.clips[self.selected]
        self.push_undo()
        if what == "reverse":
            clip.reverse = not clip.reverse
        elif what == "mirror":
            clip.mirror = "none" if clip.mirror == "h" else "h"
        elif what == "rotate":
            clip.rotate = (clip.rotate + 90) % 360
        self._sync_props_from_clip()
        self._invalidate_proxy()
        self.redraw_timeline()
        self._schedule_preview(60)

    # ==================================================== 编辑操作
    def delete_clip(self):
        if not (0 <= self.selected < len(self.project.clips)):
            return
        self.push_undo()
        del self.project.clips[self.selected]
        self.selected = min(self.selected, len(self.project.clips) - 1)
        self.playhead = min(self.playhead, self.total)
        self._invalidate_proxy()
        self._select(self.selected)
        self.redraw_timeline()
        self._update_tc()

    def split_at_playhead(self):
        i, _ = self.project.locate(self.playhead)
        if i < 0:
            return
        self.push_undo()
        new_i = core.split_clip(self.project, i, self.playhead)
        if new_i < 0:
            self._undo.pop()
            self.var_status.set("播放头在片段边缘，无法分割")
            return
        self._invalidate_proxy()
        self._select(new_i)
        self.redraw_timeline()
        self.var_status.set("已在播放头处分割")

    def mark(self, which):
        if not (0 <= self.selected < len(self.project.clips)):
            return
        starts = self.project.starts()
        clip = self.project.clips[self.selected]
        a = starts[self.selected]
        if not (a + 0.05 < self.playhead < a + clip.duration - 0.05):
            self.var_status.set("播放头不在选中片段内")
            return
        local = self.playhead - a
        self.push_undo()
        if which == "in":
            clip.start += local
            clip.duration -= local
        else:
            clip.duration = local
        self._invalidate_proxy()
        self._sync_props_from_clip()
        self.redraw_timeline()

    def insert_freeze(self):
        i, local = self.project.locate(self.playhead)
        if i < 0:
            return
        out = os.path.join(self.tmp, f"freeze_{int(time.time() * 1000)}.png")
        self.var_status.set("正在生成定格画面…")
        self._busy = True
        clip = self.project.clips[i]

        def work():
            try:
                core.snapshot(self.project, clip, local, out)
                self._q.put(("freeze", (out, i)))
            except Exception as e:
                self._q.put(("error", f"定格失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _on_freeze(self, png, idx):
        self._busy = False
        if not os.path.exists(png):
            return
        self.push_undo()
        self.project.clips.insert(idx + 1, core.Clip(kind="image", path=png, duration=3.0))
        self._invalidate_proxy()
        self._select(idx + 1)
        self.redraw_timeline()
        self.var_status.set("已插入 3 秒定格")

    # ==================================================== 撤销
    def push_undo(self):
        self._undo.append([core.replace(c) for c in self.project.clips])
        if len(self._undo) > 60:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return
        self._redo.append([core.replace(c) for c in self.project.clips])
        self.project.clips = self._undo.pop()
        self.selected = min(self.selected, len(self.project.clips) - 1)
        self.playhead = min(self.playhead, self.total)
        self._invalidate_proxy()
        self._select(self.selected)
        self.redraw_timeline()
        self.var_status.set("已撤销")

    def redo(self):
        if not self._redo:
            return
        self._undo.append([core.replace(c) for c in self.project.clips])
        self.project.clips = self._redo.pop()
        self.selected = min(self.selected, len(self.project.clips) - 1)
        self._invalidate_proxy()
        self._select(self.selected)
        self.redraw_timeline()
        self.var_status.set("已重做")

    def toggle_snap(self):
        self.snap = bool(self.var_snap.get())
        self.var_status.set("吸附：" + ("开" if self.snap else "关"))

    # ==================================================== 代理
    def _invalidate_proxy(self):
        self._proxy_gen = getattr(self, "_proxy_gen", 0) + 1
        self._proxy_dirty = True
        if self._simple_clip() is not None:
            # 单片段无特效：直接播源文件，不用生成代理
            self._proxy_dirty = False
            self.var_proxy.set("预览：直接读取源文件")
            return
        if self._proxy_job:
            self.root.after_cancel(self._proxy_job)
        self._proxy_job = self.root.after(1200, lambda: self._rebuild_proxy(now=False))

    def _rebuild_proxy(self, now=False):
        if self._proxy_busy or not self.project.clips:
            return
        self._proxy_job = None
        if self.playing:
            self._deferred_proxy = True
            return
        if self._simple_clip() is not None:
            self._proxy_dirty = False
            return
        gen = getattr(self, "_proxy_gen", 0)
        self._proxy_busy = True
        self._proxy_t0 = time.time()
        self.var_proxy.set("预览：生成中 0%")
        self.var_status.set("正在生成预览代理…")
        project = self.project
        out = self._proxy_path + ".tmp.mp4"

        def work():
            try:
                core.render_proxy(
                    project, out, width=720,
                    on_progress=lambda v: self._q.put(("proxy_progress", v)),
                    cache_dir=core.cache_dir() or None)
                if gen != getattr(self, "_proxy_gen", 0):
                    self._q.put(("proxy_stale", None))
                    return
                for _ in range(12):
                    try:
                        os.replace(out, self._proxy_path)
                        break
                    except PermissionError:
                        time.sleep(0.15)
                else:
                    raise RuntimeError("代理文件被占用")
                self._q.put(("proxy_done", None))
            except Exception as e:
                self._q.put(("proxy_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_proxy_stale(self):
        """生成期间工程又变了：丢弃结果，稍后重建（片段有缓存，通常很快）"""
        self._proxy_busy = False
        self._proxy_dirty = True
        try:
            os.remove(self._proxy_path + ".tmp.mp4")
        except OSError:
            pass
        self._proxy_job = self.root.after(600, lambda: self._rebuild_proxy(now=False))

    def _on_proxy_done(self):
        self._proxy_busy = False
        self._proxy_dirty = False
        self.var_proxy.set("预览：就绪")
        self.var_status.set("预览就绪")
        if self._pending_play:
            self._pending_play = False
            self._start_playback()
        elif not self.playing:
            self._schedule_preview(30)

    # ==================================================== 导出
    def _quality(self):
        name = self.var_quality.get()
        if name == "对齐源码率":
            for c in self.project.clips:
                if c.kind == "video":
                    info = core.probe(c.path)
                    if info.vbitrate > 0:
                        return ("bitrate", info.vbitrate)
            return ("crf", 20)
        return QUALITY_MAP.get(name, ("crf", 20))

    def export(self):
        if self._busy:
            return
        if not self.project.clips:
            messagebox.showinfo("提示", "时间线是空的")
            return
        if self.playing:
            self.pause()
        self._open_export_dialog()

    def _open_export_dialog(self):
        s = self.s
        win = tk.Toplevel(self.root)
        win.withdraw()
        try:
            win.overrideredirect(True)          # 去掉小白条，自己画
        except Exception:
            pass
        win.configure(bg="#1e1e1e")
        win.transient(self.root)

        dw, dh = int(860 * s), int(620 * s)
        try:
            ax, ay = self.root.winfo_rootx(), self.root.winfo_rooty()
            aw, ah = self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            ax = ay = 0
            aw, ah = dw, dh
        wx, wy = ax + (aw - dw) // 2, ay + (ah - dh) // 2
        mx, my, mw, mh = self._work_area()
        wx = max(mx, min(wx, mx + mw - dw))
        wy = max(my, min(wy, my + mh - dh))
        win.geometry(f"{dw}x{dh}+{wx}+{wy}")

        # 自绘标题栏（关闭键在栏内垂直居中）
        tbar = tk.Frame(win, bg="#1e1e1e", height=int(46 * s))
        tbar.pack(fill="x")
        tbar.pack_propagate(False)
        tk.Label(tbar, text="导出", bg="#1e1e1e", fg=FG, font=FONT_B).pack(
            side="left", padx=int(16 * s))
        close = tk.Label(tbar, text="✕", bg="#1e1e1e", fg="#cfcfd6",
                         font=(UI_FAMILY, 10), padx=int(16 * s),
                         cursor="hand2")
        close.pack(side="right", fill="y")
        close.bind("<Button-1>", lambda e: win.destroy())
        close.bind("<Enter>", lambda e: close.configure(bg="#c42b1c", fg="#ffffff"))
        close.bind("<Leave>", lambda e: close.configure(bg="#1e1e1e", fg="#cfcfd6"))

        def _ds(e):
            win._off = (e.x_root - win.winfo_x(), e.y_root - win.winfo_y())
        def _dm(e):
            off = getattr(win, "_off", None)
            if off:
                win.geometry(f"+{e.x_root - off[0]}+{e.y_root - off[1]}")
        tbar.bind("<ButtonPress-1>", _ds)
        tbar.bind("<B1-Motion>", _dm)
        win.bind("<Escape>", lambda e: win.destroy())

        body = tk.Frame(win, bg="#1e1e1e")
        body.pack(fill="both", expand=True, padx=int(16 * s))

        # 左：封面（异步渲染，避免打开面板时卡住）
        left = tk.Frame(body, bg="#1e1e1e")
        left.pack(side="left", fill="y")
        cover = tk.Canvas(left, bg="#000000", width=int(300 * s), height=int(170 * s),
                          highlightthickness=0)
        cover.pack()
        self._render_cover_async(cover)
        tk.Label(left, text="用当前画面作封面", bg="#1e1e1e", fg=DIM,
                 font=FONT_XS).pack(anchor="w", pady=(int(8 * s), 0))

        # 右：参数
        right = tk.Frame(body, bg="#1e1e1e")
        right.pack(side="left", fill="both", expand=True, padx=(int(20 * s), 0))

        def field(label, widget):
            row = tk.Frame(right, bg="#1e1e1e")
            row.pack(fill="x", pady=int(5 * s))
            tk.Label(row, text=label, bg="#1e1e1e", fg=DIM, font=FONT_SM,
                     width=9, anchor="w").pack(side="left")
            widget(row)

        field("导出时间线", lambda r: tk.Label(r, text="时间线01", bg="#1e1e1e", fg=FG,
                                             font=FONT_SM).pack(side="left"))

        v_title = tk.StringVar(value="未命名项目")
        field("标题", lambda r: tk.Entry(r, textvariable=v_title, bg="#2b2b2b", fg=FG,
                                        relief="flat", font=FONT_SM,
                                        highlightthickness=0).pack(
            side="left", fill="x", expand=True, ipady=int(4 * s)))

        v_out = tk.StringVar(value=os.path.join(
            os.path.expanduser("~"), "Videos", "我的剪辑.mp4"))
        row = tk.Frame(right, bg="#1e1e1e")
        row.pack(fill="x", pady=int(5 * s))
        tk.Label(row, text="导出至", bg="#1e1e1e", fg=DIM, font=FONT_SM, width=9,
                 anchor="w").pack(side="left")
        tk.Entry(row, textvariable=v_out, bg="#2b2b2b", fg=FG, relief="flat",
                 font=FONT_XS, highlightthickness=0).pack(
            side="left", fill="x", expand=True, ipady=int(4 * s))

        def browse():
            p = filedialog.asksaveasfilename(title="导出至", defaultextension=".mp4",
                                             initialfile="我的剪辑.mp4",
                                             filetypes=[("MP4 视频", "*.mp4")])
            if p:
                v_out.set(p)
        ttk.Button(row, text="…", width=3, command=browse).pack(side="left", padx=(4, 0))

        tk.Frame(right, bg="#2e2e2e", height=1).pack(fill="x", pady=int(10 * s))

        v_res = tk.StringVar(value=self.var_res.get())
        v_q = tk.StringVar(value="对齐源码率")      # 默认对齐源码率
        field("分辨率", lambda r: ttk.Combobox(
            r, textvariable=v_res, values=["原始"] + RES_CHOICES[1:], width=14,
            state="readonly", font=FONT_SM).pack(side="left"))
        field("格式", lambda r: tk.Label(r, text="MP4", bg="#1e1e1e", fg=FG,
                                        font=FONT_SM).pack(side="left"))
        field("帧率", lambda r: tk.Label(r, text=f"{self.project.fps:g}", bg="#1e1e1e",
                                        fg=FG, font=FONT_SM).pack(side="left"))
        field("视频码率", lambda r: ttk.Combobox(
            r, textvariable=v_q, values=QUALITY_CHOICES, width=14, state="readonly",
            font=FONT_SM).pack(side="left"))

        tk.Frame(right, bg="#2e2e2e", height=1).pack(fill="x", pady=int(12 * s))

        # 单片段无特效时给「无损复制」，正好对上最初那个需求
        simple = (len(self.project.clips) == 1 and self.project.clips[0].kind == "video"
                  and not self.project.clips[0].has_effects())
        v_lossless = tk.BooleanVar(value=simple)
        if simple:
            ttk.Checkbutton(
                right, variable=v_lossless,
                text="无损复制（-c copy：不重新编码，画质和码率完全不变）").pack(
                anchor="w", pady=(0, int(6 * s)))

        # 大小估计随码率设置变化
        dur = self.project.total_duration
        src_bit = 0
        for c in self.project.clips:
            if c.kind == "video":
                try:
                    src_bit = max(src_bit, core.probe(c.path).vbitrate)
                except Exception:
                    pass
        src_bit = src_bit or 6_000_000

        def est_bytes(qname):
            if qname == "对齐源码率":
                return src_bit * dur / 8
            f = {"高画质": 0.105, "标准": 0.07, "小体积": 0.042}.get(qname, 0.07)
            return (self.project.width * self.project.height * self.project.fps
                    * f * dur / 8)

        est_lbl = tk.Label(right, text="", bg="#1e1e1e", fg=DIM, font=FONT_XS)
        est_lbl.pack(anchor="w", pady=(int(10 * s), 0))

        def update_est(*_):
            text = (f"时长：{core.fmt_clock(dur)}　"
                    f"大小：约 {core.human_size(max(1, est_bytes(v_q.get())))}（估计）")
            if v_lossless.get() and simple:
                text += f"　源文件约 {core.human_size(est_bytes('对齐源码率'))}"
            est_lbl.configure(text=text)
        v_q.trace_add("write", lambda *a: update_est())
        v_lossless.trace_add("write", lambda *a: update_est())
        update_est()

        foot = tk.Frame(win, bg="#1e1e1e")
        foot.pack(fill="x", padx=int(16 * s), pady=int(12 * s))
        ttk.Button(foot, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="导出", style="Accent.TButton",
                   command=lambda: (win.destroy(),
                                    self._do_export(v_out.get(), v_res.get(),
                                                    v_q.get(),
                                                    v_lossless.get()))).pack(
            side="right", padx=(0, int(10 * s)))
        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass
        try:
            win.grab_set()
        except Exception:
            pass

    def _render_cover_async(self, canvas):
        """封面单独渲染，绝不在打开面板时同步等 ffmpeg"""
        if not self.project.clips:
            return
        i, local = self.project.locate(self.playhead)
        if i < 0:
            i, local = 0, 0.0
        png = os.path.join(self.tmp, f"cover_{int(time.time() * 1000)}.png")
        clip = self.project.clips[i]

        def work():
            try:
                core.snapshot(self.project, clip, local, png, max_width=int(300 * self.s))
                self._q.put(("cover", (canvas, png)))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _do_export(self, out, res_choice, quality_name, lossless=False):
        if not out:
            messagebox.showinfo("提示", "请选择导出位置")
            return
        self.var_quality.set(quality_name)
        if res_choice in RES_MAP and not lossless:
            self.project.width, self.project.height = RES_MAP[res_choice]
            self._sync_draft()
        self._busy = True
        self.pb.configure(value=0)
        self.var_status.set("正在导出…")
        quality = self._quality()
        project = self.project

        def work():
            try:
                if lossless:
                    c = project.clips[0]
                    r = core.lossless_trim(c.path, out, c.start, c.start + c.duration)
                    r["lossless"] = True
                else:
                    r = core.export_project(
                        project, out, quality,
                        on_progress=lambda v: self._q.put(("progress", v * 100)))
                    r["lossless"] = False
                self._q.put(("export_done", r))
            except Exception as e:
                self._q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ==================================================== 其它
    def _on_res_change(self):
        choice = self.var_res.get()
        if choice in RES_MAP:
            self.project.width, self.project.height = RES_MAP[choice]
        else:
            self.project.fit_first()
        self._sync_draft()
        self._invalidate_proxy()
        self._schedule_preview(60)

    def _sync_draft(self):
        """没选素材就留空；选了素材就按素材的真实参数填"""
        vals = getattr(self, "_draft_vals", None)
        if not vals:
            return
        m = None
        idx = getattr(self, "media_sel", -1)
        if 0 <= idx < len(self.media):
            m = self.media[idx]
        if m is None:
            for lbl in vals.values():
                lbl.configure(text="")
            return

        info = m.get("info")
        path = m["path"]
        name = os.path.splitext(os.path.basename(path))[0]
        folder = os.path.dirname(path)
        if len(folder) > 38:
            folder = folder[:14] + "…" + folder[-22:]
        w = getattr(info, "width", 0) or 0
        h = getattr(info, "height", 0) or 0
        fps = getattr(info, "fps", 0) or 0
        pix = (getattr(info, "pix_fmt", "") or "")
        vcodec = getattr(info, "vcodec", "") or ""

        if w and h:
            g = math.gcd(w, h) or 1
            ratio = f"{w // g}:{h // g}"
            res = f"{w}×{h}"
        else:
            ratio, res = "", ""
        if "10" in pix:
            cs = "10bit"
        elif pix:
            cs = "Rec.709 SDR"
        else:
            cs = ""
        if vcodec and m["kind"] == "video":
            cs = f"{vcodec.upper()} · {cs}" if cs else vcodec.upper()
        if m["kind"] == "image":
            res = f"{res}（图片）" if res else "图片"
            fps_txt = "—"
            cs = "图片"
        else:
            fps_txt = f"{fps:g} fps" if fps else "—"

        short = name if len(name) <= 20 else name[:19] + "…"
        vals["基础名称"].configure(text=short)
        vals["保存位置"].configure(text=folder)
        vals["色彩空间"].configure(text=cs)
        vals["导入方式"].configure(text="保留在原有位置")
        vals["代理模式"].configure(text="未开启")
        vals["时间线名称"].configure(text="时间线01")
        vals["比例"].configure(text=ratio)
        vals["分辨率"].configure(text=res)
        vals["帧率"].configure(text=fps_txt)

    def show_help(self):
        """快捷键说明：直接盖在软件窗口里，不另开系统窗口（没有白条、不会被挪到别的屏）"""
        old = getattr(self, "_help", None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.lift()
                    return
            except Exception:
                pass
        s = self.s
        wrap = tk.Frame(self.root, bg="#0b0b0d")
        wrap.place(x=0, y=0, relwidth=1, relheight=1)
        wrap.lift()

        panel = tk.Frame(wrap, bg="#1e1e1e", highlightthickness=1,
                         highlightbackground="#3a3a40")
        panel.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(panel, text="快捷键", bg="#1e1e1e", fg=FG, font=FONT_B).pack(
            anchor="w", padx=int(16 * s), pady=(int(12 * s), int(6 * s)))
        txt = tk.Text(panel, bg="#15151a", fg=FG, font=FONT_MONO, relief="flat",
                      padx=int(16 * s), pady=int(12 * s), width=62, height=24,
                      highlightthickness=0, borderwidth=0)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        txt.pack(padx=int(16 * s))
        ttk.Button(panel, text="关闭", command=wrap.destroy).pack(pady=int(12 * s))

        wrap.bind("<Button-1>", lambda e: wrap.destroy() if e.widget is wrap else None)
        wrap.bind("<Escape>", lambda e: wrap.destroy())
        wrap.focus_set()
        self._help = wrap

    # ==================================================== 循环
    def _drain(self):
        # 资源管理器拖进来的文件：在这里（Tk 上下文中）处理，避免在窗口过程里回调 Tk
        if self._drop_queue:
            paths = self._drop_queue[:]
            del self._drop_queue[:]
            self.root.after(1, lambda ps=paths: self._import_paths(ps))
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "frame_png":
                    self._show_png(*payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "progress":
                    self.pb.configure(value=payload)
                elif kind == "proxy_progress":
                    self.pb.configure(value=payload * 100)
                    el = time.time() - getattr(self, "_proxy_t0", time.time())
                    eta = (el / payload - el) if payload > 0.03 else 0
                    self.var_proxy.set(f"预览：生成中 {payload * 100:.0f}%"
                                       + (f"（约 {eta:.0f}s）" if eta > 1 else ""))
                elif kind == "cover":
                    cv, png = payload
                    try:
                        if cv.winfo_exists() and os.path.exists(png):
                            img = tk.PhotoImage(file=png)
                            cv.delete("all")
                            cv.create_image(cv.winfo_width() // 2,
                                            cv.winfo_height() // 2, image=img)
                            cv.image = img
                    except Exception:
                        pass
                elif kind == "proxy_stale":
                    self._on_proxy_stale()
                elif kind == "proxy_done":
                    self._on_proxy_done()
                elif kind == "proxy_error":
                    self._proxy_busy = False
                    self._pending_play = False
                    self.var_proxy.set("预览：失败")
                    self.var_status.set("预览代理失败：" + str(payload)[:120])
                elif kind == "play_end":
                    if self.playing:
                        self.pause()
                        self.playhead = self.total
                        self._after_playhead_move()
                elif kind == "freeze":
                    self._on_freeze(*payload)
                elif kind == "export_done":
                    self._busy = False
                    r = payload
                    self.pb.configure(value=100)
                    self.var_status.set(
                        f"导出完成：{core.human_size(r['size'])} · {core.fmt_clock(r['duration'])}")
                    if messagebox.askyesno("导出完成",
                                           f"已保存：\n{r['path']}\n\n打开文件夹？"):
                        try:
                            os.startfile(os.path.dirname(os.path.abspath(r["path"])))
                        except Exception:
                            pass
                elif kind == "error":
                    self._busy = False
                    self.pb.configure(value=0)
                    messagebox.showerror("失败", str(payload)[:1500])
        except queue.Empty:
            pass
        self.root.after(40, self._drain)

    def _tick_playhead(self):
        if self.playing:
            head, _, _, _ = self._tl_metrics()
            x = self._time_to_x(self.playhead)
            w = self.timeline.winfo_width() or 1000
            if x > w - 30:
                self.scroll_t = self.playhead - (w - head - 60) / self.pps
                self._clamp_scroll()
                self.redraw_timeline()
            else:
                self._draw_playhead()
            self._update_tc()
        self.root.after(80, self._tick_playhead)

    def _refresh_all(self):
        self.redraw_timeline()
        self._update_tc()
        if self.project.clips:
            self._select(0)
        else:
            self._draw_empty_hint()


def selftest(files):
    dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    out = os.path.join(tempfile.gettempdir(), "ffeditor_selftest.txt")
    lines = []
    try:
        app.open_files(files)
        t0 = time.time()
        while time.time() - t0 < 30:
            root.update()
            time.sleep(0.03)
            if not app._proxy_busy and not app._proxy_dirty:
                break
        lines.append(f"clips={len(app.project.clips)} total={app.total:.2f} "
                     f"proxy={os.path.exists(app._proxy_path)} pil={HAVE_PIL}")
        app.playhead = 0.0
        p0 = app.playhead
        app.play()
        t0 = time.time()
        while time.time() - t0 < 2.5:
            root.update()
            time.sleep(0.02)
        lines.append(f"playing={app.playing} frames={app.player.frames} "
                     f"advanced={app.playhead - p0:.2f}")
        app.pause()
        lines.append(f"paused_at={app.playhead:.2f}")
    except Exception as e:
        lines.append("ERROR " + repr(e))
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    root.destroy()
    return 0


def main():
    dpi_awareness()
    _install_crash_log()
    if "--selftest" in sys.argv:
        return selftest([os.path.abspath(a) for a in sys.argv[1:]
                         if os.path.exists(a) and a != "--selftest"])
    root = tk.Tk()
    try:
        if not core.tools_ok():
            if messagebox.askyesno(
                    "找不到 FFmpeg",
                    "需要 FFmpeg。\n\n可以放到本程序同目录，或加入 PATH；\n"
                    "也可以现在手动指定 ffmpeg.exe 所在文件夹。\n\n要现在指定吗？"):
                folder = filedialog.askdirectory(title="选择包含 ffmpeg.exe 的文件夹")
                if folder:
                    core.set_tool_dir(folder)
            if not core.tools_ok():
                messagebox.showerror("无法启动", "仍然找不到 ffmpeg / ffprobe。")
                root.destroy()
                return 1
    except Exception:
        pass
    app = App(root)
    files = [os.path.abspath(a) for a in sys.argv[1:] if os.path.exists(a)]
    if files:
        root.after(250, lambda: app.open_files(files))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
