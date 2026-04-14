# 字幕延迟器 — 技术说明

## 核心原理

遮挡框里永远显示"N 秒前"的画面，而不是当前画面。用户在 T 秒看到字幕时，遮挡框里显示的是 T-N 秒的内容，字幕自然被"藏"住。

---

## 技术栈

| 库 | 用途 |
|---|---|
| `tkinter` | GUI 框架，所有窗口均由此构建 |
| `mss` | 高性能跨平台截屏（比 PIL.ImageGrab 快） |
| `Pillow` | 图像处理：缩放、马赛克、转换为 tkinter 可用格式 |
| `ctypes` | 调用 Windows 原生 API |
| `threading` | 帧缓冲区的线程安全锁 |

---

## 关键模块

### 1. FrameBuffer — 帧缓冲区

```
FrameBuffer
├── push(img, ts)       # 存入一帧（带时间戳）
├── get_at(target_ts)   # 取最接近且不晚于 target_ts 的帧
├── latest()            # 取最新帧（马赛克模式用）
└── oldest_ts()         # 最早帧的时间戳（判断缓冲是否够久）
```

- 内部用 `collections.deque` 存储，自动丢弃超过 `max_seconds`（默认20秒）的旧帧
- 所有操作加 `threading.Lock`，截图线程和渲染线程安全共享

### 2. 截图机制 — `_capture_sync()`

每 5 个 tick（约 400ms，~2.5 fps）执行一次，步骤：

1. **快门窗口上移** — 纯黑透明窗口覆盖字幕区，用户看不到真实画面
2. **主遮挡框隐藏** — `alpha=0.0`，让截图 API 能"看穿"它
3. **`mss.grab()` 截图** — 捕获遮挡框下方的真实内容
4. **恢复** — 主框还原透明度，快门隐藏

快门窗口的作用：截图的 hide/show 之间有极短时间窗，如果没有快门，用户肉眼可能看到字幕一闪而过。

### 3. WDA 截图穿透 — `_enable_wda()`

调用 Windows API：

```python
SetWindowDisplayAffinity(hwnd, 0x00000011)  # WDA_EXCLUDEFROMCAPTURE
```

效果：遮挡窗口在屏幕上正常显示，但对截图/录屏 API 完全不可见，`mss` 直接穿透拍到下方真实画面，无需 hide/show，彻底消除闪烁。

- 要求：Windows 10 Build 19041（2004）及以上
- 当前代码中 `_enable_wda()` 已实现但**未被调用**，`wda_ok` 始终为 `False`，系统回退到快门方案

### 4. 主渲染循环 — `_tick()`

每 80ms 触发一次（`root.after(80, self._tick)`），根据当前模式决定显示内容：

| 模式 | 行为 |
|---|---|
| `delay` | 从 FrameBuffer 取 `now - delay` 时刻的帧显示 |
| `mosaic` | 取最新帧，降采样再放大产生马赛克效果 |
| `block` | 纯色填充，右键可手动查看 3/6 秒 |

预热阶段（缓冲帧不足 delay 秒时）显示纯色并提示剩余等待时间。

### 5. 马赛克实现

```python
# 缩小再放大 = 像素块化
mosaic = img.resize((w // ms, h // ms), NEAREST).resize((w, h), NEAREST)
```

`ms` 为马赛克强度（2-30），值越大像素块越粗。

---

## 窗口结构

```
_root (隐藏的主窗口，mainloop 寄宿在此)
├── ov (遮挡框) — overrideredirect，无边框，topmost
│   ├── _inner (Frame，提供 padding)
│   │   └── cv (Canvas，显示图像/纯色/状态文字)
│   └── _grip (Label，右下角拖拽缩放手柄)
├── _shutter (快门窗口) — 平时 alpha=0，截图时短暂显示
└── panel (控制面板) — 正常窗口，含所有设置项
```

---

## 已知限制

- 截图频率约 2.5 fps，遮挡框内画面会有轻微跳帧感
- WDA 功能已实现未启用，闪烁风险未彻底消除
- `mss` + `Pillow` 为可选依赖，缺失时只能使用纯色遮挡模式
