# 项目说明的统一与历史来源

当前项目的完整自述统一维护在 [README](../../README.md)，包括实现流程、源码环境、标注数据集、行为训练与识别、综合证据与决策。新手操作见[图文教程](../quick-start-390.md)。

## 三个历史来源

这些固定提交均位于 COWMATA-Pro 自身的 Git 历史，不依赖已删除仓库。

| 来源 | 整合的说明 | 固定历史自述 |
|---|---|---|
| cowmata | 尾环监测目标、现场采集与视频复核、组件分工、研究方向 | [README](https://github.com/zxq309/COWMATA-Pro/blob/468737081316e616780275836d0ae0448bc95438/README.zh-CN.md) |
| cowmata-tailring | 连续九轴、稳定标签与候选、训练和推理、数据契约与分组评估 | [README](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/README.zh-CN.md) |
| cowmata-risk | 温度/活动辅助证据、身份绑定、质量时效、缺失和状态恢复 | [README](https://github.com/zxq309/COWMATA-Pro/blob/7983ff237541683b66ffb8c54b53548a49ff9c90/README.zh-CN.md) |

本次只整合说明和实现思路；不搬入旧源码包、算法包、安装包、训练权重或旧展示网站。

## 实现的继承与变化

- 当前数据工作链延续“现场采集 → 人工确认 → 行为与风险证据 → 现场复核”的解释方式，并在一个桌面工作台提供入口。
- 3.9 统一下载目录、Raw/Label 配对、身份与原始哈希。旧缓存格式和旧 CLI 命令不作为当前输入前提。
- 当前六行为为起立、卧倒、努责、排尿、抬尾、甩尾；Motion 与 PPG 独立训练，手动导入对应模型。程序不自带训练好的行为权重。
- 旧识别文档的 GBDT/TCN 和全部按牛验证属于历史研究；当前一般事件按原始记录、努责及产犊决策按牛分组，报告需按实际口径解读。
- 旧活动量核心为现有数据集辅助组件的来源。温度辅助组件已有修订，历史评分模型外置；没有模型时仍保留原始温度观察值。
- 旧风险项目仅有独立温度与活动证据；3.9 新增多指标机器学习决策流程。旧探索回放数字不用于宣传当前预测精度。
- 心率、血氧、发情、孕期、疫病方向的未验证能力不因说明整合而变成已交付预测模型。

## 方法资料索引

- [原始数据契约](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/docs/DATA_CONTRACT.md)
- [分组验证与指标](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/docs/METRICS.md)
- [温度与活动的历史集成约定](https://github.com/zxq309/COWMATA-Pro/blob/7983ff237541683b66ffb8c54b53548a49ff9c90/docs/INTEGRATION.md)
- [历史软件验证范围](https://github.com/zxq309/COWMATA-Pro/blob/7983ff237541683b66ffb8c54b53548a49ff9c90/docs/VERIFICATION.md)
- [历史总体接口提案](https://github.com/zxq309/COWMATA-Pro/blob/468737081316e616780275836d0ae0448bc95438/docs/INTERFACES.md)
- [当前数据格式](../data-contract-390.md)、[当前决策研究依据](../decision-research-390.md)、[当前验证记录](../validation-390.md)

## 来源与许可

三个历史仓库的完整历史已归档为 `legacy/cowmata/latest`、`legacy/cowmata-tailring/latest`、`legacy/cowmata-risk/latest`。提交与文件数见 [provenance.json](provenance.json)。

历史材料保留原公司的 NOTICE 和专有条款；当前主程序的 MIT 许可不重新许可这些材料。具体区分见根目录 [NOTICE](../../NOTICE)。
