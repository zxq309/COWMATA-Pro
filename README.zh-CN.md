# COWMATA Annotator 3.6.0

奶牛多视角录像与连续九轴数据的归类、下载、同步标注和复核工具。Windows 安装包与便携包包含 Python、Qt、VLC、FFmpeg 和离线 OCR 模型。

[安装包](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.6.0/COWMATA-Annotator-3.6.0-Setup.exe) · [便携包](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.6.0/COWMATA-Annotator-3.6.0-Portable.zip) · [源码包](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.6.0/COWMATA-Annotator-3.6.0-Source.zip) · [发布说明](docs/release-360.md) · [English](README.md)

- **工具 → 数据归类**：单窗口支持 JSON 与视频一起归类，或已有 JSON 补充视频。已归类且文件身份未变化的材料直接复用，不重复识别、哈希或复制；目标缺失时补齐。
- **暂停与继续**：保留原任务和已验证的复制断点。实时 CSV 记录每份文件的状态、耗时与复用情况，可随时打开。
- **日期与目录**：优先读取录像流内时钟，补充画面 OCR 标点容错。视频不因没有对应九轴日期而漏归；无法确定的时间明确保留待处理，不伪造日期。
- **工具 → 端侧数据下载**：手动、自动或定时下载 Motion/PPG JSON，支持失败重试、迟上传补下、多牧场配置和可选 SSH 隧道。[下载说明](docs/edge-download.md)
- **纯净发行**：不携带本机密钥、个人设置、下载记录、日志、缓存或现场试验数据。标注、历史复核、证据导出及数据集构建能力保留。

完整解压便携 ZIP 后运行 `COWMATA.exe`，或运行安装包选择一个新的软件目录。采集数据请放在软件目录之外。

自动、定时下载需要程序保持运行；3090 原始库只提供 Motion，PPG 需要提供脉搏数据的服务端。真实远端服务未连接验收，协议与控制流程使用本地 HTTP 模拟服务验证。

归类日期不等于整段同步已经核验；自动候选与演示标签不能作为现场效果或研究真值。[既有标注操作指南](docs/quick-start-illustrated.md)
