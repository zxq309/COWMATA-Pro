# 躺卧占比算法 lying-occupancy-2（4.3.4）

实现代码：`cowmata_tailring/algorithms/lying_occupancy.py`；决策特征插件：`cowmata_engine/features/lying_ratio.py`（特征键 `lying_ratio`）。

## 替换了什么

4.3.2 的 `posture.occupancy()` 只能在两次起卧事件之间推断姿态，存在两个问题：

- 没有观察到起卧事件的时段一律算"未知"；
- 尾环脱落的时段也被当成躺卧。

新算法给每个有效秒都估计姿态，并用起立/卧倒检测结果约束状态切换。旧函数仍然保留，供旧版证据流程使用；综合决策引擎改用 `lying_ratio`。

## 处理步骤

1. **逐秒摘要**：用 `summarize_motion` / `summarize_motion_file` 把 50 Hz 数据汇总到每秒。
2. **连续网格**：同一头牛的多份记录按 epoch 拼成连续 1 Hz 序列（`build_grid`）。
3. **信号层**（`signals`）：
   - 每小时做一次因果零偏校正；零偏过大且暂时无法拟合时，该段记为未知。
   - 佩戴检测（`wear_mask`）：尾抬角持续 >75°，或角速度持续 <1 dps，判为未佩戴。
4. **站立参考方向**（`references`）：每小时取前 24 h 活动最高 15% 的秒，求重力方向中位数。
5. **姿态层**：`posture_features` 计算 28 个旋转不变特征，由数值森林给出 P(躺卧)。
6. **起卧层**：`transition_candidates` 取 ±25 s 方向变化 ≥8° 的峰；`transition_features` 计算 19 个特征；三分类逻辑回归输出 P(起立) 和 P(卧倒)。
7. **状态层**（`state_layer`）：两状态 HMM。前向滤波，再对每个 10 min 窗口做 150 s 固定滞后平滑；最短躺卧/站立段为 60 s。

## 输出（每个 10 min 绝对时间窗）

- **通用字段**：`start_epoch_ms`、`end_epoch_ms`，`available_epoch_ms`（= end + 15 min），`coverage`（有效且已佩戴的秒数占比）。
- **特征列**：
  - `lying_ratio`：主特征；
  - `lying_ratio_hard`、`lying_confidence`；
  - `lying_down_count`、`standing_up_count`；
  - `lying_bout_minutes`、`standing_reference_ok`。

有效秒少于 60 s 时，所有特征列输出 None。

## 模型文件

模型文件不放在源码树里。查找顺序：

1. 环境变量 `$COWMATA_LYING_RATIO_MODEL`；
2. `<model_home>/experiments/LYING_RATIO/lying_ratio/`。

该目录需包含 `posture.json`、`transition.json`、`hmm.json` 和 `lying_ratio_params.json`。加载时逐个核对 sha256。

## 验证摘要

按牛分组 10 折交叉验证，真值为视频标注的逐秒站/卧，共 27 头牛、155 k 秒。

- 逐秒准确率 0.986。
- 10 min 躺卧占比平均绝对误差 1.7 个百分点，96% 的窗口误差 ≤5 个百分点。
- 犊牛完全娩出前后 ±8 h：逐秒准确率 0.943，窗口误差 6.8 个百分点。
- 卧倒召回 0.96，起立召回 0.92（±60 s）。
- 对照：4.3.2 旧算法即使输入人工标注的起卧事件，窗口误差也有 22.5 个百分点。

研究报告、数据集与复现脚本见交付目录 `4.3.4/躺卧占比/`。
