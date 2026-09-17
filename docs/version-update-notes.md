# 版本更新说明：v2.1.0 → v2.2.0

> 本文基于已发布的 `v2.1.0` 标签与本次 `v2.2.0` 发布分支的逐文件比对。所有播放修复均在 `SafeMediaEngine` 及其配套模块内，不影响普通 MP4 的处理路径。**没有修改、移动或重新封装任何源视频文件**；缓存仍为原有轻量 JSON 格式，无需清理。

## 1. 本次更新的核心目标

高邮牧场 2026-08-18~08-22 三个摄像头目录（乐橙 / 右1 / 海康）的录像都是"伪装成 `.mp4` 的 MPEG 程序流"：libVLC 对它们报告的时长要么是 0，要么是按首尾 PTS 跨度算出的 8~16 小时垃圾值。v2.1.0 只对海康强制了 avformat 解复用并做包级时间轴校正，其余格式会直接暴露错误时长，导致进度条比例错误、九轴同步区间被截断。本版本为这三种录像建立了一套"**先校验、后发布**"的时长与跳转体系，并补上了数据卫生防护。

## 2. 变更清单

### 2.1 新增：乐橙（Imou/Dahua）MPEG-PS 专用支持

- 新增 [dahua_duration.py](../cowmata_tailring/media/dahua_duration.py)（847 行）：内容级程序流扫描（HEVC 帧计数、关键帧位置、PES 时间戳），配合 ffprobe 包扫描与相邻文件 mtime 差三重交叉验证，产出真实时长与跳转索引。
- 新增 [dahua_stream.py](../cowmata_tailring/media/dahua_stream.py)（267 行）：时间戳归一化的回调式虚拟播放流，将乐橙文件中不可用的 DTS/PTS 现场修补为单调时钟，并以内置关键帧索引实现精确跳转。
- **效果**：乐橙文件在 v2.1.0 下显示 VLC 的 662 s（或 0），现为正确的 2 010 000 ms（33.5 min），跳转落点误差 0 ms（真实窗口实测）。

### 2.2 修复：时长仲裁不再被拷贝破坏的 mtime 覆盖

`e98f261`（标签 `fix/dahua-mtime-arbitration-20260826`）

- 完整内容扫描结果不再被相邻文件 mtime 差覆盖；mtime 估计值仅在扫描为空、不完整或两者互相印证时参与。缓存 schema 2→3，旧缓存若存有"扫描与 mtime 冲突"的错误值会自动重扫。
- 实测仲裁表（mtime 差 0.9 s / 1 s / 2.6 s / 30 s / 7200 s 与完整扫描冲突时）全部保持正确时长。

### 2.3 修复：乐橙探测失败的内置后备

`1cb621f`（标签 `fix/dahua-probe-fallback-20260826`）

- ffprobe 缺失、失败、空结果或超时（30 s）时，改用内置 MPEG-PS 内容扫描推断帧时长（从 MPEG 时钟增量回归），实测在真实文件上推断出 66.668 ms 帧时长、1 988 000 ms 总时长，约 0.6 s 完成。

### 2.4 修复：全零占位文件的识别与跳过

`08e532d`（标签 `fix/zero-video-placeholders-20260826`）

- 新增 [video_file_health.py](../cowmata_tailring/media/video_file_health.py)：空文件与首/中/尾三点抽样全零占位识别。
- 打开前拦截并提示"空占位文件，已阻止打开"；上一个/下一个视频导航自动跳过占位并提示跳过数量。
- 实测：右1 的 mb00236~mb00260（25 个）、海康的 hiv00438~hiv00470（33 个）均被正确识别；正常文件不受影响。

### 2.5 修复：乐橙相邻文件的分段时钟续接

`eb3cdeb`（标签 `fix/dahua-segment-clock-20260826`）

- 新增 [dahua_segment_clock.py](../cowmata_tailring/media/dahua_segment_clock.py)：已钉住状态下切换相邻乐橙文件时，用"与内容时长互相印证"的 mtime 差计算新片段的精确墙钟起点（`align_method = "segment_clock"`），九轴定位到新文件真实起点；校验不通过自动回退旧的"冻结当前位置"逻辑。
- 实测：imou00024→00025 前向差值 = 1 988 000 ms，与文件 24 内容时长 0.0% 偏差。

### 2.6 修复：乐橙回调媒体交接的稳定性

`8fd5928`（标签 `fix/dahua-callback-stream-handoff-20260826`）

- 替换/关闭回调式播放流前增加 250 ms 解码线程静默期与失败取消机制，避免 libVLC 原生访问冲突导致的崩溃。
- 实测：连续 8 次远距离跳转 + 换文件再跳 2 次共 10 次，无崩溃。

### 2.7 修复：海康未校验时长的隔离（本次最新改动）

`21d9c2c`（标签 `fix/hikvision-duration-gate-20260826`，详见 [change-records/20260826-08-hikvision-duration-validation-gate.md](../docs/change-records/20260826-08-hikvision-duration-validation-gate.md)）

- 问题现象：切换海康文件时进度条右侧短暂显示 08:33:41 之类的原始值，约半秒后跳回 00:19:15 真实值；扫描失败时垃圾值永久保留。
- 修改：非 Dahua 的 MPEG-PS 文件打开后进入"时长未校验"状态——公开时长固定为 0、`is_seekable()` 为 False、内部同步定位排队；校验成功后一次性发布校正时长；**校验失败时冻结**（暂停、清除待执行跳转、拒绝播放与跳转、状态栏持久提示），不再回退不可信时长；后台线程捕获全部异常，杜绝"永远停在正在校验"。
- 独立复测（真实文件 hiv00023，清空缓存）：时长发布序列 `0 → 0 → 1,155,872 ms`，全过程无 30,821,470 ms；模拟 ffprobe 缺失时 `duration=0、is_seekable=False、play()=False、set_time_ms()=False`；缓存命中即时恢复。

### 2.8 基线：拖动提交与早期跳转门控（checkpoint `6c7af15`）

- 进度条新增 `seekCommitted` 信号（拖动松手时提交）；乐橙拖动过程中跳转防抖（避免长 GOP 解码跳转风暴）；早期跳转等待时长的门控标志，避免迟到时长截断已排队的九轴同步定位。

## 3. 验证情况

| 项目 | 结果 |
|---|---|
| 完整测试 | **166 项全部通过**（覆盖时长仲裁、后备探测、占位识别、分段时钟、回调交接、时长门控及待闭合区间交互） |
| Ruff | 通过 |
| 真实窗口（HWND）跳转复测 | 暂停跳转：右1/海康 0 ms 误差瞬时确认，乐橙 0.78~1.7 s；播放中跳转三种格式均精确落点、时钟平滑 |
| 时长交叉验证 | 乐橙 2 010 000 ms（帧扫描与 mtime 双源印证）、右1 1 169 317 ms、海康 958 364 ms / 1 155 872 ms（按文件） |
| 九轴钉住方程 | 不依赖时长，钉住操作数值自洽（详见审计文档） |

> 注意：原审计文档 [video-playback-compatibility-audit.md](../docs/video-playback-compatibility-audit.md) 第 3 节的"每次拖动卡 8 秒、落点偏 2~3 秒"结论是在 headless 引擎（`--vout=dummy`）下测得的假象，真实窗口复测已推翻，未做代码修改。请以 [playback-remediation-status.md](../docs/change-records/20260826-07-playback-remediation-status.md) 与本文为准。

## 4. 已知限制（沿用至本版本）

1. 右1/海康缓存未命中时仍依赖可用的 ffprobe（内置无 ffprobe 后备只覆盖乐橙）。
2. VLC 查找路径仍依赖候选目录；换机器时应通过 `VLC_HOME` 配置本地安装目录。
3. 打开/关闭视频及乐橙每次跳转有约 250 ms 的界面静默期（换取回调媒体交接稳定性的有意取舍）。
4. 7 个头部损坏的 hiv 文件未被专门分类，打开仍为黑屏（占位文件已可跳过，损坏文件不行）。
5. 乐橙拖动进度条期间无实时画面预览（松手才跳转）。
6. 历史审计文档中的本地盘符路径可能已过期；复测时应以当前部署环境的只读数据路径为准。
7. `cowmata_tailring/__version__` 已随本更新提升为 `2.2.0`。

## 5. 回滚方式

每个修复有独立本地标签，可单独回滚（使用 `git revert <标签>`，禁止 `git reset --hard`）：

| 标签 | 内容 |
|---|---|
| `fix/dahua-mtime-arbitration-20260826` | 时长仲裁修复 |
| `fix/dahua-probe-fallback-20260826` | 乐橙后备探测 |
| `fix/zero-video-placeholders-20260826` | 占位文件识别跳过 |
| `fix/dahua-segment-clock-20260826` | 分段时钟续接 |
| `fix/dahua-callback-handoff-20260826` | 回调交接稳定化 |
| `fix/hikvision-duration-gate-20260826` | 海康时长门控 |

基线回滚点：`checkpoint/pre-mtime-arbitration-20260826`、`checkpoint/pre-hikvision-duration-gate-20260826`。

## 6. 建议

- 发布时在 GitHub 打 `v2.2.0` 标签并使用 [release-notes-v2.2.0.md](release-notes-v2.2.0.md) 作为 Release 正文（版本号与 CHANGELOG 的 `## [2.2.0]` 条目已同步）。
- 发布前应确认所有说明均不包含本机盘符或私有数据路径。
