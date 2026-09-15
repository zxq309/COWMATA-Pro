# 项目与历史资料整合

COWMATA Pro™ 是统一维护的项目仓库。日常操作按“下载与归类 → 标注与复核 → 数据集”进行；行为识别产生供人工复核的候选。健康与繁殖保留研究入口，未接入模型的任务明确显示状态。

## 合并方式

原标注仓库直接更名，保留问题、发布版本和完整提交历史。另外三个仓库的完整 Git 历史进入本仓库的历史标签，并合入主分支祖先记录。下面的链接均指向 **COWMATA-Pro** 内部的固定提交，不依赖已删除的旧仓库。

工作目录只维护当前版本。历史程序、旧训练检查点、旧配置与旧网页不会加载进标注软件，也不会增大便携运行环境；需要研究复现时，可从本仓库提取固定历史版本。

| 原仓库 | 保留的有用内容 | 当前入口 |
|---|---|---|
| cowmata | 产品介绍、尾环现场素材、总体框架、接口与研究路线 | [固定历史版本](https://github.com/zxq309/COWMATA-Pro/blob/468737081316e616780275836d0ae0448bc95438/README.zh-CN.md) |
| cowmata-tailring | 连续九轴训练与推理、按牛划分、评估约束、数据契约和原始模型 | [固定历史版本](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/README.zh-CN.md) |
| cowmata-risk | 温度和活动量证据模块、集成契约、原实验说明与验证记录 | [固定历史版本](https://github.com/zxq309/COWMATA-Pro/blob/7983ff237541683b66ffb8c54b53548a49ff9c90/README.zh-CN.md) |

## 当前实现与历史研究的关系

- 温度与活动量组件在 `assets/dataset_recipes/`，由当前数据集工作流调用。活动量核心文件与旧风险仓逐字节相同；温度组件以本工具已修订的实现为准，不用历史代码覆盖它。
- 当前五个事件模型在 `assets/event_models/20260906/`；兼容模型加载器在 `cowmata_tailring/model_runtime/`。独立研究仓的训练、评估与历史权重仍完整保留，可单独复现，不自动替换当前候选模型。
- 原始数据与人工标签是不同层；候选、推断的日期、温度/活动量证据均不能自动变成人工确认标签。
- 历史产犊融合、发情、怀孕和疫病方向不代表已交付可验证预测模型。保留原证据边界，不把旧展示页面当作当前软件实测截图。

## 研究资料

- [数据契约](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/docs/DATA_CONTRACT.md)
- [按牛评估与指标](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/docs/METRICS.md)
- [原始数据获取与完整性](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/docs/DATA_ACCESS.md)
- [训练与推理使用方法](https://github.com/zxq309/COWMATA-Pro/blob/00e5660fbcbdbea01e79eb5f9f589ae52374ab2b/docs/QUICKSTART.zh.md)
- [温度/活动量集成约定](https://github.com/zxq309/COWMATA-Pro/blob/7983ff237541683b66ffb8c54b53548a49ff9c90/docs/INTEGRATION.md)
- [软件验证与适用边界](https://github.com/zxq309/COWMATA-Pro/blob/7983ff237541683b66ffb8c54b53548a49ff9c90/docs/VERIFICATION.md)
- [项目路线图](https://github.com/zxq309/COWMATA-Pro/blob/468737081316e616780275836d0ae0448bc95438/docs/ROADMAP.md)
- [产品与现场展示资料](https://github.com/zxq309/COWMATA-Pro/blob/468737081316e616780275836d0ae0448bc95438/showcase/index.html)

## 开发者取用

```bash
git clone https://github.com/zxq309/COWMATA-Pro.git
cd COWMATA-Pro
# 在独立目录检查历史研究，不替换当前主工作目录。
git worktree add ../cowmata-recognition-history legacy/cowmata-tailring/latest
git worktree add ../cowmata-risk-history legacy/cowmata-risk/latest
```

原始仓库的许可与署名保留在同目录的 NOTICE 文件中。历史公司研究代码、模型和素材不因整合而改为 MIT。来源提交与文件数见 [provenance.json](provenance.json)。
