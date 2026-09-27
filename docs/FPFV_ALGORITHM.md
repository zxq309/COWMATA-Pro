# 胎儿首个部位首次可见（FETAL_PART_FIRST_VISIBLE, FPFV）标准算法

版本：算法 `fpfv-stage2-onset-1`，特征 `fpfv-tail-hold-3`，首个模型 `fpfv-20260925-v1`
代码：`cowmata_tailring/algorithms/fpfv.py`、`cowmata_tailring/algorithms/fpfv_features.py`
训练与验证流水线：`E:\zxq_copilot_20260925\fetal_part_first_visible\fpfv_pipeline.py`
模型：`F:\科牧特_模型\行为识别\FPFV\<版本>\fpfv_model.json`（附 validation_report.json）

## 1. 数据

- 标签：`COWMATA_Behavior_Dataset/FetalPartFirstVisible` 共 25 条。
  - 16 条是视频精确对时（`legacy_imported`，证据“两者”）。
  - 9 条是 `needs_review`：原“露蹄”记录，时间被取整到 xx:29:59 或 xx:59:59，与 CFE 恰好相差 30 或 60 min。这 9 条不能当精确时刻使用，只作 ±30 min 的弱标签。
  - 另外关联了 CFE 25 条、努责 222 段、助产 1 条。
- 连续流：3866 份一小时 Raw（全部类别去重），共 66 台设备。按设备用 epoch 拼接，构成连续序列。
  - 背景约 147.6 牛·日：已剔除所有产犊时段 ±3 h 和 FPFV ±12 h。
- 读盘：`ProcessPoolExecutor(max_workers=16)`，16 路同时读盘，3866 份约 4.5 min 完成。之后增量缓存，文件大小或 mtime 改变才重读。

## 2. 规律（每次增加标签后由 `fpfv_pipeline.py law` 重新统计，写入 `law_statistics.json`）

| # | 规律 | 证据（16 精确 / 9 待复核） |
|---|---|---|
| L0 | **尾环坐标**：尾巴下垂时重力≈传感器 −Y（66/66 台一致）。尾抬角 `elev = arccos(−u_y)` 不受尾环绕尾转动影响。卧地时尾巴平放，表现为 u_y≈0 且陀螺≈0.1 dps | 设备重力方向聚类 |
| L1 | **抬尾频率骤增**：以本牛 24 h 尾姿为基线，每小时“抬尾上穿”次数在事件时的中位为 1.05 /min，背景中位为 0.04 /min（事件处于背景分位 0.98）；事件前 90–30 min 已升至 0.31 /min | 15/16 精确、8/9 待复核超过背景 P95，合计 **23/25（92%）符合** |
| L2 | **第二产程持续抬尾**：FPFV 之后尾巴被抬起并保持（30–85°，或比本牛基线高 ≥20°，且不静止），伴随节律性努责抬尾。持续平台的起点 − FPFV：中位 −0.6 min，IQR −5.3~+2.4 min。平台终点≈犊牛娩出 | 未来 5 min 相对持续抬尾比例超过背景 P95：10/16 精确、6/9 待复核；平台起点落在 ±15 min 内：12/16 |
| L3 | **娩出时间**：CFE 在 FPFV 后中位 13.3 min（P10 6.2 min，P90 31.3 min） | 14 对 |
| L4 | **辅助线索**：尾部传感器温度在 FPFV 附近下降（中位 37.9→37.2 ℃），娩出后回升；娩出后 10–30 min 活动量上升（站起、舔犊） | 群体曲线 |

与文献一致：尾部上举是最稳定的产犊信号（Krieger 2018、Miller 2020、Higaki 2022）；单一线索的精确率很低，需要多线索融合（Aoki 2023）。调研全文见 `fetal_part_first_visible/research_notes.md`。

## 3. 算法

1. **逐秒摘要**：每秒计算加速度均值与标准差、陀螺标准差、jerk，温度按分钟记录。
   - 每台设备按 epoch 拼接成连续序列；缺口保持为 NaN，任何窗口都不跨缺口插值。
2. **特征**：共 184 维，按 10 s 步长计算。
   - 过去窗口：30 s、2 min、5 min、15 min、1 h、3 h。
   - 镜像的未来窗口，以及“未来 − 过去”的对比。
   - 相对本牛 24 h 基线的抬尾角、持续抬尾比例、抬尾频率、活动量。
   - 尾抬角 5 min 自相关（节律努责）和温度变化。
   - 剔除绝对温度、绝对基线这类因设备而异的量。
3. **第二产程状态模型**：`HistGradientBoostingClassifier`，导出为数值 JSON 树（`numeric-gbdt-1`），推理时不使用 pickle。
   - 正例：[FPFV, min(CFE, FPFV+45 min)]。
   - 待复核标签：[−15, +30] min 作弱正例包，权重 0.5，其余 ±60/90 min 忽略。
   - 未标注的产犊时段一律忽略，不当背景。
4. **解码**：概率先做 2 min 居中平滑。
   - 概率 ≥ 阈值且持续 ≥2 min 的段为一个 episode，间隔 <20 min 的段合并。
   - 上升沿为 episode 前 30 min 内首次达到“onset_frac × 峰值”的时刻，取作 FPFV 近似点。
   - 每 12 h 最多一个候选，输出 `approximate_point`，并要求视频确认。
   - 工作点（阈值 0.7，onset_frac 0.7）由嵌套验证中最常选中的组合决定。

## 4. 实测验证

验证协议：
- 按牛留一，共 25 折。背景设备轮流分配到各折，同一头牛的所有时段只出现在同一折。
- 阈值与 onset_frac 只在其余牛上选定（嵌套）。
- 在全部连续时段上评估，计入产前时段产生的假警报。

| 指标 | 新算法 fpfv-20260925-v1 | 第一版（event-shape-1+RF+score_events，同一协议） |
|---|---|---|
| ±5 min 命中 | 6/16（38%，95%CI 18–61%） | —— |
| ±10 min 命中 | 7/16（44%，23–67%） | —— |
| **±15 min 命中** | **9/16（56%，33–77%）** | 每 12 h 取最强 1 个：2/16，1.84 次/牛·日；阈值 0.95：1/16，0.05 次/牛·日 |
| ±30 min 命中 | 12/16（75%，51–90%） | —— |
| 找到产程 episode | 13/16 | —— |
| 假警报 | **0.075 次/牛·日**（11 次 / 147.6 牛·日） | 不限候选数时 1295 次/牛·日（15/16“命中”是靠密集候选覆盖） |
| 待复核标签 ±45 min | 4/9 | —— |
| 命中者的中位绝对误差 | 6.5 min | —— |

**结论**：新算法在误报相当的条件下，±15 min 命中率约为第一版的 5–9 倍，并且把候选数从“每小时数十个”降到“约每两周一个”。

**局限**：
- 目前精确事件只有 16 个，置信区间很宽。
- 3 头牛完全没有被检出：2DF4 尾巴不形成持续平台（努责为短脉冲）；2E2B 的持续抬尾平台只有约 3 min；2E36 从 FPFV 到娩出仅 1.7 min。
- 另有 07E5（−61 min）、2E24（+23 min）、2E2F（−21 min）找到了产程，但时刻定位偏差 >15 min。
- 24203 的 FPFV 落在记录起点（t=0），疑似被截断，应复核。

## 5. 随数据增加的迭代（“精调规律”）

```
cd E:\zxq_copilot_20260925\fetal_part_first_visible
..\4.2.7\COWMATA-Pro-4.2.7-Portable\runtime\python.exe fpfv_pipeline.py all --workers 16 --version fpfv-YYYYMMDD-vN
```

依次执行 scan → extract（16 路增量）→ features（仅重算变动设备）→ law（重算规律统计）→ validate（LOCO 报告）→ train（导出新版本模型，旧版本保留）。

新版本只有同时满足以下两点才替换旧版本：
- `validation_report.json` 中 ±15 min 命中率不低于上一版；
- 假警报率 ≤ 0.1 次/牛·日。

提升优先级：
1. 把 9 条待复核标签按视频复核为精确时间，这是收益最大的一项。
2. 补标产犊记录中漏标的努责。
3. 接入【努责】会话导出的逐秒努责分数，前提是覆盖全部 3866 份记录，否则缺失本身会泄漏标签。
4. 接入【犊牛全部娩出】检测结果作为回溯约束：FPFV ≈ CFE − 13 min。

## 6. 调用

```python
from cowmata_tailring.algorithms import fpfv
model = fpfv.load_model(r"F:\科牧特_模型\行为识别\FPFV\fpfv-20260925-v1\fpfv_model.json")
events, trace = fpfv.detect_records(motion_records_of_one_device, model)   # MotionRecord 列表
# events[i]: point_epoch_ms / start_epoch_ms / end_epoch_ms / score / requires_video_confirmation=True
```
