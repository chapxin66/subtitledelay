#!/usr/bin/env python3
"""
字幕延迟器 v2.0
===============
核心原理：遮挡框里永远显示「N秒前」的画面。
  - 字幕在 T 秒出现 → 遮挡框在 T+N 秒才显示它
  - 全程自动，无需点击，无需任何触发
  - 下一条字幕出现时同样自动延迟

需要安装:
    pip install Pillow mss

Windows 10 2004+ 专属功能:
    程序利用 SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
    让遮挡框对截图不可见，从而能捕获遮挡框下方的真实画面。
"""

import tkinter as tk
from tkinter import colorchooser
from collections import deque
import threading
import time
import platform

WINDOWS = platform.system() == 'Windows'

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

C = {
    'bg':      '#0d1117',
    'panel':   '#161b22',
    'surface': '#21262d',
    'border':  '#30363d',
    'accent':  '#58a6ff',
    'red':     '#ff4757',
    'green':   '#3fb950',
    'yellow':  '#d29922',
    'text':    '#c9d1d9',
    'muted':   '#6e7681',
}


class FrameBuffer:
    """线程安全的帧缓冲区"""

    def __init__(self, max_seconds=20.0):
        self._buf = deque()
        self._lock = threading.Lock()
        self._max = max_seconds

    def push(self, img, ts):
        with self._lock:
            self._buf.append((ts, img))
            cutoff = ts - self._max
            while self._buf and self._buf[0][0] < cutoff:
                self._buf.popleft()

    def get_at(self, target_ts):
        """返回最接近且 <= target_ts 的帧"""
        with self._lock:
            result = None
            for ts, img in self._buf:
                if ts <= target_ts:
                    result = img
                else:
                    break
            return result

    def latest(self):
        with self._lock:
            return self._buf[-1][1] if self._buf else None

    def oldest_ts(self):
        with self._lock:
            return self._buf[0][0] if self._buf else None

    def __len__(self):
        with self._lock:
            return len(self._buf)


class SubtitleDelayer:

    def __init__(self):
        # 隐藏根窗口，避免多余空白窗口
        self._root = tk.Tk()
        self._root.withdraw()

        self.running = True
        self.wda_ok = False
        self.buf = FrameBuffer()
        self.photo = None

        self.delay = 2.0
        self.block_color = '#000000'
        self._manual_reveal_until = 0.0  # 手动查看的截止时间
        self._ov_rect = (0, 0, 0, 0)
        self._tick_n  = 0           # 计数，控制截图频率
        self._sct     = mss.mss() if CAPTURE_OK else None

        self._build_overlay()
        self._build_panel()

        # 必须在窗口显示后才能拿到有效 HWND
        self._root.update()
        if WINDOWS and CAPTURE_OK:
            self.wda_ok = self._enable_wda()

        self._refresh_badge()

        self._tick()
        self._root.mainloop()

    # ─── 遮挡窗口 ─────────────────────────────────────────────────────────────

    def _build_overlay(self):
        ov = tk.Toplevel(self._root)
        ov.overrideredirect(True)
        ov.attributes('-topmost', True)
        ov.attributes('-alpha', 1.0)
        ov.geometry('880x100+20+860')
        ov.configure(bg=C['red'])
        self.ov = ov

        # 快门窗口：截图期间顶在最上方挡住字幕，防止频闪露出真实画面
        sh = tk.Toplevel(self._root)
        sh.overrideredirect(True)
        sh.attributes('-topmost', True)
        sh.attributes('-alpha', 0.0)   # 平时隐藏
        sh.geometry('880x100+20+860')
        sh.configure(bg='#000000')
        self._shutter = sh

        # self._inner = tk.Frame(ov, bg=C['red'], padx=2, pady=2)
        self._inner = tk.Frame(ov, bg=C['red'], padx=0, pady=0)
        self._inner.pack(fill='both', expand=True)

        self.cv = tk.Canvas(self._inner, bg=self.block_color,
                             highlightthickness=0, cursor='fleur')
        self.cv.pack(fill='both', expand=True)

        self._sid_bg = self.cv.create_rectangle(
            4, 3, 300, 18, fill='#000000', outline='', stipple='gray50'
        )
        self._sid = self.cv.create_text(
            7, 5, anchor='nw', text='⏳', fill='#ffffff', font=('Consolas', 9, 'bold')
        )

        self._grip = tk.Label(ov, text='◢', fg=C['red'], bg=self.block_color,
                               cursor='sizing', font=('Arial', 10))
        self._grip.place(relx=1.0, rely=1.0, anchor='se')

        self._dd = {}
        for w in (self.cv, self._inner):
            w.bind('<Button-1>',        self._d0)
            w.bind('<B1-Motion>',       self._d1)
            w.bind('<ButtonRelease-1>', self._d2)

        self._rd = {}
        self._grip.bind('<Button-1>',  self._r0)
        self._grip.bind('<B1-Motion>', self._r1)
        self.cv.bind('<Button-3>', self._ctx)

    # ─── 控制面板 ─────────────────────────────────────────────────────────────

    def _build_panel(self):
        p = tk.Toplevel(self._root)
        p.title('字幕延迟器')
        p.attributes('-topmost', True)
        p.geometry('300x600+20+20')
        p.configure(bg=C['bg'])
        p.resizable(False, False)
        p.protocol('WM_DELETE_WINDOW', self.quit)
        self.panel = p

        hdr = tk.Frame(p, bg=C['panel'], pady=10)
        hdr.pack(fill='x')
        tk.Label(hdr, text='🎬  字幕延迟器', bg=C['panel'], fg=C['accent'],
                  font=('Arial', 13, 'bold')).pack(side='left', padx=16)

        def sep(py=4):
            tk.Frame(p, bg=C['border'], height=1).pack(fill='x', padx=14, pady=py)

        # 模式
        sep()
        self._sec(p, '显示模式')
        self.mode_v = tk.StringVar(value='delay')
        mf = tk.Frame(p, bg=C['bg'])
        mf.pack(fill='x', padx=14, pady=4)
        modes = [
            ('delay',  '⏱  延迟显示',   '始终显示 N 秒前的画面（推荐）'),
            ('mosaic', '🔲  实时马赛克',  '模糊显示当前内容'),
            ('block',  '⏸  纯色遮挡',   '完全遮挡，右键手动查看'),
        ]
        for val, label, tip in modes:
            row = tk.Frame(mf, bg=C['bg'])
            row.pack(fill='x', pady=2)
            tk.Radiobutton(
                row, text=label, variable=self.mode_v, value=val,
                bg=C['bg'], fg=C['text'], selectcolor=C['surface'],
                activebackground=C['bg'], activeforeground=C['accent'],
                font=('Arial', 10)
            ).pack(side='left')
            tk.Label(row, text=tip, bg=C['bg'], fg=C['muted'],
                      font=('Arial', 8)).pack(side='left', padx=4)

        # 滑条
        sep()
        self._sec(p, '延迟时间')
        self.delay_v = tk.DoubleVar(value=self.delay)
        self._slider(p, self.delay_v, 0.5, 15, 0.5,
                     lambda v: setattr(self, 'delay', float(v)), '秒')

        self._sec(p, '遮挡透明度')
        self.alpha_v = tk.DoubleVar(value=1.0)
        self._slider(p, self.alpha_v, 0.1, 1.0, 0.05,
                     lambda v: self.ov.attributes('-alpha', float(v)))

        self._sec(p, '马赛克强度')
        self.mosaic_v = tk.IntVar(value=10)
        self._slider(p, self.mosaic_v, 2, 30, 1)

        # 颜色
        sep()
        tk.Button(p, text='🎨  选择遮挡颜色',
                   bg=C['surface'], fg=C['text'], relief='flat',
                   pady=5, cursor='hand2', font=('Arial', 10),
                   activebackground=C['border'],
                   command=self._pick_color).pack(padx=14, fill='x', pady=4)

        # 状态
        sep()
        self._badge = tk.Label(p, text='', bg=C['bg'], fg=C['muted'],
                                font=('Arial', 8), justify='left', anchor='w')
        self._badge.pack(fill='x', padx=14, pady=2)

        tips = (
            '📌  将红框拖动到视频字幕区域\n'
            '↔   右下角 ◢ 拖动调整框的大小\n'
            '🖱   右键遮挡框 → 手动查看 / 菜单'
        )
        tk.Label(p, text=tips, bg=C['surface'], fg=C['muted'],
                  font=('Arial', 8), justify='left',
                  padx=10, pady=7).pack(fill='x', padx=14, pady=6)

        if not CAPTURE_OK:
            miss = []
            if not PIL_OK: miss.append('Pillow')
            if not MSS_OK: miss.append('mss')
            tk.Label(
                p,
                text=f'⚠  延迟/马赛克需要安装:\npip install {" ".join(miss)}',
                bg='#2a1a00', fg='#ffa502', font=('Arial', 8),
                justify='left', padx=10, pady=6
            ).pack(fill='x', padx=14, pady=4)

    def _sec(self, parent, title):
        tk.Label(parent, text=title, bg=C['bg'], fg=C['muted'],
                  font=('Arial', 8, 'bold'), anchor='w',
                  padx=14).pack(fill='x', pady=(6, 0))

    def _slider(self, parent, var, lo, hi, res, cmd=None, unit=''):
        f = tk.Frame(parent, bg=C['bg'])
        f.pack(fill='x', padx=14, pady=(1, 4))
        row = tk.Frame(f, bg=C['bg'])
        row.pack(fill='x')
        tk.Label(row, textvariable=var, bg=C['bg'], fg=C['accent'],
                  font=('Consolas', 9), width=5, anchor='e').pack(side='right')
        if unit:
            tk.Label(row, text=unit, bg=C['bg'], fg=C['muted'],
                      font=('Arial', 8)).pack(side='right')
        tk.Scale(
            f, from_=lo, to=hi, resolution=res, orient='horizontal',
            variable=var, bg=C['bg'], fg=C['text'],
            highlightthickness=0, troughcolor=C['surface'],
            activebackground=C['accent'], sliderrelief='flat',
            showvalue=False, command=cmd
        ).pack(fill='x')

    # ─── Windows API ──────────────────────────────────────────────────────────

    def _enable_wda(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # 修复：必须声明返回类型为 c_void_p，否则 64 位 HWND 被截断
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
            user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

            def set_wda(tk_win):
                hwnd = user32.GetAncestor(tk_win.winfo_id(), 2)  # GA_ROOT=2
                if not hwnd:
                    hwnd = tk_win.winfo_id()
                return user32.SetWindowDisplayAffinity(hwnd, 0x00000011)

            # 遮挡框和快门都排除在截图之外
            ok1 = set_wda(self.ov)
            ok2 = set_wda(self._shutter)
            return bool(ok1 and ok2)
        except Exception:
            return False

    def _refresh_badge(self):
        if not CAPTURE_OK:
            self._badge.config(text='⚠  需安装 Pillow + mss', fg=C['yellow'])
        elif self.wda_ok:
            self._badge.config(text='✓  截图穿透已启用，延迟模式完整可用', fg=C['green'])
        else:
            self._badge.config(
                text='ℹ  截图穿透不可用（需 Win10 Build 19041+）\n   延迟/马赛克模式受限',
                fg=C['yellow']
            )

    # ─── 主线程截图（隐藏→截→恢复，避免截到自身）────────────────────────────

    def _capture_sync(self):
        """WDA 生效时遮挡框对 mss 不可见，直接截图即可，无需隐藏窗口。"""
        if not self._sct:
            return
        try:
            x, y, w, h = self._ov_rect
            if w < 10 or h < 10:
                return
            if self.wda_ok:
                # WDA 已排除遮挡框，直接截到真实画面
                shot = self._sct.grab({'top': y, 'left': x, 'width': w, 'height': h})
            else:
                # 降级：快门遮住 → 主框透明 → 截图 → 恢复
                prev_alpha = float(self.ov.attributes('-alpha'))
                self._shutter.geometry(f'{w}x{h}+{x}+{y}')
                self._shutter.attributes('-alpha', prev_alpha)
                self._shutter.lift()
                self._shutter.update()
                self.ov.attributes('-alpha', 0.0)
                self.ov.update()
                shot = self._sct.grab({'top': y, 'left': x, 'width': w, 'height': h})
                self.ov.attributes('-alpha', prev_alpha)
                self._shutter.attributes('-alpha', 0.0)
            img = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
            self.buf.push(img, time.time())
        except Exception:
            pass

    # ─── 主渲染循环 ───────────────────────────────────────────────────────────

    def _tick(self):
        if not self.running:
            return

        now  = time.time()
        mode = self.mode_v.get() if hasattr(self, 'mode_v') else 'block'

        # 更新遮挡框坐标
        try:
            self._ov_rect = (
                self.ov.winfo_x(), self.ov.winfo_y(),
                self.ov.winfo_width(), self.ov.winfo_height()
            )
        except Exception:
            pass

        # 每 5 tick (~400ms) 截一次图：先隐藏框，截框下方画面，再恢复
        self._tick_n += 1
        if CAPTURE_OK and self._tick_n % 5 == 0:
            self._capture_sync()

        # 手动查看优先级最高
        if now < self._manual_reveal_until:
            frame = self.buf.latest()
            if frame:
                self._show_frame(frame)
                rem = self._manual_reveal_until - now
                self._set_status(f'👁  手动查看 ({rem:.1f}s)', '#bbb')
            self._root.after(80, self._tick)
            return

        if mode == 'delay' and CAPTURE_OK:
            # ════ 核心逻辑：永远显示 delay 秒前的画面 ════
            target_ts = now - self.delay
            oldest = self.buf.oldest_ts()
            if oldest is not None and oldest <= target_ts:
                frame = self.buf.get_at(target_ts)
                if frame:
                    self._show_frame(frame)
                    self._set_status(f'⏱  {self.delay:.1f}s 延迟中')
                else:
                    self._show_solid()
                    self._set_status('⏳  缓冲中...')
            else:
                # 刚启动，缓冲还不够久
                self._show_solid()
                if oldest is not None:
                    waited = now - oldest
                    remain = self.delay - waited
                    self._set_status(f'⏳  预热中，还需 {remain:.1f}s...')
                else:
                    self._set_status('⏳  等待捕获...')

        elif mode == 'mosaic' and CAPTURE_OK:
            frame = self.buf.latest()
            if frame:
                self._show_mosaic(frame)
                self._set_status('🔲  马赛克遮挡中')
            else:
                self._show_solid()
                self._set_status('🔲  等待画面...')

        else:
            self._show_solid()
            self._set_status('⏸  遮挡中（右键手动查看）')

        self._root.after(80, self._tick)

    # ─── 渲染 ─────────────────────────────────────────────────────────────────

    def _show_solid(self):
        self.cv.delete('img')
        self.cv.configure(bg=self.block_color)
        self._grip.configure(bg=self.block_color)

    def _show_frame(self, img):
        try:
            w = self.cv.winfo_width()
            h = self.cv.winfo_height()
            if w < 2 or h < 2:
                return
            ph = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
            self.cv.delete('img')
            self.cv.create_image(0, 0, anchor='nw', image=ph, tags='img')
            self.cv.tag_raise(self._sid_bg)
            self.cv.tag_raise(self._sid)
            self.photo = ph
        except Exception:
            pass

    def _show_mosaic(self, img):
        try:
            w = self.cv.winfo_width()
            h = self.cv.winfo_height()
            if w < 2 or h < 2:
                return
            ms = max(2, self.mosaic_v.get())
            mosaic = img.resize(
                (max(1, w // ms), max(1, h // ms)), Image.NEAREST
            ).resize((w, h), Image.NEAREST)
            ph = ImageTk.PhotoImage(mosaic)
            self.cv.delete('img')
            self.cv.create_image(0, 0, anchor='nw', image=ph, tags='img')
            self.cv.tag_raise(self._sid_bg)
            self.cv.tag_raise(self._sid)
            self.photo = ph
        except Exception:
            pass

    def _set_status(self, text, color='#ffffff'):
        try:
            self.cv.itemconfig(self._sid, text=text, fill=color)
        except Exception:
            pass

    # ─── 交互 ─────────────────────────────────────────────────────────────────

    def _pick_color(self):
        res = colorchooser.askcolor(color=self.block_color,
                                     title='选择遮挡颜色', parent=self.panel)
        if res and res[1]:
            self.block_color = res[1]

    def _ctx(self, event):
        m = tk.Menu(self._root, tearoff=0,
                     bg=C['surface'], fg=C['text'],
                     activebackground=C['accent'], activeforeground='#fff')
        m.add_command(label='👁  立即查看 3 秒',
                       command=lambda: self._manual_peek(3))
        m.add_command(label='👁  立即查看 6 秒',
                       command=lambda: self._manual_peek(6))
        m.add_command(label='⚙  控制面板', command=self.panel.lift)
        m.add_separator()
        m.add_command(label='✕  退出', command=self.quit)
        m.post(event.x_root, event.y_root)

    def _manual_peek(self, seconds=3):
        self._manual_reveal_until = time.time() + seconds

    # ─── 拖动 & 缩放 ──────────────────────────────────────────────────────────

    def _d0(self, e):
        self._dd = {'x': e.x_root - self.ov.winfo_x(),
                     'y': e.y_root - self.ov.winfo_y()}

    def _d1(self, e):
        if self._dd:
            self.ov.geometry(f'+{e.x_root-self._dd["x"]}+{e.y_root-self._dd["y"]}')

    def _d2(self, e):
        self._dd = {}

    def _r0(self, e):
        self._rd = {'x': e.x_root, 'y': e.y_root,
                     'w': self.ov.winfo_width(), 'h': self.ov.winfo_height()}

    def _r1(self, e):
        if self._rd:
            w = max(200, self._rd['w'] + e.x_root - self._rd['x'])
            h = max(40,  self._rd['h'] + e.y_root - self._rd['y'])
            self.ov.geometry(f'{w}x{h}')

    # ─── 退出 ─────────────────────────────────────────────────────────────────

    def quit(self):
        self.running = False
        if self._sct:
            try:
                self._sct.close()
            except Exception:
                pass
        try:
            self._root.quit()
            self._root.destroy()
        except Exception:
            pass


if __name__ == '__main__':
    if not CAPTURE_OK:
        miss = [x for x, ok in [('Pillow', PIL_OK), ('mss', MSS_OK)] if not ok]
        print(f'\n⚠  建议安装依赖以启用延迟/马赛克模式：')
        print(f'   pip install {" ".join(miss)}\n')

    SubtitleDelayer()
