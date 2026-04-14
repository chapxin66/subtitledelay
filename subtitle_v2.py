#!/usr/bin/env python3
"""
字幕延迟器 v3 — dxcam 录屏方案
================================
唯一功能：遮挡框始终显示「N 秒前」的画面。

依赖：
    pip install dxcam Pillow

要求 Windows 10 2004+（Build 19041+），用于 WDA_EXCLUDEFROMCAPTURE
让遮挡框对 dxcam 不可见。
"""

import tkinter as tk
from collections import deque
import threading
import time
import platform
import ctypes

# ── 依赖检查 ──────────────────────────────────────────────────────────────────

try:
    import dxcam
    DXCAM_OK = True
except ImportError:
    DXCAM_OK = False

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

CAPTURE_OK = DXCAM_OK and PIL_OK

# ── 颜色 ──────────────────────────────────────────────────────────────────────

C = {
    'bg':      '#0d1117',
    'panel':   '#161b22',
    'surface': '#21262d',
    'border':  '#30363d',
    'accent':  '#58a6ff',
    'green':   '#3fb950',
    'yellow':  '#d29922',
    'red':     '#ff4757',
    'text':    '#c9d1d9',
    'muted':   '#6e7681',
}

# ── 帧缓冲 ────────────────────────────────────────────────────────────────────

class FrameBuffer:
    """线程安全的环形帧缓冲区"""

    def __init__(self, max_seconds: float = 30.0):
        self._buf: deque = deque()
        self._lock = threading.Lock()
        self._max = max_seconds

    def push(self, img, ts: float):
        with self._lock:
            self._buf.append((ts, img))
            cutoff = ts - self._max
            while self._buf and self._buf[0][0] < cutoff:
                self._buf.popleft()

    def get_at(self, target_ts: float):
        """返回时间戳 <= target_ts 的最新帧，没有则返回 None。"""
        with self._lock:
            result = None
            for ts, img in self._buf:
                if ts <= target_ts:
                    result = img
                else:
                    break
            return result

    def oldest_ts(self):
        with self._lock:
            return self._buf[0][0] if self._buf else None

    def __len__(self):
        with self._lock:
            return len(self._buf)


# ── 主程序 ────────────────────────────────────────────────────────────────────

class SubtitleDelayerV2:

    # 采集目标帧率（dxcam 会尽力维持）
    CAPTURE_FPS = 60

    def __init__(self):
        self.running = True
        self.delay   = 3.0          # 默认延迟秒数
        self.buf     = FrameBuffer(max_seconds=30.0)
        self.photo   = None         # 防止 GC 回收 PhotoImage
        self._ov_rect = (0, 0, 0, 0)
        self._wda_ok  = False
        self._capture_error: str | None = None

        self._root = tk.Tk()
        self._root.withdraw()

        self._build_overlay()
        self._build_panel()

        # 窗口必须先渲染才能拿到有效 HWND
        self._root.update()
        self._wda_ok = self._enable_wda()
        self._refresh_badge()

        # 启动 dxcam 采集线程
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True
        )
        self._capture_thread.start()

        # 启动渲染循环
        self._render_tick()
        self._root.mainloop()

    # ── 遮挡窗口 ─────────────────────────────────────────────────────────────

    def _build_overlay(self):
        ov = tk.Toplevel(self._root)
        ov.overrideredirect(True)
        ov.attributes('-topmost', True)
        ov.geometry('880x100+20+860')
        ov.configure(bg='#000000')
        self.ov = ov

        self.cv = tk.Canvas(ov, bg='#000000', highlightthickness=0, cursor='fleur')
        self.cv.pack(fill='both', expand=True)

        # 状态文字
        self._sid = self.cv.create_text(
            7, 5, anchor='nw', text='⏳ 预热中...',
            fill='#ffffff', font=('Consolas', 9, 'bold')
        )

        # 右下角拖拽缩放手柄
        self._grip = tk.Label(ov, text='◢', fg='#555555', bg='#000000',
                               cursor='sizing', font=('Arial', 10))
        self._grip.place(relx=1.0, rely=1.0, anchor='se')

        # 绑定拖动
        self._dd: dict = {}
        for w in (self.cv,):
            w.bind('<Button-1>',        self._d0)
            w.bind('<B1-Motion>',       self._d1)
            w.bind('<ButtonRelease-1>', self._d2)

        # 绑定缩放
        self._rd: dict = {}
        self._grip.bind('<Button-1>',  self._r0)
        self._grip.bind('<B1-Motion>', self._r1)

    # ── 控制面板 ─────────────────────────────────────────────────────────────

    def _build_panel(self):
        p = tk.Toplevel(self._root)
        p.title('字幕延迟器 v3')
        p.attributes('-topmost', True)
        p.geometry('280x220+20+20')
        p.configure(bg=C['bg'])
        p.resizable(False, False)
        p.protocol('WM_DELETE_WINDOW', self.quit)
        self.panel = p

        hdr = tk.Frame(p, bg=C['panel'], pady=10)
        hdr.pack(fill='x')
        tk.Label(hdr, text='字幕延迟器 v3', bg=C['panel'], fg=C['accent'],
                  font=('Arial', 12, 'bold')).pack(side='left', padx=16)

        tk.Frame(p, bg=C['border'], height=1).pack(fill='x', padx=14, pady=6)

        tk.Label(p, text='延迟时间', bg=C['bg'], fg=C['muted'],
                  font=('Arial', 8, 'bold'), anchor='w',
                  padx=14).pack(fill='x')

        self.delay_v = tk.DoubleVar(value=self.delay)
        f = tk.Frame(p, bg=C['bg'])
        f.pack(fill='x', padx=14, pady=(0, 6))
        row = tk.Frame(f, bg=C['bg'])
        row.pack(fill='x')
        tk.Label(row, textvariable=self.delay_v, bg=C['bg'], fg=C['accent'],
                  font=('Consolas', 10), width=5, anchor='e').pack(side='right')
        tk.Label(row, text='秒', bg=C['bg'], fg=C['muted'],
                  font=('Arial', 8)).pack(side='right')
        tk.Scale(
            f, from_=0.5, to=15.0, resolution=0.5, orient='horizontal',
            variable=self.delay_v, bg=C['bg'], fg=C['text'],
            highlightthickness=0, troughcolor=C['surface'],
            activebackground=C['accent'], sliderrelief='flat',
            showvalue=False,
            command=lambda v: setattr(self, 'delay', float(v))
        ).pack(fill='x')

        tk.Frame(p, bg=C['border'], height=1).pack(fill='x', padx=14, pady=4)

        self._badge = tk.Label(p, text='', bg=C['bg'], fg=C['muted'],
                                font=('Arial', 8), justify='left', anchor='w',
                                wraplength=250)
        self._badge.pack(fill='x', padx=14, pady=4)

        tip = tk.Label(p, text='拖动黑框到字幕区  |  右下角 ◢ 调整大小',
                        bg=C['surface'], fg=C['muted'], font=('Arial', 8),
                        padx=10, pady=6)
        tip.pack(fill='x', padx=14, pady=(0, 6))

        if not CAPTURE_OK:
            miss = []
            if not DXCAM_OK: miss.append('dxcam')
            if not PIL_OK:   miss.append('Pillow')
            tk.Label(
                p,
                text=f'⚠ 请安装：pip install {" ".join(miss)}',
                bg='#2a1a00', fg='#ffa502', font=('Arial', 8),
                justify='left', padx=10, pady=6
            ).pack(fill='x', padx=14)

    # ── Windows WDA ───────────────────────────────────────────────────────────

    def _enable_wda(self) -> bool:
        """让遮挡框对 dxcam（DXGI）不可见。需要 Win10 Build 19041+。"""
        if platform.system() != 'Windows':
            return False
        try:
            user32 = ctypes.windll.user32
            user32.GetAncestor.restype     = ctypes.c_void_p
            user32.GetAncestor.argtypes    = [ctypes.c_void_p, ctypes.c_uint]
            user32.SetWindowDisplayAffinity.restype  = ctypes.c_bool
            user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

            WDA_EXCLUDEFROMCAPTURE = 0x00000011

            hwnd = user32.GetAncestor(self.ov.winfo_id(), 2)  # GA_ROOT=2
            if not hwnd:
                hwnd = self.ov.winfo_id()
            return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            return False

    def _refresh_badge(self):
        if not CAPTURE_OK:
            self._badge.config(text='⚠ 需安装 dxcam + Pillow', fg=C['yellow'])
        elif self._wda_ok:
            self._badge.config(text='✓ 截图穿透已启用，dxcam 60fps 采集中', fg=C['green'])
        else:
            self._badge.config(
                text='ℹ 截图穿透不可用（需 Win10 Build 19041+）\n  遮挡框可能出现在截图中',
                fg=C['yellow']
            )

    # ── dxcam 采集线程 ────────────────────────────────────────────────────────

    def _capture_loop(self):
        """后台线程：用 dxcam.grab() 轮询采集遮挡框区域，存入环形缓冲。"""
        if not CAPTURE_OK:
            return

        interval = 1.0 / self.CAPTURE_FPS  # 目标帧间隔

        try:
            camera = dxcam.create(output_color='RGB')
        except Exception as e:
            self._capture_error = f'dxcam 初始化失败: {e}'
            return

        self._capture_error = None

        try:
            while self.running:
                t0 = time.perf_counter()

                x, y, w, h = self._ov_rect
                if w < 10 or h < 10:
                    time.sleep(0.05)
                    continue

                region = (x, y, x + w, y + h)
                try:
                    frame = camera.grab(region=region)  # numpy RGB or None
                except Exception as e:
                    self._capture_error = str(e)
                    time.sleep(0.1)
                    continue

                if frame is not None:
                    self._capture_error = None
                    img = Image.fromarray(frame)
                    self.buf.push(img, time.time())

                # 限速到目标 fps
                elapsed = time.perf_counter() - t0
                sleep_t = interval - elapsed
                if sleep_t > 0:
                    time.sleep(sleep_t)

        finally:
            try:
                del camera
            except Exception:
                pass

    # ── 渲染循环（主线程，~60fps）────────────────────────────────────────────

    def _render_tick(self):
        if not self.running:
            return

        now = time.time()

        # 同步遮挡框坐标
        try:
            self._ov_rect = (
                self.ov.winfo_x(), self.ov.winfo_y(),
                self.ov.winfo_width(), self.ov.winfo_height()
            )
        except Exception:
            pass

        if CAPTURE_OK:
            # 采集线程报错时，优先显示错误
            if self._capture_error:
                self._show_black(f'⚠ {self._capture_error}')
            else:
                target_ts = now - self.delay
                oldest    = self.buf.oldest_ts()

                if oldest is not None and oldest <= target_ts:
                    frame = self.buf.get_at(target_ts)
                    if frame:
                        self._show_frame(frame)
                        self._set_status(f'⏱ {self.delay:.1f}s 前')
                    else:
                        self._show_black('⏳ 缓冲中...')
                else:
                    if oldest is not None:
                        remain = self.delay - (now - oldest)
                        self._show_black(f'⏳ 预热中，还需 {remain:.1f}s')
                    else:
                        self._show_black('⏳ 等待采集...')
        else:
            self._show_black('⚠ 需安装 dxcam + Pillow')

        self._root.after(16, self._render_tick)   # ~60fps

    # ── 渲染帧 ────────────────────────────────────────────────────────────────

    def _show_frame(self, img: 'Image.Image'):
        try:
            w = self.cv.winfo_width()
            h = self.cv.winfo_height()
            if w < 2 or h < 2:
                return
            ph = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
            self.cv.delete('img')
            self.cv.create_image(0, 0, anchor='nw', image=ph, tags='img')
            self.cv.tag_raise(self._sid)
            self.photo = ph          # 防止被 GC
        except Exception:
            pass

    def _show_black(self, status: str = ''):
        self.cv.delete('img')
        self.cv.configure(bg='#000000')
        self._set_status(status)

    def _set_status(self, text: str):
        try:
            self.cv.itemconfig(self._sid, text=text)
        except Exception:
            pass

    # ── 拖动 & 缩放 ──────────────────────────────────────────────────────────

    def _d0(self, e):
        self._dd = {'x': e.x_root - self.ov.winfo_x(),
                     'y': e.y_root - self.ov.winfo_y()}

    def _d1(self, e):
        if self._dd:
            self.ov.geometry(f'+{e.x_root - self._dd["x"]}+{e.y_root - self._dd["y"]}')

    def _d2(self, _e):
        self._dd = {}

    def _r0(self, e):
        self._rd = {'x': e.x_root, 'y': e.y_root,
                     'w': self.ov.winfo_width(), 'h': self.ov.winfo_height()}

    def _r1(self, e):
        if self._rd:
            w = max(200, self._rd['w'] + e.x_root - self._rd['x'])
            h = max(40,  self._rd['h'] + e.y_root - self._rd['y'])
            self.ov.geometry(f'{w}x{h}')

    # ── 退出 ─────────────────────────────────────────────────────────────────

    def quit(self):
        self.running = False
        try:
            self._root.quit()
            self._root.destroy()
        except Exception:
            pass


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if not CAPTURE_OK:
        miss = [n for n, ok in [('dxcam', DXCAM_OK), ('Pillow', PIL_OK)] if not ok]
        print(f'⚠  请先安装依赖：pip install {" ".join(miss)}')

    SubtitleDelayerV2()
