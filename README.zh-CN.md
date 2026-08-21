<div align="center">

<a href="https://www.cowmata.com/"><img src="assets/brand/cowmata-logo.svg" alt="COWMATA" width="300"></a>

# COWMATA 牛尾环标注工具

**面向奶牛行为与分娩研究的人机协同视频—九轴 IMU 标注工作台**

[![CI](https://github.com/zxq309/cattle-tail-ring-annotator/actions/workflows/ci.yml/badge.svg)](https://github.com/zxq309/cattle-tail-ring-annotator/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)](#环境要求)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[English](README.md) · [简体中文](README.zh-CN.md) · [主工程算法仓库](https://github.com/zxq309/cowmata-tailring)

</div>

![COWMATA 牛尾环标注工具：同步显示牛舍视频与九轴 IMU 波形](assets/screenshots/annotator-overview.jpg)

<p align="center"><sub>真实联调截图：59 分 59.859 秒、179,378 个采样点、50 Hz 九轴记录与奶牛视频同步加载；原始素材仅保留在本地，不上传 GitHub。</sub></p>

## 项目简介

COWMATA 牛尾环标注工具是一款 Windows 桌面工作台，用于同步复核奶牛视频与连续九轴尾环 IMU 数据。它在一个界面内提供共用时间轴、v4 标注协议、安全工程保存、逐条复核和科研数据导出。

> [!IMPORTANT]
> 本仓库负责**数据标注与人工复核**；模型训练、实验评估和当前算法工程基线位于 [COWMATA Tail-Sensor Intelligence 主工程](https://github.com/zxq309/cowmata-tailring)。两个仓库互为配套，可直接跳转使用。

### 核心能力

- **视频—九轴共时间轴** —— 播放、定位、缩放并同时检查加速度计、陀螺仪和磁力计九个通道。
- **先对齐、后标注** —— 视频与传感器时间轴完成钉住前，正式标注保持锁定，避免无声错位。
- **内置 v4 协议** —— 15 项行为/分娩标签，另含一个不参与训练的同步锚点。
- **人控模型辅助** —— 模型只生成候选项；未经过人工确认，不会成为正式标签。
- **可追溯输出** —— 稳定机器码、显示名称、绝对时间戳、来源字段与导出前结构校验。
- **中英双语** —— 界面语言仅影响显示，不会改写落盘标签码或历史数据。

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 及以上
- 系统已安装 [VLC media player 3.x](https://www.videolan.org/vlc/)，用于视频播放
- Git
- 可选：FFmpeg/ffprobe，用于特殊监控视频的探测或转封装流程

应用主体采用 Python/Qt，但当前视频集成与正式测试以 Windows 为主。

## 快速开始

打开 PowerShell：

```powershell
git clone https://github.com/zxq309/cattle-tail-ring-annotator.git
cd cattle-tail-ring-annotator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
cowmata-annotator --mode basic
```

不依赖命令行入口时，可用等价模块命令：

```powershell
python -m cowmata_tailring --mode basic
```

需要模型辅助复核时：

```powershell
pip install -e ".[model]"
cowmata-annotator --mode model-assist
```

Windows 用户也可以先运行一次 `scripts\install.bat`，以后直接双击 `scripts\launch.bat`。需要查看控制台诊断信息时，运行 `scripts\launch-debug.bat`。

## 使用示例

把自己的九轴 JSON 和对应视频放入本地 `examples/` 目录，然后在启动时一次性打开：

```powershell
cowmata-annotator --mode basic --lang zh `
  --json "examples\2026-08-08 10_44_34.json" `
  --video "examples\hiv00102.mp4"
```

本次截图使用的视频为 256 MiB，九轴 JSON 约 5 MiB，二者均已通过 `.gitignore` 排除，不会上传 GitHub。具体见 [examples/README.md](examples/README.md)。

## 使用指南

1. **打开九轴 JSON** —— 核对采样数、频率、时间范围、缺口与九通道真实波形。
2. **打开视频** —— 加载对应录像，并检查工具识别的真实时长。
3. **对齐并钉住** —— 将视频与 IMU 定位到同一可观察事件，再点击“钉住”；完成后才允许正式标注。
4. **创建标签** —— 按标签快捷键；点标签落在当前播放头，区间标签通过拖动波形创建。
5. **复核、保存与导出** —— 校正边界、补充备注、撤销/重做；通过结构校验后保存工程并导出。

工程文件采用原子写入，意外中断不会用半截文件覆盖已有有效工程。

## 快捷键

### 编辑与导航

| 功能 | 快捷键 |
| --- | --- |
| 播放 / 暂停 | `Space` |
| 保存工程 | `Ctrl+S` |
| 撤销 | `Ctrl+Z` |
| 重做 | `Ctrl+Y` 或 `Ctrl+Shift+Z` |
| 删除选中事件 | `Delete` 或 `Backspace` |
| 上一帧 / 下一帧 | `[` / `]` |
| 播放头前后移动 100 ms | `←` / `→` |
| 上一个 / 下一个活动 | `P` / `N` |
| 显示完整信号范围 | `F` |
| 视频全屏 | `F11` 或双击视频 |
| 退出全屏 / 取消待建区间 | `Esc` |
| 校准选中的模型区间 | `Ctrl+E`（模型辅助模式） |

### v4 标签协议

| 快捷键 | 中文标签 | 机器码 | 类型 |
| --- | --- | --- | --- |
| `1` | 站立 | `STANDING` | 区间 |
| `2` | 躺卧 | `LYING` | 区间 |
| `3` | 行走 | `WALKING` | 区间 |
| `4` | 努责首次出现 | `STRAINING_ONSET` | 点 |
| `5` | 努责区间 | `STRAINING_BOUT` | 区间 |
| `6` | 胎膜囊（水囊）首次可见 | `AMNIOTIC_SAC_FIRST_VISIBLE` | 点 |
| `7` | 胎儿首个部位首次可见 | `FETAL_PART_FIRST_VISIBLE` | 点 |
| `8` | 犊牛完全娩出（T0） | `CALF_FULLY_EXPELLED` | 点 |
| `9` | 胎膜完全排出 | `FETAL_MEMBRANES_FULLY_EXPELLED` | 点 |
| `Q` | 抬尾 | `TAIL_RAISED` | 区间 |
| `W` | 甩尾 | `TAIL_WAGGING` | 区间 |
| `E` | 起立过程 | `STANDING_UP` | 区间 |
| `R` | 卧倒过程 | `LYING_DOWN` | 区间 |
| `A` | 排尿 | `URINATION` | 区间 |
| `S` | 排便 | `DEFECATION` | 区间 |
| `0` | 同步敲击锚点 | `SYNC_ANCHOR` | 点，不参与训练 |

## 模型辅助复核

模型文件不纳入本仓库版本管理。通过“模型辅助 → 模型包设置…”选择本地目录：

| 文件 | 是否必需 | 用途 |
| --- | --- | --- |
| `gbdt_full.joblib` | 必需 | 六类事件，并提供姿态/行走回退 |
| `best.pt` | 可选 | `OfflineMultiTaskTCN`，负责站立、躺卧与行走 |
| `inference_config.json` | 可选 | 对包外 JSON 提供传感器换算覆盖 |

为保持模型文件兼容，`xgboost` 固定为 `3.2.0`。模型建议导入后状态为“待复核”，会保留原始类别、边界、得分及后续人工修改记录。完整契约见 [模型辅助标注文档](docs/model-assist.md)；当前训练与评估流程请以[主工程算法仓库](https://github.com/zxq309/cowmata-tailring)为准。

## 输入、工程与导出

| 项目 | 用途 |
| --- | --- |
| 九轴 JSON | 连续加速度计、陀螺仪、磁力计与时间戳数据 |
| 视频 | MP4 及 VLC 支持的其他奶牛录像 |
| 工程 JSON | 对齐关系、源文件、协议、标签、事件、备注与复核状态 |
| 事件 CSV + 元数据 JSON | 含 `label`、`code`、`en` 字段的扁平事件交换格式 |
| 聚合 / BORIS CSV | 兼容点事件与区间事件的汇总格式 |
| 训练样本 CSV | 基于时间戳的多热样本；导出前必须填写真实 `cow_id` |
| IRR CSV | 标注者间一致性报告 |

界面语言不会改写历史数据；`STANDING`、`TAIL_RAISED` 等机器键在中英文会话中保持一致。

## 仓库结构

```text
cattle-tail-ring-annotator/
├── cowmata_tailring/       # 应用、界面、媒体、标注与推理代码
├── assets/                 # 授权品牌素材与 README 实测截图
├── docs/                   # 使用、模型辅助、架构与打包说明
├── examples/               # 仅本地保留的样例数据说明
├── scripts/                # Windows 与 shell 启动脚本
├── tests/                  # 自动化测试
└── .github/                # CI、Issue 表单与 PR 模板
```

## 开发与验证

```powershell
pip install -e ".[dev]"
ruff check cowmata_tailring tests
pytest -q
python -m cowmata_tailring --version
```

截图所示发布候选版本已在 Windows 环境完成真实联调：Python 3.12.13、VLC 3.0.23、真实样例对和自动化测试套件。

## 关联仓库

| 仓库 | 作用 |
| --- | --- |
| [zxq309/cattle-tail-ring-annotator](https://github.com/zxq309/cattle-tail-ring-annotator) | 本桌面标注与人工复核工具 |
| [zxq309/cowmata-tailring](https://github.com/zxq309/cowmata-tailring) | COWMATA 尾部传感器算法、实验与工程基线 |

## 贡献、安全与引用

- 提交 PR 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开提交 Issue。
- 软件引用信息见 [CITATION.cff](CITATION.cff)。
- 可复现缺陷与边界明确的功能建议请提交到 [GitHub Issues](https://github.com/zxq309/cattle-tail-ring-annotator/issues)。

## 团队

- **张相清（Xiangqing Zhang）** —— 杨凌园上园智能科技有限公司 CTO；延安大学
- **张亚龙（Yalong Zhang）** —— 杨凌园上园智能科技有限公司创始人
- **焦腾宇（Tengyu Jiao）** —— 延安大学
- **赵亚晨（Yachen Zhao）** —— 延安大学

## 许可证与品牌素材

源代码按 [MIT License](LICENSE) 发布。`assets/brand/` 内的 COWMATA 名称与 Logo 为公司品牌资产，不因 MIT 许可证而重新授权；详见 [NOTICE](NOTICE) 与 [assets/README.md](assets/README.md)。
