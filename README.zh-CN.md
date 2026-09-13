# COWMATA Annotator 3.5.3

奶牛多视角录像与连续九轴数据的整理、同步标注和复核工具。Windows安装包与便携包包含Python、Qt、VLC、FFmpeg及离线OCR模型，无须额外配置运行环境。

[下载安装包](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.5.3/COWMATA-Annotator-3.5.3-Setup.exe) · [下载便携包](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.5.3/COWMATA-Annotator-3.5.3-Portable.zip) · [在线完整图文教程](docs/quick-start-illustrated.md) · [56页PDF手册](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.5.3/COWMATA-3.5.3-Manual.pdf) · [English](README.md)

3.5.3修复了“列表有目标路径、实际文件尚未归类”的流程问题。选好来源后直接点击“一键执行归类”：多个录像同时识别，完成一条就真实复制或移动一条，核对后显示“已归档”。

- 批量输入视角01～08的多个目录，每个来源递归搜索子目录。“仅补视频”和“九轴与视频一起整理”共用流程。
- 视频先按后缀筛选；支持的流内绝对时间优先，没有时读取首帧，必要时自动尝试开头邻近帧并回推开始秒。无法确定的文件保留、跳过，不阻断其他文件。
- 目录按 `Video/日期/视角/开始时间` 保存，视频不绑定单头牛。九轴维持设备、耳标和现场记号规范；既有标签引用随九轴改名更新，标注内容保持一致。
- 关闭整理窗口会停止后台任务，主界面能继续保存退出。更新显示独立进度，先启动通过校验的新版，再后台清理旧版；更新后这一次启动不重复联网检查。
- 保留单日工程、单文件跨盘配对、母标签、历史复核、证据导出与按牛划分的数据集构建。PPG波形解析仍为预留能力。

**开始使用：** 关闭旧版后运行安装包，或完整解压便携ZIP后打开 `COWMATA.exe`。从旧更新器迁移到本版，建议本次直接运行3.5.3安装包。点击“仅预览”时不会复制或移动文件。

首帧命名与后续帧回推不代表整段同步已经核验；自动候选与演示标签不作为现场效果或研究真值。速度实测、校验范围见[本版说明](docs/release-353.md)。
