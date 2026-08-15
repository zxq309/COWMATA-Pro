# HierarchicalMSResTCN 模型卡

> 历史归档：该固定中心窗权重已不再是自动标注工具默认模型。当前默认使用
> `牛尾环IMU_20260815_整理包/复现实验/development_all/fold_0_None_20260815_111619/best.pt`
> 的新版 `CausalMultiTaskTCN`。

## 模型

- 文件：`best_model.pt`
- 架构：HierarchicalMSResTCN，304,256 参数
- 输入：50 Hz、九轴 + 三组三轴模长 + gap，共 13 通道
- 输出：2 Hz；四类主体 softmax + 六类事件 sigmoid
- 最佳监督 epoch：18；`smoke_test=false`
- 训练硬件：RTX 4070 Laptop，CUDA bf16，batch 8

checkpoint 自包含：模型权重、标签顺序、预处理版本、train-only median/MAD、温度、事件阈值、最短持续时间和训练元数据。

## 工具侧分层适配（2026-08-14）

上面的“四类主体 softmax”描述的是权重本身，不是升级后标注工具的最终输出。工具将该历史权重作为证据模型使用：`FEEDING/STANDING/WALKING` 合并为直立证据，站卧由已知初始站立状态、起卧事件、迟滞和最短驻留状态机构成；`WALKING` 再作为直立子状态覆盖输出。因此工具不会自动生成 `FEEDING` 建议。

该适配使旧权重遵循当前标签语义，但不等于新 `CausalMultiTaskTCN` 已经训练。选择训练代码生成的因果 checkpoint 时，工具会自动切换到因果流式推理；排便、抬尾、甩尾仍只作为研究性扩样候选。

## 训练数据

- train：89 个有效会话，88.998 h；
- val：10 个有效会话，10.000 h；
- SSL：train + 274 个有效未标注会话；val 未进入 SSL；
- 10 个坏的 untrained JSON 被拒绝并记录在 `rejected_sessions.json`。

主体状态未标注点为 IGNORE。事件区间外默认作为负例，模型元数据记录 `event_negative_scope=full_session`；若标注协议并非全会话穷尽，必须重新训练为 `annotated_body`。

## 验证结果

主体状态仅在人工标注覆盖点计算：

| 类别 | Precision | Recall | F1 | 支持点（0.5 s） |
|---|---:|---:|---:|---:|
| FEEDING | 0.8830 | 0.9978 | 0.9369 | 7,841 |
| LYING | 1.0000 | 1.0000 | 1.0000 | 14,888 |
| STANDING | 0.9900 | 0.8593 | 0.9200 | 7,683 |
| WALKING | 0.9202 | 0.9122 | 0.9162 | 683 |
| **Macro** |  |  | **0.9433** |  |

事件逐点结果使用 checkpoint 中的校准/阈值：

| 类别 | 阈值 | Precision | Recall | F1 | AP | 独立验证事件数 |
|---|---:|---:|---:|---:|---:|---:|
| DEFECATION | 0.50 | 0.7625 | 0.9531 | 0.8472 | 0.9638 | 1 |
| URINATION | 0.90 | 0.7225 | 0.7500 | 0.7360 | 0.7564 | 7 |
| LYING_DOWN | 0.75 | 0.6182 | 0.4595 | 0.5271 | 0.5410 | 5 |
| STANDING_UP | 0.90 | 0.6574 | 0.7717 | 0.7100 | 0.7766 | 6 |
| TAIL_RAISED | 0.50 | 0.1471 | 0.2273 | 0.1786 | 0.0992 | 2 |
| TAIL_WAGGING | 0.50 | 0.0000 | 0.0000 | 0.0000 | 0.0025 | 1 |
| **Macro** |  |  |  | **0.4998** | **0.5232** |  |

`DEFECATION / TAIL_RAISED / TAIL_WAGGING` 少于 3 个独立验证事件，未拟合专属温度和阈值，保持 $T=1$、阈值 0.5。完整逐点、segment F1@IoU、ECE、Brier、TP/FP/FN 和分设备结果见 `validation_metrics.json`。

## 使用边界

这是一份验证集最优模型，不是独立测试集结果。val 同时用于 early stopping、校准和阈值选择，因此数字是 validation estimate。训练/验证包含相邻日期和部分相同设备，不能据此保证新牛、新牧场或新硬件的性能。

`TAIL_WAGGING` 当前不可用，`TAIL_RAISED` 很弱；它们需要补充跨牛、跨日、跨设备的独立事件。新设备 `546C50CA07DE` 没有主体状态验证覆盖，事件 macro-F1 约 0.138，显示跨设备仍是主要风险。不要把高主体 F1 外推成所有事件均可靠。

## 使用

```powershell
python .\training_code\predict.py `
  --model .\models\20260811_172711\best_model.pt `
  --imu "D:\path\new.json"
```

Qt：双击 `training_code\run_gui.bat`，选择本目录的 `best_model.pt`。
