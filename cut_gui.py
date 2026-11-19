#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cut_gui.py —— 一个极简的 FFmpeg 剪辑器：无损剪切，不改变码率

  · 拖时间轴预览画面，设入点 / 出点
  · 默认「无损复制」：-c copy，画质和码率 1:1 不变，导出只要零点几秒
  · 需要精确到帧时切到「重编码」，输出码率自动对齐源视频，体积不会莫名膨胀

依赖：Python 3.8+ 自带 tkinter，以及 PATH 中的 ffmpeg / ffprobe
运行：python cut_gui.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lossless_cut as lc

BG = "#1b1b1f"
FG = "#e8e8ea"
DIM = "#8b8b93"
SEL = "#2b4a7a"

FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 10)


def _enable_dpi_awareness():
    """让窗口按真实像素渲染，避免 125%/150% 缩放下被系统拉伸变糊"""
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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("FFmpeg 无损剪切 · 不改变码率")

        # 高 DPI 屏幕：按显示器缩放比例放大窗口与像素尺寸，避免字体被系统拉糊
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.tk.call("tk", "scaling", 96.0 * self.s / 72.0)
        s = self.s
        root.geometry(f"{int(900 * s)}x{int(810 * s)}")
        root.minsize(int(840 * s), int(760 * s))
        root.configure(bg=BG)

        self.path: str | None = None
        self.info: lc.MediaInfo | None = None
        self.kfs: list[float] = []
        self.pos = 0.0

        self.var_file = tk.StringVar()
        self.inp = tk.StringVar(value="0:00:00.000")
        self.outp = tk.StringVar(value="0:00:00.000")
        self.mode = tk.StringVar(value="copy")
        self.outfile = tk.StringVar()
        self.status = tk.StringVar(value="选择一个视频文件开始")

        self._img = None
        self._preview_job = None
        self._preview_gen = 0
        self._busy = False
        self._q: queue.Queue = queue.Queue()
        self._tmp_png = os.path.join(tempfile.gettempdir(), f"ffcut_preview_{os.getpid()}.png")

        self._build_ui()
        self._bind_keys()
        self.root.after(60, self._drain_queue)

    # ------------------------------------------------------------ 界面
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=FONT)
        style.configure("Dim.TLabel", background=BG, foreground=DIM, font=FONT_SM)
        style.configure("TButton", font=FONT_SM, padding=(10, 5))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
        style.configure("TRadiobutton", background=BG, foreground=FG, font=FONT_SM)
        style.map("TRadiobutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("TEntry", fieldbackground="#2a2a30", foreground=FG, insertcolor=FG)

        s = self.s
        outer = ttk.Frame(self.root, padding=int(14 * s))
        outer.pack(fill="both", expand=True)

        row = ttk.Frame(outer)
        row.pack(fill="x")
        ttk.Label(row, text="文件", width=4).pack(side="left")
        e = ttk.Entry(row, textvariable=self.var_file, state="readonly", font=FONT_SM)
        e.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="打开…", command=self.pick_file).pack(side="left")

        self.lbl_info = ttk.Label(outer, text="", style="Dim.TLabel", justify="left")
        self.lbl_info.pack(fill="x", pady=(6, 8))

        self.canvas = tk.Canvas(outer, width=int(860 * s), height=int(300 * s),
                                bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="x")

        self.tl = tk.Canvas(outer, height=int(54 * s), bg="#111114", highlightthickness=0,
                            cursor="hand2")
        self.tl.pack(fill="x", pady=(8, 2))
        self.tl.bind("<Configure>", lambda e: self.draw_timeline())
        self.tl.bind("<Button-1>", self._on_seek)
        self.tl.bind("<B1-Motion>", self._on_seek)

        row = ttk.Frame(outer)
        row.pack(fill="x")
        ttk.Label(row, text="位置", style="Dim.TLabel").pack(side="left")
        self.lbl_pos = ttk.Label(row, text="0:00:00.000", font=FONT_MONO)
        self.lbl_pos.pack(side="left", padx=8)

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="入点", width=4).pack(side="left")
        ttk.Entry(row, textvariable=self.inp, width=14, font=FONT_MONO).pack(side="left", padx=6)
        ttk.Button(row, text="设为入点", command=self.set_in).pack(side="left")
        ttk.Button(row, text="跳到", command=lambda: self._seek_var(self.inp)).pack(side="left", padx=4)

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="出点", width=4).pack(side="left")
        ttk.Entry(row, textvariable=self.outp, width=14, font=FONT_MONO).pack(side="left", padx=6)
        ttk.Button(row, text="设为出点", command=self.set_out).pack(side="left")
        ttk.Button(row, text="跳到", command=lambda: self._seek_var(self.outp)).pack(side="left", padx=4)

        box = ttk.Frame(outer)
        box.pack(fill="x", pady=(12, 0))
        ttk.Label(box, text="模式").pack(side="left")
        ttk.Radiobutton(box, text="无损复制（-c copy，码率不变，切点吸附关键帧）",
                        variable=self.mode, value="copy",
                        command=self._update_estimate).pack(side="left", padx=(10, 14))
        ttk.Radiobutton(box, text="精确到帧（重编码，码率对齐源视频）",
                        variable=self.mode, value="accurate",
                        command=self._update_estimate).pack(side="left")

        self.lbl_note = ttk.Label(outer, text="", style="Dim.TLabel",
                                  wraplength=int(860 * s), justify="left")
        self.lbl_note.pack(fill="x", pady=(6, 0))

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="输出", width=4).pack(side="left")
        ttk.Entry(row, textvariable=self.outfile, font=FONT_SM).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="另存…", command=self.pick_output).pack(side="left")

        self.lbl_est = ttk.Label(outer, text="", style="Dim.TLabel")
        self.lbl_est.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(12, 0))
        self.btn_export = ttk.Button(row, text="开始导出", style="Accent.TButton",
                                     command=self.export)
        self.btn_export.pack(side="left")
        self.pb = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=12)

        ttk.Label(outer, textvariable=self.status, style="Dim.TLabel",
                  wraplength=int(860 * s), justify="left").pack(fill="x", pady=(8, 0))

        for var in (self.inp, self.outp):
            var.trace_add("write", lambda *a: self._update_estimate())

    def _bind_keys(self):
        self.root.bind("<Left>", lambda e: self.seek(self.pos - (5 if e.state & 1 else 1)))
        self.root.bind("<Right>", lambda e: self.seek(self.pos + (5 if e.state & 1 else 1)))
        self.root.bind("<i>", lambda e: self.set_in())
        self.root.bind("<o>", lambda e: self.set_out())
        self.root.bind("<space>", lambda e: self.seek(self.pos))

    # ------------------------------------------------------------ 文件
    def pick_file(self):
        p = filedialog.askopenfilename(
            title="选择视频",
            filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.avi *.ts *.m2ts *.webm *.flv *.wmv *.m4v"),
                       ("所有文件", "*.*")])
        if p:
            self.load(p)

    def load(self, path: str):
        if self._busy:
            return
        try:
            info = lc.probe(path)
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
            return
        self.path = path
        self.info = info
        self.kfs = []
        self.var_file.set(path)
        self.lbl_info.configure(text=info.summary())
        self.inp.set("0:00:00.000")
        self.outp.set(lc.fmt_clock(info.duration))
        stem, ext = os.path.splitext(path)
        self.outfile.set(f"{stem}_cut{ext if ext.lower() in ('.mp4', '.mkv', '.mov') else '.mp4'}")
        self.status.set("正在读取关键帧…")
        self.seek(0.0)
        self.root.after(100, self._resize_preview)
        threading.Thread(target=self._load_kfs, args=(path,), daemon=True).start()

    def _resize_preview(self):
        if self.info:
            self._schedule_preview(0)

    def _load_kfs(self, path):
        try:
            kfs = lc.keyframes(path)
        except Exception as e:
            kfs = []
            self._q.put(("status", f"关键帧读取失败：{e}"))
        self._q.put(("kfs", (path, kfs)))

    def pick_output(self):
        p = filedialog.asksaveasfilename(
            title="输出文件", defaultextension=".mp4",
            initialfile=os.path.basename(self.outfile.get() or "output.mp4"),
            filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("MOV", "*.mov")])
        if p:
            self.outfile.set(p)

    # ------------------------------------------------------------ 预览 / 时间轴
    def seek(self, t):
        if not self.info:
            return
        self.pos = max(0.0, min(float(t), self.info.duration))
        self.lbl_pos.configure(text=lc.fmt_clock(self.pos))
        self.draw_timeline()
        self._schedule_preview()

    def _seek_var(self, var):
        try:
            self.seek(lc.parse_time(var.get()))
        except Exception as e:
            self.status.set(f"时间格式错误：{e}")

    def _on_seek(self, event):
        if not self.info:
            return
        w = max(1, self.tl.winfo_width())
        self.seek(max(0, min(event.x, w)) / w * self.info.duration)

    def _schedule_preview(self, delay=120):
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(delay, self._render_preview)

    def _render_preview(self):
        self._preview_job = None
        if not self.path or not self.info:
            return
        self._preview_gen += 1
        cw = self.canvas.winfo_width() or 880
        ch = self.canvas.winfo_height() or 320
        threading.Thread(target=self._extract_frame,
                         args=(self.path, self.pos, self._preview_gen, cw, ch),
                         daemon=True).start()

    def _extract_frame(self, path, t, gen, cw, ch):
        pre = max(0.0, t - 5.0)
        post = t - pre
        vf = f"scale={cw}:{ch}:force_original_aspect_ratio=decrease"
        cmd = [lc._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-ss", lc.fmt_time(pre), "-i", path,
               "-ss", f"{post:.3f}", "-frames:v", "1",
               "-vf", vf, "-f", "image2", self._tmp_png]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=lc.CREATE_NO_WINDOW)
        except Exception:
            return
        self._q.put(("frame", (gen, self._tmp_png)))

    def _show_frame(self, gen, png):
        if gen != self._preview_gen or not os.path.exists(png):
            return
        try:
            img = tk.PhotoImage(file=png)
        except Exception:
            return
        self._img = img
        self.canvas.delete("all")
        cw = self.canvas.winfo_width() or 880
        ch = self.canvas.winfo_height() or 320
        self.canvas.create_image(cw // 2, ch // 2, image=img)

    def draw_timeline(self):
        c = self.tl
        if not c.winfo_exists():
            return
        w = c.winfo_width() or 880
        h = c.winfo_height() or 54
        c.delete("all")
        if not self.info or self.info.duration <= 0:
            c.create_text(w // 2, h // 2, text="时间轴（白色竖线为关键帧）",
                          fill=DIM, font=FONT_SM)
            return
        dur = self.info.duration
        c.create_rectangle(0, 0, w, h, fill="#111114", outline="")

        try:
            a = max(0.0, min(lc.parse_time(self.inp.get()), dur))
            b = max(0.0, min(lc.parse_time(self.outp.get()), dur))
        except Exception:
            a, b = 0.0, dur
        if self.mode.get() == "copy" and self.kfs and b > a:
            a = lc.snap_start(self.kfs, a)
        if b > a:
            c.create_rectangle(a / dur * w, 6, b / dur * w, h - 18, fill=SEL, outline="")

        for k in self.kfs:
            x = k / dur * w
            c.create_line(x, h - 16, x, h - 4, fill="#77777f", width=1)

        x = self.pos / dur * w
        c.create_line(x, 0, x, h - 2, fill="#ffcc00", width=2)
        c.create_polygon(x - 5, 0, x + 5, 0, x, 8, fill="#ffcc00", outline="")

        c.create_text(4, h - 2, anchor="sw", text=lc.fmt_clock(0), fill=DIM, font=FONT_SM)
        c.create_text(w - 4, h - 2, anchor="se", text=lc.fmt_clock(dur), fill=DIM, font=FONT_SM)

    # ------------------------------------------------------------ 入出点
    def set_in(self):
        self.inp.set(lc.fmt_clock(self.pos))
        self._update_estimate()

    def set_out(self):
        self.outp.set(lc.fmt_clock(self.pos))
        self._update_estimate()

    def _update_estimate(self):
        self.draw_timeline()
        if not self.info:
            return
        try:
            a = lc.parse_time(self.inp.get())
            b = lc.parse_time(self.outp.get())
        except Exception:
            self.lbl_est.configure(text="")
            return
        b = min(b, self.info.duration)
        dur = max(0.0, b - a)
        if self.mode.get() == "copy" and self.kfs:
            k = lc.snap_start(self.kfs, a)
            dur = max(0.0, b - k)
            if abs(k - a) > 0.02:
                self.lbl_note.configure(
                    text=f"无损模式：入点会吸附到最近的关键帧 {lc.fmt_clock(k)}"
                         f"（比 {lc.fmt_clock(a)} 早 {a - k:.3f}s，多出来的部分会一起导出）。"
                         f"必须从指定帧开始时请改用「精确到帧」。")
            else:
                self.lbl_note.configure(text="无损模式：入点正好落在关键帧上，画面与码率零损失。")
        else:
            self.lbl_note.configure(text="精确模式：从指定帧开始，仅一次重编码，码率对齐源视频。")
        size = self.info.total_bitrate * dur / 8 if self.info.total_bitrate else 0
        self.lbl_est.configure(
            text=f"新时长 {dur:.3f}s ｜ 预计体积 ≈ {lc.human_size(size)}"
                 f"（源文件 {lc.human_size(self.info.size)} / {lc.fmt_clock(self.info.duration)}）")

    # ------------------------------------------------------------ 导出
    def export(self):
        if self._busy:
            return
        if not self.path or not self.info:
            messagebox.showinfo("提示", "请先打开一个视频文件")
            return
        try:
            a = lc.parse_time(self.inp.get())
            b = min(lc.parse_time(self.outp.get()), self.info.duration)
        except Exception as e:
            messagebox.showerror("时间格式错误", str(e))
            return
        if b - a < 0.04:
            messagebox.showerror("提示", "出点必须晚于入点")
            return
        out = self.outfile.get().strip()
        if not out:
            messagebox.showerror("提示", "请填写输出文件")
            return
        if os.path.abspath(out) == os.path.abspath(self.path):
            messagebox.showerror("提示", "输出文件不能覆盖源文件")
            return

        accurate = self.mode.get() == "accurate"
        self._busy = True
        self.btn_export.configure(state="disabled")
        if accurate:
            self.pb.configure(mode="determinate", value=0)
        else:
            self.pb.configure(mode="indeterminate")
            self.pb.start(12)
        self.status.set("正在导出…")
        threading.Thread(target=self._export_worker,
                         args=(self.path, out, a, b, accurate), daemon=True).start()

    def _export_worker(self, src, out, a, b, accurate):
        try:
            if accurate:
                res = lc.cut_accurate(src, out, a, b, info=self.info,
                                      on_progress=lambda p: self._q.put(("progress", p * 100)))
            else:
                res = lc.cut_copy(src, out, a, b, info=self.info)
            self._q.put(("done", (out, res)))
        except Exception as e:
            self._q.put(("error", str(e)))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "frame":
                    self._show_frame(*payload)
                elif kind == "kfs":
                    path, kfs = payload
                    if path == self.path:
                        self.kfs = kfs
                        self.status.set(f"就绪 · 共 {len(kfs)} 个关键帧 · 空格/←→ 预览，I/O 设入出点")
                        self._update_estimate()
                elif kind == "progress":
                    if str(self.pb.cget("mode")) == "determinate":
                        self.pb.configure(value=payload)
                elif kind == "done":
                    out, res = payload
                    self._finish()
                    self.status.set(
                        f"完成：{os.path.basename(out)} ｜ 实际 {lc.fmt_clock(res['start'])} → "
                        f"{lc.fmt_clock(res['start'] + res['duration'])} ｜ "
                        f"{lc.human_size(res['size'])} ｜ {res['bitrate'] / 1000:.0f} kbps")
                    self.pb.configure(value=100)
                elif kind == "error":
                    self._finish()
                    self.status.set("导出失败")
                    messagebox.showerror("导出失败", payload[:1500])
                elif kind == "status":
                    self.status.set(payload)
        except queue.Empty:
            pass
        self.root.after(60, self._drain_queue)

    def _finish(self):
        self._busy = False
        self.btn_export.configure(state="normal")
        try:
            self.pb.stop()
            self.pb.configure(mode="determinate", value=0)
        except tk.TclError:
            pass


def main():
    _enable_dpi_awareness()
    try:
        lc._ffmpeg()
        lc._ffprobe()
    except FileNotFoundError as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("缺少 FFmpeg", str(e))
        return 1
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        root.after(150, lambda: app.load(os.path.abspath(sys.argv[1])))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
