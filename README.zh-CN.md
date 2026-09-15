<p align="center"><img src="assets/brand/pro-wordmark.svg" width="360" alt="COWMATA Pro™"></p>

# COWMATA Pro™ 3.8

**整理原始数据，加快人工标注，构建完整数据集。** 奶牛多视角录像、连续九轴与 PPG 的离线 Windows 工作台。

[下载 3.8](https://github.com/zxq309/COWMATA-Pro/releases/tag/v3.8.1) · [实测图文教程](https://zxq309.github.io/COWMATA-Pro/operator-guide-380.html) · [English](README.md) · [项目简介](docs/project/overview.md)

![实际标注界面，使用受控演示数据](docs/images/guide380/view-B.png)

## 一条工作流程

| 顺序 | 菜单 | 完成什么 |
|---|---|---|
| 1 | 文件 | 打开原始数据或已有工程，继续工作 |
| 2 | 数据准备 | 端侧下载；选择牧场与视频总目录，勾选视角后归类 |
| 3 | 标注与复核 | 对齐视频与九轴/PPG；快捷键标注、自动候选、修改原有标签 |
| 4 | 数据集构建 | 原始 Raw 与 Label 配对；行为、产犊、发情、怀孕、疫病五类完整数据集 |
| 5 | 健康与繁殖 | 保留研究入口，查看已接入状态；未接入模型不输出预测 |
| 6 | 帮助 | 图文教程、版本和更新、版权与商标说明 |

多视角归类并行传输，解码与 OCR 限制资源占用。已归类且身份未变化的文件直接复用，支持暂停续做。独立工作窗口可从 Windows 任务栏最小化和恢复；实时记录保持选中行。

自动候选需要人工核对。复核修改原标签及时间范围，不新增重复事件；完整数据集持续增量更新，标签修改保留内部历史，不再导出整套时间戳目录。历史“采食”记录保留，不占用新增快捷键。

## 下载安装

- **安装包**：运行 `COWMATA-Pro-3.8.1-Setup.exe`，选择位置，完成后打开桌面的 **COWMATA Pro™** 快捷方式。
- **便携包**：解压整个 ZIP，在 `COWMATA-Pro-3.8.1-Portable` 文件夹中运行 `COWMATA.exe`。
- **源码包**：供开发与审查，包含代码、测试和图文教程。完整离线环境使用便携包。

无需配置 Python。采集数据放在软件目录之外。启动直接进入软件，网络或代理故障不阻断离线标注。3.7 及更早版本请直接下载安装包或完整便携包升级一次。

Release 仅提供源码 ZIP、便携 ZIP、安装 EXE 三个附件；教程已随包提供。

## 完整项目与验证

三个旧仓库的介绍、训练与评估方法、温度/活动量证据、产品素材及完整历史统一保存在本仓库。[整合入口与来源](docs/project/repository-consolidation.md) · [3.8 更新说明](docs/release-380.md)

当前五个模型在精简运行环境中与原环境输出一致；这是运行一致性验证，不是现场预测准确率验证。端侧下载协议已用本地 HTTP 服务测试，实际远端服务器仍需连通验收。健康与繁殖未注册的任务没有占位预测。

开发检查：`python -m pip install -e ".[dev]"`，随后执行 `pytest`、`ruff check cowmata_tailring tests`。

COWMATA Pro™ 为产品商标标识。当前标注源码保留 MIT 许可；公司品牌与历史研究材料按各自权利声明使用，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
