# Windows 下载、源码开发与发布

## 普通用户

从本仓库 Releases 下载安装版 `COWMATA-3.1.0-rc.1-Setup.exe`，或者免安装版 `COWMATA-3.1.0-rc.1-Windows-x64.zip`。前者只在安装时解压一次；后者完整解压后运行 `COWMATA.exe`。不需要 Python、pip、VLC 或 CUDA 工具包。完整应用目录约 2 GB，实际压缩体积以 Release 附件为准。

`COWMATA.exe` 是 Windows 启动程序，调用相邻的私有 Python/Qt 运行库；不是把几 GB 内容每次重新解压的单文件 Python 程序。不要只复制 EXE。Windows 10/11 自带的 .NET Framework 4.x 用于小型启动器，主界面仍在包内 Python 3.13 上运行。

安装路径必须是新建/空的软件目录，不能覆盖工程或已有安装；可选择新版本目录并保留旧版回退。卸载按打包清单删除软件文件，只移除空目录，保留用户新增的工程、日志和其他文件。不要将真实数据放入软件的 runtime/vendor 等组件目录。

文件目前没有商业代码签名证书。Windows 可能提示未知发布者或 SmartScreen；请核对仓库、版本和 SHA-256，不关闭杀毒软件或全局安全保护。不确定时停止运行并联系维护者。

## 为什么源码仓库很小

Git 只跟踪应用/算法源代码、测试、文档、模型配置/身份清单和必要的小型界面资源。`.gitignore` 排除运行库、第三方二进制、权重、缓存、录像、私有九轴和构建产物。发布前再按明确的文件清单核验，不依赖一次笼统的 `git add .`。

大型 Setup/ZIP 上传为 Release 附件，不提交进 Git 历史。GitHub 自动产生的 `Source code.zip` 不包含这些附件。依据 [GitHub Releases 说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)，单个附件须小于 2 GiB；发布脚本会核验本次实际体积。普通用户不需要为了节省 Git 仓库容量自行配环境。

## 完整离线源码运行

1. 克隆源码并切换到与便携 ZIP 相同的 tag。
2. 下载该 tag 的完整便携 ZIP 和 `.zip.sha256`，在本地核对 SHA-256 后解压到另一个空目录。
3. 将便携包中的 `runtime/`、`model_runtime_20260906/`、`vendor/` 复制到源码根目录；不要覆盖现有开发环境。
4. 将便携包 `assets/ocr/ppocrv6_medium/` 中的 `.onnx` 文件，以及 `assets/event_models/20260906/` 下的 `.pkl/.joblib` 权重，按原相对位置放入源码。清单/模型代码使用相同 tag，不混合不同版本。
5. 在源码根目录运行 `runtime\python.exe portable_start.py`。运行库不会进入 Git 提交。

若只修改纯 Python 逻辑，也可用自己的 Python 3.10+：`python -m venv .venv`，再用该环境 `python -m pip install -e ".[dev,ocr]"`。原生播放还需要上述私有 VLC/FFmpeg；离线 OCR 权重及事件旧运行库仍须匹配。旧 `[model]` 可选依赖仅对应旧单视频模型辅助入口，不替代五类新候选包。

事件序列化权重使用经对照的 Python 3.8.10/sklearn 0.24.1 私有进程；3.8 已停止维护，只运行审查过的本地模型，不是任意 pickle 的安全沙箱。不要直接升级其依赖或替换权重。训练与新模型发布另做版本化兼容验证。

## 维护者构建

1. 准备上述运行库与权重，固定版本见 `requirements-portable.txt`、`requirements-events-20260906.txt`。不修改模型原代码/文件 hash。
2. 用开发环境运行全量 `pytest`、`ruff check cowmata_tailring tests`，并用私有运行库做实际原片/模型/多路压力测试。
3. `powershell -File scripts/build_launcher.ps1` 生成根目录 `COWMATA.exe`；编译器是 Windows 自带 Framework64 的 csc，已有输出时拒绝覆盖。
4. `python scripts/build_portable.py --out <全新输出目录>` 生成便携文件夹、ZIP、清单和 SHA-256。
5. 使用 NSIS 3.12 便携编译器：`python scripts/build_installer.py --package <便携目录> --compiler <makensis.exe> --version 3.1.0-rc.1 --out <全新Setup.exe>`。
6. 核验复制包的离线自检、EXE/BAT 原生启动、八路播放/历史回看、安装及卸载保留用户文件，再发布。源码测试不替代包内测试；未过门槛不得发布为稳定版。

NSIS 官方入口是 [nsis.sourceforge.io](https://nsis.sourceforge.io/Download)。本次编译器 ZIP 的 SHA-256 为 `56581f90db321581c5381193d796fffcf2d24b2f8fed2160a6c6a3baa67f2c4f`；经原始包哈希核验的 Chocolatey 缓存副本解决了官方镜像下载中断。NSIS 编译工具不随最终应用发给普通用户。

## GPU 与流畅度

原生 VLC 自动尝试 D3D11VA 等硬解；实际测试日志须出现所选 GPU 的解码记录才能声称硬解已生效，配置选项本身不是证明。主路优先时只有主路全速，辅路是带时间差提示的低频预览；暂停回到各路原片精确 PTS，预览不能作为真值。

原片帧 RAM 缓存预算按物理内存估算，最多 512 MiB/查看窗口；保存的是已核验原片帧，不预先复制海量录像。主界面进程请求“高于正常”调度优先级，仍可被 Windows 抢占；后台模型继续低优先级且限资源。应用不修改全局电源计划、不强占所有内存、不设置 High/实时优先级。权限不允许时正常优先级继续工作。读机械盘/云盘、解码驱动冷启动、原片异常封装仍可能造成等待，具体已测指标与失败记录随 Release 提供。
