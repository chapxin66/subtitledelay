# 字幕延迟器 iPad 版 — 实现方案

## 背景与目标

**字幕延迟器**：在视频字幕区域上方覆盖一个遮挡框，框内永远显示"N 秒前"的画面。
用户在 T 秒看到字幕时，框内显示的是 T-N 秒的内容，字幕自然被藏住，不会提前剧透。

### 核心逻辑（与 Windows 版相同）

```
每帧截图 → 存入带时间戳的环形缓冲区 → 显示时取 (now - delay) 时刻的帧
```

### Windows 版已验证的关键设计

- `FrameBuffer`：线程安全的带时间戳帧队列，自动丢弃超过 max_seconds 的旧帧
- 渲染循环：每 80ms tick 一次，从 FrameBuffer 取延迟帧渲染到遮挡框
- 截图穿透：Windows 用 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`，让遮挡框对截图 API 不可见，mss 直接看穿它拍到下方真实画面，遮挡框对用户全程可见、无闪烁

**iPad 版的核心挑战**：iOS 没有 WDA，需要用其他手段让截图绕过遮挡框。

---

## 方案一：isSecureTextEntry 图层技巧

### 原理

iOS 为保护密码输入框，会把 `isSecureTextEntry = true` 的 UITextField **子图层自动排除在截图和 ReplayKit 录屏之外**。将遮挡 View 挂到这个 TextField 的图层上，对用户正常可见，但所有截图 API 都看不到它——效果与 Windows WDA 完全一致。

### 架构

```
App (SwiftUI / UIKit)
├── VideoPlayerView          # 播放视频（或覆盖在任意内容上方）
├── OverlayContainerView     # 遮挡框容器
│   └── SecureFieldHost      # UITextField (isSecureTextEntry=true) 的 UIViewRepresentable 包装
│       └── overlayView.layer  # 挂在 secureField.layer 上的真正遮挡 View
│           └── DelayedFrameView  # 显示延迟帧的 ImageView
└── ControlPanelView         # 控制面板（延迟时长、模式切换等）

CaptureEngine               # 独立模块，负责截图 + FrameBuffer 管理
├── FrameBuffer              # 带时间戳的环形帧队列
├── captureLoop()            # 定时截图，直接调 UIScreen / ReplayKit，遮挡框不出现在截图里
└── getFrame(at: delay)      # 取延迟帧
```

### 关键实现

```swift
// 1. SecureField 包装器（UIViewRepresentable）
struct SecureOverlayHost: UIViewRepresentable {
    let overlayView: UIView

    func makeUIView(context: Context) -> UITextField {
        let field = UITextField()
        field.isSecureTextEntry = true
        field.isUserInteractionEnabled = false
        // 把遮挡 View 挂到 secureField 的图层
        field.layer.addSublayer(overlayView.layer)
        return field
    }

    func updateUIView(_ uiView: UITextField, context: Context) {
        // 同步 overlayView frame
        overlayView.layer.frame = uiView.bounds
    }
}

// 2. 截图（遮挡框不可见，拍到真实画面）
func captureScreen(in rect: CGRect) -> UIImage? {
    // 方式A：UIScreen snapshot（轻量，主线程）
    UIGraphicsBeginImageContextWithOptions(rect.size, false, UIScreen.main.scale)
    UIScreen.main.snapshotView(afterScreenUpdates: false)
        .drawHierarchy(in: CGRect(origin: .zero, size: rect.size), afterScreenUpdates: false)
    let img = UIGraphicsGetImageFromCurrentImageContext()
    UIGraphicsEndImageContext()
    return img
    // 注：snapshotView 不包含 secureTextField 的子图层，所以遮挡框不出现
}

// 3. FrameBuffer（Swift 版）
actor FrameBuffer {
    private var frames: [(timestamp: Double, image: UIImage)] = []
    private let maxSeconds: Double = 20.0

    func push(_ image: UIImage) {
        let now = Date().timeIntervalSince1970
        frames.append((now, image))
        let cutoff = now - maxSeconds
        frames.removeAll { $0.timestamp < cutoff }
    }

    func frame(at targetTimestamp: Double) -> UIImage? {
        frames.last { $0.timestamp <= targetTimestamp }?.image
    }
}

// 4. 渲染循环
func startRenderLoop() {
    Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { _ in
        let targetTs = Date().timeIntervalSince1970 - self.delaySeconds
        Task {
            if let frame = await self.frameBuffer.frame(at: targetTs) {
                await MainActor.run {
                    self.delayedFrameView.image = frame
                }
            }
        }
    }
}
```

### 截图触发方式

每 400ms（可调）在后台线程截一次图：

```swift
Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { _ in
    DispatchQueue.global(qos: .userInitiated).async {
        if let img = self.captureScreen(in: self.overlayRect) {
            Task { await self.frameBuffer.push(img) }
        }
    }
}
```

### 优缺点

| 优点 | 缺点 |
|------|------|
| 无需用户授权额外权限 | 依赖苹果私有行为，非文档化 API |
| 代码简单，无权限弹窗 | iOS 系统更新可能失效 |
| 遮挡框对用户全程可见，零闪烁 | 只在同一 App 进程内有效（无法遮挡其他 App 的字幕） |
| 类比 WDA，原理最直接 | 需要用户在 App 内播放视频 |

---

## 方案二：ReplayKit 双流分离

### 原理

用 `RPScreenRecorder.shared().startCapture` 获取**整个屏幕的实时帧流**（包含所有 App）。
遮挡框正常显示给用户，但在帧流里用已知的遮挡框 rect 把该区域**裁掉/跳过**，只把"真实画面"区域送入 FrameBuffer。
这样 FrameBuffer 里永远是无遮挡的原始内容。

### 架构

```
App
├── OverlayWindow            # 遮挡框窗口（独立 UIWindowScene）
│   └── DelayedFrameView     # 显示延迟帧
└── ControlPanelWindow       # 控制面板

ReplayCaptureEngine
├── startCapture()           # 启动 RPScreenRecorder 帧流
├── onFrame(sampleBuffer)    # 每帧回调
│   ├── crop(overlayRect)    # 裁出字幕区域（此时遮挡框在帧里，但我们只用它的坐标，不用它的像素）
│   │   └── 注：此处需要知道遮挡框在屏幕上的精确坐标
│   └── frameBuffer.push()
└── FrameBuffer              # 同方案一
```

### 关键实现

```swift
// 1. 启动 ReplayKit 屏幕捕获
import ReplayKit

class ReplayCaptureEngine {
    let frameBuffer = FrameBuffer()
    var overlayRect: CGRect = .zero  // 遮挡框在屏幕坐标系中的位置

    func start() {
        RPScreenRecorder.shared().startCapture(
            handler: { [weak self] sampleBuffer, bufferType, error in
                guard bufferType == .video,
                      let self,
                      let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer)
                else { return }

                let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
                let fullSize = ciImage.extent

                // 把屏幕坐标的 overlayRect 转成 CIImage 坐标（Y 轴翻转）
                let captureRect = CGRect(
                    x: self.overlayRect.minX,
                    y: fullSize.height - self.overlayRect.maxY,
                    width: self.overlayRect.width,
                    height: self.overlayRect.height
                )

                // 裁出遮挡框区域的真实画面（此时 RPScreenRecorder 捕获的帧里
                // 遮挡框 View 是可见的，需要在遮挡框建立之前先拿背景帧，
                // 或配合方案一的 secureLayer 让遮挡框在 ReplayKit 帧里也不可见）
                let cropped = ciImage.cropped(to: captureRect)
                let context = CIContext()
                if let cgImage = context.createCGImage(cropped, from: cropped.extent) {
                    let uiImage = UIImage(cgImage: cgImage)
                    Task { await self.frameBuffer.push(uiImage) }
                }
            },
            completionHandler: { error in
                if let error { print("ReplayKit start error: \(error)") }
            }
        )
    }

    func stop() {
        RPScreenRecorder.shared().stopCapture()
    }
}
```

> **重要**：ReplayKit 捕获的帧里遮挡框是可见的（普通 View）。
> 解决方式：**将方案一的 secureLayer 技巧也应用到遮挡框上**，这样 ReplayKit 同样看不到遮挡框，裁出来的就是纯净的真实画面。两方案组合使用效果最佳。

### 权限处理

```swift
// 首次调用时系统弹窗"允许录制屏幕？"
// 需在 Info.plist 添加：
// NSMicrophoneUsageDescription（即使不录音也可能需要）
// 以及引导用户说明用途

RPScreenRecorder.shared().isMicrophoneEnabled = false  // 不需要麦克风
```

### 优缺点

| 优点 | 缺点 |
|------|------|
| 可捕获任意 App 的屏幕内容 | 需要用户授权录屏权限（有系统弹窗） |
| 官方 API，稳定可靠 | 权限被拒时功能完全失效 |
| 帧率高（可达 60fps） | 单独使用时遮挡框出现在帧里，需配合 secureLayer |
| 可做跨 App 字幕遮挡 | 后台运行时 ReplayKit 可能被系统限制 |

---

## 两方案对比

| | 方案一（secureLayer） | 方案二（ReplayKit） |
|---|---|---|
| 权限要求 | 无 | 录屏权限（系统弹窗） |
| 可遮挡其他 App | 否 | 是 |
| API 稳定性 | 不稳定（私有行为） | 稳定（官方 API） |
| 遮挡框零闪烁 | 是 | 是（配合 secureLayer） |
| 实现复杂度 | 低 | 中 |
| 推荐场景 | App 内视频播放器 | 系统级字幕遮挡（任意 App）|

**推荐组合**：遮挡框用方案一（secureLayer，无权限要求），截图用方案二（ReplayKit，帧率高）。这样遮挡框既对用户可见、又对 ReplayKit 不可见，同时还能跨 App 工作。

---

## 需要实现的功能模块

参考 Windows 版完整功能：

### 必须实现
- [ ] `FrameBuffer`：带时间戳的环形帧队列（Swift Actor，线程安全）
- [ ] 遮挡框 View：可拖动、可缩放（手势），显示延迟帧
- [ ] SecureOverlayHost：UITextField secureLayer 包装（UIViewRepresentable）
- [ ] 截图引擎：定时截图 → push 到 FrameBuffer
- [ ] 渲染循环：每 80ms 取 (now - delay) 时刻的帧渲染
- [ ] 控制面板：延迟时长滑条（0.5s - 15s）

### 可选实现
- [ ] 马赛克模式：最新帧降采样放大
- [ ] 纯色遮挡模式：右键/长按手动查看
- [ ] 透明度调节
- [ ] 预热提示（缓冲不足时显示剩余等待时间）

---

## 技术依赖

| 框架 | 用途 | 系统要求 |
|------|------|----------|
| SwiftUI / UIKit | UI 框架 | iOS 14+ 推荐 |
| ReplayKit | 屏幕帧流捕获（方案二） | iOS 11+ |
| AVFoundation | 视频播放（如需内置播放器） | iOS 10+ |
| CoreImage | 截图裁剪、图像处理 | iOS 9+ |

最低系统要求：**iOS 14**（支持 SwiftUI + Actor）
