# 产犊综合决策与预测引擎（4.3.4）

代码：`cowmata_engine/decision/`（数据集 `dataset.py`、结局 `labels.py`、模型库 `models.py`、训练评价 `train.py`、推理 `predict.py`）。
界面：健康与繁殖 → 产犊（`cowmata_tailring/ui/algorithms/decision_ui.py`）。对外接口见 [ENGINE_API_433.md](ENGINE_API_433.md)。

## 1. 数据流

```
原始 Motion / PPG / Temp JSON
   │  7 个特征插件（cowmata_engine/features/*.py，各特征会话维护），16 路并行
   ▼
Features/<key>/windows.csv      绝对对齐 10 min 窗口，缺失为空，available_epoch_ms 保证因果
   │  按牛对齐到 10 min 网格 → 整点决策时刻
   ▼
派生趋势（每个特征列 × 6）：1h 均值、6h 均值、d24（相对前 24 h 中位数）、z72（相对前 72 h 稳健 z 分数）、
slope6h（6 h 斜率）、circ（相对前 3 天同时段的日节律残差）；另加时段 sin/cos
   │  只使用决策时刻之前已可用的数据；基线额外滞后 6 h，避免产程本身污染基线
   ▼
decision_table.csv  + 产犊结局（牧场登记 + 视频“犊牛完全娩出”标签，二者 12 h 内以视频为准）
   │  只标注产前 10 天内的时刻；产后和远离产犊的时刻不当作负例
   ▼
模型库 × 按牛分组交叉验证 → 校准 → 阈值 → 事件级评价 → 部署模型（6/12/24/48 h + 剩余时间）
```

## 2. 七项特征（各会话交付，4.3.4 实测）

| 特征 | 版本 | 主列 | 已验证的产前规律（来源会话） |
|---|---|---|---|
| 温度 | temperature-2 | temp_median_c | 产前 24 h 较本牛 72 h 基线下降；z72 单列 AUC 0.78，全部输入中最强 |
| 心率 | heart-rate-decision-1.0.0 | hr_level_6h_bpm | 由尾部 PPG 估计，窗口质量不足时为空 |
| 血氧 | spo2-ratio-gated-1 | perfusion_index_percent | 尾部反射式 PPG，SpO₂ 绝对值未经血气标定，只作相对趋势 |
| 活动量 | activity-2 | vedba_mean_g | 产前 6–12 h 不安、动作增多 |
| 躺卧占比 | lying-occupancy-2 | lying_ratio | 姿态 + 起立/卧倒同步 HMM，10 min 误差 22.5→1.7 pp；产前躺卧减少、娩出前 2 h 起卧最频繁 |
| 努责占比 | straining-ratio-2 | straining_ratio | 只对 ≤3 h 临产有区分力；娩出前最后 1 h 60 min 均值 0.107，远离产犊 98% 为 0 |
| 角速度频谱熵 | gse-1 | gyro_spectral_entropy | 产前尾部转动功率升高、频谱熵降低 |

## 3. 决策算法库

| 键 | 算法 | 说明 |
|---|---|---|
| expert_rules | 专家规则打分 + Platt 校准 | 按文献方向对各特征主列偏离本牛基线加权（努责用 1 h 占比），只学 2 个参数 |
| baseline_deviation | 本牛基线偏离度 | 各主列 z72 的均方根（类 Hotelling T²），不需要方向 |
| logistic | L2 逻辑回归 | 标准化 + 缺失指示，系数可解释 |
| decision_tree | CART 决策树 | 深度 ≤5，叶 ≥40 |
| random_forest | 随机森林 | 300 棵 |
| extra_trees | 极端随机树 | 300 棵 |
| adaboost | AdaBoost（SAMME，深度 2 树） | 150 轮 |
| xgboost | XGBoost | 原生缺失值，记录逐轮训练/验证损失，推理可用精确 TreeSHAP 解释 |
| stacking | 堆叠集成 LR+RF+XGB → LR + 等渗校准 | 内层按牛交叉拟合生成元特征 |
| （附加）time_to_event | 分位数梯度提升 | log(距产犊小时) 的 P10/P50/P90 |

所有模型保存为数值 JSON（树结构 / 系数 / XGBoost JSON），推理不加载 pickle；训练报告中的分数由与部署相同的推理代码计算。

## 4. 验证协议

- 按牛分组 K 折（默认 5 折）：同一头牛不会同时在训练与验证中。
- 概率：留出分数经交叉拟合等渗回归校准；部署校准器在全部留出分数上拟合。
- 阈值：Youden 指数（灵敏度 + 特异度 − 1）最优，与正例比例无关。
- 事件级：连续 ≥2 个整点超过阈值才算一次预警；产犊检出 = 提前量窗口内出现预警（只统计提前量窗口有 ≥50% 数据的产犊）；误报 = 提前量以外的预警次数 / 牛·天。
- 只在每个特征自己的计算时段内训练；`coverage.*` 不作为模型输入（避免“有没有算这个特征”泄漏结局）。
- 选择最优算法：先比 PR AUC，再比“产犊检出率 − 0.1 × 误报/牛·天”。

## 5. 真实数据结果（扬大高邮牧场，2026-09-26）

见 [DECISION_RESULTS_433.md](DECISION_RESULTS_433.md)（由真实数据训练与特征消融自动整理）。

## 6. 指标取舍

保留：6/12/24/48 h 校准概率、预警等级、剩余时间 80% 区间、驱动特征、覆盖率；
训练侧 ROC/PR AUC（含按牛自助 95% CI）、Brier、Log loss、ECE、灵敏度、特异度、精确率、NPV、F1、MCC、混淆矩阵、
产犊检出率、提前量、误报/牛·天、剩余时间 MAE 与区间覆盖率、学习曲线、特征组置换重要性。

去掉（做不到或不可信）：分钟级产犊时刻误差（登记时间常取整）；跨牧场泛化精度（仅一个牧场）；
难产/死胎/犊牛性别/产后疾病（无真值）；深度时序模型作为主模型（有结局产犊仅约百余次）；
未经校准的“风险分数”；心率/血氧的绝对临床值（尾部 PPG 未经标定，只用相对本牛基线的变化）。

## 7. 模型部署（模型不随安装包分发）

源码包、便携包、安装包都不含训练好的模型（与行为模型相同的外置原则）。模型库根目录为“行为识别模型目录”（环境变量 `COWMATA_ALGORITHM_HOME`，或在软件设置中选择），其上一级为产犊决策目录：

| 模型 | 放置位置 | 也可用环境变量 |
|---|---|---|
| 躺卧占比（posture / transition / hmm / lying_ratio_params.json） | `<模型库>/experiments/LYING_RATIO/lying_ratio/` | `COWMATA_LYING_RATIO_MODEL` |
| 努责占比（straining_bundle.json / straining_ratio_params.json） | `<模型库>/experiments/STRAINING_BOUT/straining_ratio/` | `COWMATA_STRAINING_RATIO_MODEL` |
| 综合决策（decision.json + model-*.json） | 任意目录，在“产犊 → 导入决策模型…”选择 `decision.json` | — |

本机已部署：`F:\科牧特_模型\行为识别\experiments\…` 与 `F:\科牧特_模型\产犊决策\models\decision-433-xgboost-24h\`。
缺少某个特征模型时，该特征在结果中标为缺失，其余特征照常参与决策。
