#!/usr/bin/env python3
"""
字幕延迟遮挡器 v1.0
Subtitle Delay Overlay

将红色边框的矩形框拖到视频字幕区域，它会自动遮挡字幕。
双击遮挡框可以手动查看字幕；在自动模式下，检测到字幕变化后延迟显示。

安装依赖:
    pip install Pillow mss

Windows 10 2004+ 可使用完整的自动延迟模式（截图穿透遮挡层）
其他系统：手动双击模式，马赛克模式
"""

import tkinter as tk
from tkinter import colorchooser
import threading
import time
import platform
import sys

PLATFORM = platform.system()
WINDOWS  = PLATFORM == 'Windows'

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import mss
    MSS_OK = True
except ImportError:
    MSS_OK = False

CAPTURE_OK = PIL_OK and MSS_OK


# ─── Color Palette ────────────────────────────────────────────────────────────
C = {
    'bg':       '#0e1117',
    'panel':    '#161b27',
    'surface':  '#1c2333',
    'border':   '#2d3748',
    'accent':   '#4f9eff',
    'red':      '#ff4757',
    'green':    '#2ed573',
    'orange':   '#ffa502',
    'text':     '#c8d3e0',
    'muted':    '#6b7a90',
    'overlay':  '#000000',
}


class SubtitleDelayer:
    """主应用类"""

    # 遮挡状态
    STATE_BLOCKING  = 'blocking'   # 正在遮挡
    STATE_PEEKING   = 'peeking'    # 正在显示（手动或自动触发）

    def __init__(self):
        self.root = tk.Tk()
        self.running = True

        # ── 用户配置 ──
        self.delay      = 2.0    # 自动模式延迟秒数
        self.peek_dur   = 4.0    # 显示字幕的持续秒数
        self.block_color = C['overlay']
        self.wda_ok     = False  # Windows 截图排除 API 是否可用

        # ── 运行状态 ──
        self.state       = self.STATE_BLOCKING
        self.peek_start  = 0.0
        self.peek_frame  = None      # 当前显示的帧（PIL.Image）
        self.frames      = []        # [(timestamp, PIL.Image), ...]
        self.last_thumb  = None      # 上一帧缩略图像素列表，用于变化检测
        self.reveal_at   = None      # 自动模式：计划显示的时间点
        self.current_photo = None    # tkinter PhotoImage 引用（防止GC）

        # ── 拖动/缩放临时数据 ──
        self._drag = {}
        self._resz = {}

        # ── 构建界面 ──
        self.build_overlay()
        self.build_panel()

        # ── Windows 截图排除 ──
        if WINDOWS and CAPTURE_OK:
            self.wda_ok = self._enable_wda()
            self._update_wda_badge()

        # ── 启动捕获线程 ──
        if CAPTURE_OK:
            threading.Thread(target=self._capture_loop, daemon=True).start()

        # ── 主循环 ──
        self._tick()
        self.root.mainloop()

    # ══════════════════════════════════════════════════════════════════════════
    # UI 构建
    # ══════════════════════════════════════════════════════════════════════════

    def build_overlay(self):
        """构建始终置顶的遮挡窗口"""
        r = self.root
        r.overrideredirect(True)
        r.attributes('-topmost', True)
        r.attributes('-alpha', 0.92)
        r.geometry('900x105+150+700')
        r.configure(bg=C['red'])

        # 红色边框外框
        self.outer = tk.Frame(r, bg=C['red'], padx=2, pady=2)
        self.outer.pack(fill='both', expand=True)

        # 主画布（显示遮挡色 / 截图 / 马赛克）
        self.cv = tk.Canvas(self.outer, bg=self.block_color,
                             highlightthickness=0, cursor='fleur')
        self.cv.pack(fill='both', expand=True)

        # 状态文字
        self.status_id = self.cv.create_text(
            8, 6, anchor='nw', text='⏸  遮挡中',
            fill='#3a3a3a', font=('Consolas', 9)
        )

        # 右下角缩放手柄
        self.grip = tk.Label(r, text='◢', fg=C['red'], bg=self.block_color,
                              cursor='sizing', font=('Arial', 11))
        self.grip.place(relx=1.0, rely=1.0, anchor='se')

        # ── 绑定事件 ──
        for w in (self.cv, self.outer):
            w.bind('<Button-1>',       self._drag_start)
            w.bind('<B1-Motion>',      self._drag_move)
            w.bind('<ButtonRelease-1>',self._drag_end)

        self.grip.bind('<Button-1>',  self._resz_start)
        self.grip.bind('<B1-Motion>', self._resz_move)

        self.cv.bind('<Double-Button-1>', self.toggle_peek)
        self.cv.bind('<Button-3>',        self._ctx_menu)

    def build_panel(self):
        """构建控制面板窗口"""
        p = tk.Toplevel(self.root)
        p.title('字幕延迟器 · 控制面板')
        p.attributes('-topmost', True)
        p.geometry('285x465+10+10')
        p.configure(bg=C['bg'])
        p.resizable(False, False)
        p.protocol('WM_DELETE_WINDOW', self.quit)
        self.panel = p

        # ── 标题 ──
        hdr = tk.Frame(p, bg=C['panel'], pady=12)
        hdr.pack(fill='x')
        tk.Label(hdr, text='🎬', bg=C['panel'], fg=C['accent'],
                  font=('Arial', 18)).pack(side='left', padx=(16,6))
        tk.Label(hdr, text='字幕延迟器', bg=C['panel'], fg=C['text'],
                  font=('Arial', 13, 'bold')).pack(side='left')

        def sep():
            tk.Frame(p, bg=C['border'], height=1).pack(fill='x', padx=14, pady=5)

        # ── 模式选择 ──
        sep()
        self._section(p, '显示模式')
        self.mode_v = tk.StringVar(value='block')
        mf = tk.Frame(p, bg=C['bg'])
        mf.pack(padx=14, fill='x', pady=(2, 4))
        mode_defs = [
            ('遮挡+手动', 'block',  '双击查看字幕'),
            ('自动延迟',  'auto',   '检测字幕自动延迟'),
            ('马赛克',    'mosaic', '模糊显示内容'),
        ]
        for label, val, tip in mode_defs:
            row = tk.Frame(mf, bg=C['bg'])
            row.pack(fill='x', pady=1)
            rb = tk.Radiobutton(row, text=label, variable=self.mode_v, value=val,
                                 bg=C['bg'], fg=C['text'], selectcolor=C['surface'],
                                 activebackground=C['bg'], activeforeground=C['accent'],
                                 font=('Arial', 10),
                                 command=self._mode_changed)
            rb.pack(side='left')
            tk.Label(row, text=tip, bg=C['bg'], fg=C['muted'],
                      font=('Arial', 8)).pack(side='left', padx=4)

        # ── 参数滑条 ──
        sep()
        self._section(p, '参数调节')

        self.delay_v    = tk.DoubleVar(value=self.delay)
        self.peek_dur_v = tk.DoubleVar(value=self.peek_dur)
        self.alpha_v    = tk.DoubleVar(value=0.92)
        self.mosaic_v   = tk.IntVar(value=12)
        self.thresh_v   = tk.IntVar(value=10)

        self._slider(p, '⏱  延迟时间',  '秒', self.delay_v,    0,    10,   0.5,
                     lambda v: setattr(self, 'delay', float(v)))
        self._slider(p, '👁  显示时长',  '秒', self.peek_dur_v, 1,    15,   0.5,
                     lambda v: setattr(self, 'peek_dur', float(v)))
        self._slider(p, '🌫  遮挡透明度', '',  self.alpha_v,    0.05, 1.0,  0.05,
                     lambda v: self.root.attributes('-alpha', float(v)))
        self._slider(p, '🔲  马赛克强度', '',  self.mosaic_v,   2,    25,   1)
        self._slider(p, '📡  变化灵敏度', '',  self.thresh_v,   3,    30,   1)

        # ── 颜色 ──
        sep()
        self._section(p, '外观')
        tk.Button(p, text='🎨  选择遮挡颜色',
                   bg=C['surface'], fg=C['text'], relief='flat',
                   pady=5, cursor='hand2', font=('Arial', 10),
                   activebackground=C['border'], activeforeground=C['accent'],
                   command=self._pick_color).pack(padx=14, fill='x', pady=3)

        # ── Windows API 徽章 ──
        sep()
        self.wda_badge = tk.Label(p, text='', bg=C['bg'],
                                   fg=C['muted'], font=('Arial', 8))
        self.wda_badge.pack(padx=14, anchor='w')

        # ── 使用提示 ──
        tips = (
            '📌  将红框拖到视频字幕区域\n'
            '↔  右下角 ◢ 拖动调整大小\n'
            '🖱  双击遮挡框 → 立即查看/隐藏\n'
            '🖱  右键 → 更多选项\n'
        )
        tf = tk.Label(p, text=tips, bg=C['surface'], fg=C['muted'],
                       font=('Arial', 8), justify='left',
                       padx=10, pady=6, anchor='w')
        tf.pack(fill='x', padx=14, pady=6)

        # ── 安装提示 ──
        if not CAPTURE_OK:
            missing = []
            if not PIL_OK: missing.append('Pillow')
            if not MSS_OK: missing.append('mss')
            tk.Label(p,
                      text=f'⚠ 安装依赖以使用自动/马赛克模式：\npip install {" ".join(missing)}',
                      bg='#2a1a00', fg='#ffa502', font=('Arial', 8),
                      justify='left', padx=8, pady=6
                      ).pack(fill='x', padx=14, pady=2)

        # ── 底部状态 ──
        sep()
        self.stat_lbl = tk.Label(p, text='● 就绪', bg=C['bg'],
                                  fg=C['green'], font=('Arial', 9))
        self.stat_lbl.pack(pady=4)

    def _section(self, parent, title):
        tk.Label(parent, text=title.upper(), bg=C['bg'], fg=C['muted'],
                  font=('Arial', 8, 'bold'), anchor='w',
                  padx=14).pack(fill='x', pady=(4, 0))

    def _slider(self, parent, label, unit, var, lo, hi, res, cmd=None):
        f = tk.Frame(parent, bg=C['bg'])
        f.pack(fill='x', padx=14, pady=1)
        row = tk.Frame(f, bg=C['bg'])
        row.pack(fill='x')
        tk.Label(row, text=label, bg=C['bg'], fg=C['text'],
                  font=('Arial', 9), anchor='w').pack(side='left')
        vl = tk.Label(row, textvariable=var, bg=C['bg'], fg=C['accent'],
                       font=('Consolas', 9), width=4)
        vl.pack(side='right')
        if unit:
            tk.Label(row, text=unit, bg=C['bg'], fg=C['muted'],
                      font=('Arial', 8)).pack(side='right')
        tk.Scale(f, from_=lo, to=hi, resolution=res, orient='horizontal',
                  variable=var, bg=C['bg'], fg=C['text'],
                  highlightthickness=0, troughcolor=C['surface'],
                  activebackground=C['accent'], sliderrelief='flat',
                  showvalue=False, command=cmd).pack(fill='x')

    # ══════════════════════════════════════════════════════════════════════════
    # Windows API
    # ══════════════════════════════════════════════════════════════════════════

    def _enable_wda(self):
        """
        SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
        让本窗口对截图工具不可见，但用户仍能看到。
        需要 Windows 10 2004 (build 19041)+
        """
        try:
            import ctypes
            hwnd = self.root.winfo_id()
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            result = ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint32(WDA_EXCLUDEFROMCAPTURE)
            )
            return bool(result)
        except Exception:
            return False

    def _update_wda_badge(self):
        if self.wda_ok:
            self.wda_badge.config(
                text='✓ 截图穿透已启用（自动模式完整功能）',
                fg=C['green']
            )
        else:
            self.wda_badge.config(
                text='ℹ 截图穿透不可用（需要 Win10 2004+）',
                fg=C['muted']
            )

    # ══════════════════════════════════════════════════════════════════════════
    # 屏幕捕获线程
    # ══════════════════════════════════════════════════════════════════════════

    def _capture_loop(self):
        """后台线程：每 100ms 捕获遮挡区域下方的画面"""
        with mss.mss() as sct:
            while self.running:
                try:
                    x = self.root.winfo_x()
                    y = self.root.winfo_y()
                    w = self.root.winfo_width()
                    h = self.root.winfo_height()

                    if w < 10 or h < 10:
                        time.sleep(0.1)
                        continue

                    mon = {'top': y, 'left': x, 'width': w, 'height': h}
                    shot = sct.grab(mon)
                    img = Image.frombytes(
                        'RGB', shot.size, shot.bgra, 'raw', 'BGRX'
                    )
                    ts = time.time()

                    self.frames.append((ts, img))

                    # 保留最近 15 秒
                    cutoff = ts - 15.0
                    self.frames = [(t, f) for t, f in self.frames if t >= cutoff]

                    # 变化检测（仅自动模式 + WDA 可用时有意义）
                    if self.mode_v.get() == 'auto' and self.wda_ok:
                        self._detect_change(img)

                    time.sleep(0.1)
                except Exception:
                    time.sleep(0.1)

    def _detect_change(self, img: 'Image.Image'):
        """
        比较当前帧与上一帧缩略图，若变化超过阈值则触发延迟计时。
        使用小缩略图（60×15）以提高效率。
        """
        try:
            thumb = list(img.resize((60, 15)).convert('L').getdata())
            if self.last_thumb is not None:
                n = len(thumb)
                diff = sum(abs(a - b) for a, b in zip(thumb, self.last_thumb)) / n
                threshold = self.thresh_v.get()
                if diff > threshold and self.reveal_at is None \
                        and self.state == self.STATE_BLOCKING:
                    # 检测到变化，安排在 delay 秒后显示
                    self.reveal_at = time.time() + self.delay
            self.last_thumb = thumb
        except Exception:
            pass

    def _get_frame(self, at_time=None):
        """取最近时间点 <= at_time 的帧；at_time=None 取最新帧"""
        if not self.frames:
            return None
        if at_time is None:
            return self.frames[-1][1]
        result = self.frames[0][1]
        for ts, f in self.frames:
            if ts <= at_time:
                result = f
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # 主循环 (80ms Tick)
    # ══════════════════════════════════════════════════════════════════════════

    def _tick(self):
        if not self.running:
            return

        now  = time.time()
        mode = self.mode_v.get() if hasattr(self, 'mode_v') else 'block'

        # ── 自动模式：到时间了，触发显示 ──
        if mode == 'auto' and self.reveal_at and now >= self.reveal_at:
            reveal_ts = self.reveal_at - self.delay   # 检测时刻的截图
            frame = self._get_frame(reveal_ts)
            self.reveal_at = None
            self._start_peek(frame)

        # ── Peek 超时 ──
        if self.state == self.STATE_PEEKING:
            elapsed = now - self.peek_start
            if elapsed >= self.peek_dur:
                self._end_peek()

        # ── 渲染 ──
        if self.state == self.STATE_PEEKING:
            if self.peek_frame:
                self._render_frame(self.peek_frame)
            else:
                self._render_solid('#1a2a1a')   # 没有截图时显示深绿提示
            rem = self.peek_dur - (now - self.peek_start)
            self._set_status(f'👁  {rem:.1f}s 后恢复遮挡', bright=True)

        elif mode == 'mosaic' and CAPTURE_OK and self.wda_ok:
            frame = self._get_frame()
            if frame:
                self._render_mosaic(frame)
                self._set_status('🔲  马赛克')
            else:
                self._render_solid()
                self._set_status('🔲  等待捕获...')

        else:
            self._render_solid()
            if mode == 'auto' and self.reveal_at:
                rem = self.reveal_at - now
                self._set_status(f'⏳  {rem:.1f}s 后显示字幕')
            elif mode == 'auto':
                self._set_status('📡  监听字幕变化...')
            else:
                self._set_status('⏸  遮挡中  双击查看')

        self.root.after(80, self._tick)

    # ══════════════════════════════════════════════════════════════════════════
    # 显示 / 渲染
    # ══════════════════════════════════════════════════════════════════════════

    def _start_peek(self, frame=None):
        self.state      = self.STATE_PEEKING
        self.peek_start = time.time()
        self.peek_frame = frame

    def _end_peek(self):
        self.state      = self.STATE_BLOCKING
        self.peek_frame = None
        self._render_solid()

    def _render_solid(self, color=None):
        color = color or self.block_color
        self.cv.delete('img')
        self.cv.configure(bg=color)

    def _render_frame(self, img: 'Image.Image'):
        try:
            w = self.cv.winfo_width()
            h = self.cv.winfo_height()
            if w < 2 or h < 2:
                return
            resized = img.resize((w, h), Image.LANCZOS)
            ph = ImageTk.PhotoImage(resized)
            self.cv.delete('img')
            self.cv.create_image(0, 0, anchor='nw', image=ph, tags='img')
            self.cv.tag_raise(self.status_id)
            self.current_photo = ph   # 防止被 GC
        except Exception:
            pass

    def _render_mosaic(self, img: 'Image.Image'):
        try:
            w = self.cv.winfo_width()
            h = self.cv.winfo_height()
            if w < 2 or h < 2:
                return
            ms = max(2, self.mosaic_v.get())
            sw = max(1, w // ms)
            sh = max(1, h // ms)
            mosaic = img.resize((sw, sh), Image.NEAREST).resize((w, h), Image.NEAREST)
            ph = ImageTk.PhotoImage(mosaic)
            self.cv.delete('img')
            self.cv.create_image(0, 0, anchor='nw', image=ph, tags='img')
            self.cv.tag_raise(self.status_id)
            self.current_photo = ph
        except Exception:
            pass

    def _set_status(self, text: str, bright=False):
        try:
            color = '#cccccc' if bright else '#3a3a3a'
            self.cv.itemconfig(self.status_id, text=text, fill=color)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # 用户交互
    # ══════════════════════════════════════════════════════════════════════════

    def toggle_peek(self, event=None):
        """双击切换显示/遮挡"""
        if self.state == self.STATE_PEEKING:
            self._end_peek()
        else:
            frame = self._get_frame() if (CAPTURE_OK and self.wda_ok and self.frames) else None
            self._start_peek(frame)

    def _mode_changed(self):
        self.state     = self.STATE_BLOCKING
        self.reveal_at = None
        self.last_thumb = None
        self._end_peek()

    def _pick_color(self):
        result = colorchooser.askcolor(
            color=self.block_color, title='选择遮挡颜色', parent=self.panel
        )
        if result and result[1]:
            self.block_color = result[1]
            self.grip.configure(bg=self.block_color)
            if self.state == self.STATE_BLOCKING:
                self.cv.configure(bg=self.block_color)

    def _ctx_menu(self, event):
        m = tk.Menu(self.root, tearoff=0, bg=C['surface'], fg=C['text'],
                     activebackground=C['accent'], activeforeground='#fff')
        m.add_command(label='👁  查看/隐藏字幕  (双击)',  command=self.toggle_peek)
        m.add_command(label='⚙  控制面板',               command=self.panel.lift)
        m.add_separator()
        m.add_command(label='✕  退出',                    command=self.quit)
        m.post(event.x_root, event.y_root)

    # ══════════════════════════════════════════════════════════════════════════
    # 拖动 & 缩放
    # ══════════════════════════════════════════════════════════════════════════

    def _drag_start(self, e):
        self._drag = {'x': e.x_root - self.root.winfo_x(),
                       'y': e.y_root - self.root.winfo_y()}

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(
                f'+{e.x_root - self._drag["x"]}+{e.y_root - self._drag["y"]}'
            )

    def _drag_end(self, e):
        self._drag = {}

    def _resz_start(self, e):
        self._resz = {
            'x': e.x_root, 'y': e.y_root,
            'w': self.root.winfo_width(),
            'h': self.root.winfo_height()
        }

    def _resz_move(self, e):
        if self._resz:
            w = max(150, self._resz['w'] + e.x_root - self._resz['x'])
            h = max(40,  self._resz['h'] + e.y_root - self._resz['y'])
            self.root.geometry(f'{w}x{h}')

    # ══════════════════════════════════════════════════════════════════════════
    # 退出
    # ══════════════════════════════════════════════════════════════════════════

    def quit(self):
        self.running = False
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────

def main():
    missing = []
    if not PIL_OK:
        missing.append('Pillow')
    if not MSS_OK:
        missing.append('mss')

    if missing:
        print('=' * 50)
        print('字幕延迟器 · 依赖提示')
        print('=' * 50)
        print(f'缺少依赖: {", ".join(missing)}')
        print(f'运行以下命令安装：')
        print(f'  pip install {" ".join(missing)}')
        print()
        print('基础模式（遮挡+手动双击）无需任何额外依赖，直接运行即可。')
        print('自动延迟和马赛克模式需要以上依赖。')
        print('=' * 50)
        print()
        # 仍然启动基础模式

    SubtitleDelayer()


if __name__ == '__main__':
    main()
