# 心率决策特征（heart-rate-decision-1.0.0）

尾根 PPG 心率用于产犊预测的算法说明。统计依据和评价见 `4.3.4/心率/report/心率统计规律与决策特征报告.md`。

## 模块

| 文件 | 作用 |
|---|---|
| `cowmata_tailring/algorithms/heart_rate_signal.py` | 单次 90 s PPG → 脉率 + 质量等级（`ppg-hr-1.0.0`） |
| `cowmata_tailring/algorithms/heart_rate.py` | 采集/上传时刻、按绑定的因果决策特征 `HeartRateModule`、`as_fusion_features`、`attach_heart_rate` |
| `cowmata_engine/features/heart_rate.py` | `cowmata-decision-feature-1` 插件（综合决策引擎 7 项特征之一，主列 `hr_rise_bpm`） |
| `cowmata_tailring/algorithms/decision.py` | “生成综合证据 / 文件夹滚动预警”调用 `attach_heart_rate` 填写 `heart_rate_bpm` |

## 单次心率

1. 0.6–5 Hz 零相位带通；DC < 2000 → `NO_SKIN_CONTACT`，>1% 饱和 → `SATURATED`。
2. 2 s 分块，幅度超过中位数 3 倍或低于 1/3 判为伪迹，相邻块一并剔除。干净时长 < 35% → `MOTION_ARTIFACT`。
3. 16 s/4 s 窗口计算谐波和谱，40–140 bpm。三路独立校验：收缩峰 IBI、自相关、红光。
4. 等级判定：
   - HIGH：一致率 ≥ 0.7，SQI ≥ 0.30，峰检测和自相关都在 ±4 bpm 内，规则度 ≥ 0.6。
   - MEDIUM：一致率 ≥ 0.5，SQI ≥ 0.20，且至少两路一致。
   - LOW / REJECT：`heart_rate_bpm` 不参与任何特征。
5. RMSSD / SDNN 只在 ≥ 30 个规则 IBI 时给出。

只有 `data` 单通道的固件，用 `data` 作主通道（`primary_channel=red_only`）。

## 时间口径

- 采集时刻 = `time`：`work_mode=6` 固件在采集后约 62 min 才上传，此时 `0 < create_time − time ≤ 3 h`。否则用 `create_time`，并标记 `create_time_upper_bound`。
- 可用时刻 = `max(create_time, update_time, 采集结束)`。
- 任何评估时刻只使用可用时刻 ≤ 评估时刻的采集。

> 注意：`sensor_records.parse_ppg_object` 仍以 `create_time` 作为波形零点，用于标注显示。心率模块不依赖它。

## 决策特征（评估时刻 t，全部因果）

| 列 | 定义 |
|---|---|
| `heart_rate_bpm` | 最近一次合格心率；距 t 超过 90 min 置空 |
| `hr_level_6h_bpm` | (t−6 h, t] 合格心率去日节律后的中位数（≥2 条） |
| `hr_baseline_bpm` | (t−96 h, t−24 h] 合格心率去日节律中位数（≥6 条为临时基线，≥10 条且历史 ≥48 h 为正式基线） |
| `hr_rise_bpm` | level − baseline |
| `hr_robust_z` | rise / max(3, 1.4826·MAD 基线) |
| `hr_instability_bpm` | 近 6 h 合格心率标准差（≥3 条） |
| `hr_slope_24h_bpm_per_h` | 近 24 h Theil–Sen 斜率（≥8 条） |
| `ppg_pulse_quality_6h` | 近 6 h 合格采集比例 |
| `ppg_pulse_quality_drop` | 上项减去 (t−72 h, t−6 h] 的合格比例 |
| `prv_rmssd_ratio` | 近 6 h RMSSD 中位数 / 基线 RMSSD 中位数 |

日节律 `CIRCADIAN_BPM` 取自本场产前 >48 h 的本牛内偏差：12 时 −4.4 bpm，17–23 时 +3 bpm。
`Config(circadian_bpm=...)` 可替换为其他 24 点曲线；决策数据集按牛留一生成（每头牛用其余牛全部合格记录估计，不读台账）。

## 综合决策插件 `cowmata_engine/features/heart_rate.py`

- `SPEC.columns`（7 列）：`heart_rate_bpm, hr_level_6h_bpm, hr_rise_bpm, hr_instability_bpm, hr_slope_24h_bpm_per_h, ppg_pulse_quality_6h, ppg_pulse_quality_drop`；
  `primary=hr_level_6h_bpm`（引擎 z72 即“较本牛 72 h 基线”），`derivations=1h/6h/d24/z72/slope6h`（模块已去日节律，不再派生 circ）。
  `hr_baseline_bpm / hr_robust_z / prv_rmssd_ratio` 仍由算法模块输出，但不进决策表（前者无区分力，后两者与 rise 冗余或 AUC≈0.5）。
- `extract_series` 对一个绑定的**每个** 10 min 窗口都输出一行：从首份采集的采样窗口到末份采集到达服务器的窗口，断档也保留；
  窗口值为窗口终点时服务器已收到的采集得到的状态（`available_epoch_ms = end_epoch_ms`），算不出为 `None`，
  `coverage = min(1, 近 6 h 合格采集数 / 5.4)`。
- `rows_from_measurements(..., history=...)`：更早的同绑定采集只作基线上下文，不扩展输出时段。

质量状态：`NO_DATA` → `STALE_DATA`（>3 h 无采集）→ `LOW_COVERAGE` → `INSUFFICIENT_HISTORY` → `PROVISIONAL` → `VALID`。只有 `VALID` / `PROVISIONAL` 输出 rise/z/PRV 与证据等级。

证据等级：

| 等级 | 规则 | 24 h 内命中 | 产前 >48 h 误报 |
|---|---|---|---|
| `ELEVATED` | rise ≥ 8 bpm | 70% | 0.48 次/牛·天 |
| `STRONG` | rise ≥ 8、6 h 离散度 ≥ 6、合格率变化 ≤ −0.15 | 20% | 0.046 次/牛·天 |

`calving_probability` 恒为 `None`，概率由综合决策模型给出。

## 实测规律（扬大_高邮牧场，43 578 次采集，304 头）

- 合格率 62%；合格心率中位数 82.6 bpm，IQR 74–91；逐牛中位数 71–100 bpm（P5–P95）。
- 本牛内较产前 7–4 天：
  - 产前 84–72 h 为 +1.7 bpm；
  - 产前 72 h 起约 +3 bpm；
  - 最后 24 h 为 +4.5 bpm（配对中位数 +6.0 bpm，82% 的牛上升，p=1.4×10⁻⁸）。
- 最后 24 h：近 6 h 离散度从约 7 升到 10.8 bpm，合格脉搏波比例从 0.62 降到 0.51，RMSSD 从约 48 降到 42 ms。
- 仅用心率特征、按牛交叉验证的 AUC：6 h 0.70，24 h 0.67。心率是融合输入，不是独立报警。

## 复现

```
runtime\python.exe 4.3.4\心率\scripts\s01_measure.py            # 逐次心率 + HeartRate 记录树
runtime\python.exe 4.3.4\心率\scripts\s02_decision_features.py  # Features/heart_rate/windows.csv / 逐小时评价表
python            4.3.4\心率\scripts\s03_statistics.py          # 统计、评价与图表（需 matplotlib）
```

测试：`tests/test_heart_rate_433.py`。
