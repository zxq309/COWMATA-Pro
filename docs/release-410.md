# COWMATA Pro 4.1.0

## 视频解析恢复

- 4.0.0 工程中因媒体工具暂时不可用而记录为异常的录像，在升级到 4.1.0 后会自动回到待解析队列；只有明确属于 FFmpeg/FFprobe 缺失的错误会这样恢复，损坏或时间轴不可信的源文件仍保留原错误。
- 解析使用发布包内置的 `vendor/ffmpeg/bin/ffmpeg.exe` 和 `ffprobe.exe`，不修改原始录像。重新打开工程即可继续索引，按需解析策略保持不变。

## 版本缓存清理

- 每次版本变化只清理工程维护清单中已确认的派生兼容视频、播放时间轴缓存和大华时长缓存。
- 原始录像、Motion/PPG/Temp、标注、人工校正、证据图和未完成归类任务不受影响。
- 若另一个 COWMATA 窗口仍占用工程，清理会延后到占用释放后自动重试。

## 交付

- `COWMATA-Pro-4.1.0-Setup.exe`
- `COWMATA-Pro-4.1.0-Portable.zip`
- `COWMATA-Pro-4.1.0-Source.zip`

安装包发布到 GitHub Release；便携包和源码包用于离线部署与复现。
