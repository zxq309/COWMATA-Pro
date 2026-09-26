# 血氧（SpO2）算法 · 4.3.3

## 模块

| 文件 | 内容 |
|---|---|
| `cowmata_tailring/algorithms/spo2_signal.py` | `analyse_signal(red, ir, fs, accel=None)` / `analyse_ppg(PPGData)`：逐采集计算血氧、R、脉率、PI，附质量等级和拒收原因。版本 `spo2-ratio-gated-1` |
| `cowmata_tailring/spo2.py` | `cowmata-spo2-1` 记录协议：`ppg_timing`、`ppg_spo2_record`、`read_spo2_record`、`save_spo2_record`、`export_spo2_sources`、`find_spo2_sources`、`external_spo2` |
| `cowmata_tailring/algorithms/spo2_features.py` | 因果的 `SpO2Module.evaluate(now)`；`attach_spo2(rows, ppg_records)` 在综合证据中填写 `spo2_percent` 等列 |
| `cowmata_engine/features/spo2.py` | `cowmata-decision-feature-1` 插件，key 为 `spo2`。每个绑定从首份采样窗口到末份到达窗口逐 10 min 连续输出；窗口值是窗口起点时的因果状态，算不出为 None；波形无法解析的采集仍作为"无效采集"保留在时间轴上 |

## 计算步骤

1. DC 取 2 s 滑动中位；AC 为 `x/DC−1` 经 0.7–3.5 Hz、3 阶 Butterworth 零相位带通后的结果。
2. 按 8 s 窗、2 s 步进切窗，逐窗门控（阈值见 `GATES`）：脱落或饱和、接触不良、加速度或 DC 漂移、红外周期性、脉率范围、灌注范围、红光/红外相关系数 ≥0.8、红光/红外脉率一致、R 在 0.2–1.2。
3. R = rms(AC红)/rms(AC红外)。SpO2 = −45.060R² + 30.354R + 94.845（Maxim RD117），其中 R 先取 max(R, 0.337)，保证曲线单调。
4. 一次采集取合格窗 R 的中位数，至少 3 窗。SpO2 在 70–100% 之外时判为 implausible，不输出。
5. 脉率取红外自相关的第一个主峰，要求 ≥最大峰的 80%，避免快心率被识别成一半。
6. 记录时间：采样起点优先用设备 `time`（与 create_time 相差 0–3 h 时采用），否则用 create_time。可用时刻 = max(update_time, create_time, 采样结束)。

## 输出语义

- `data`（SpO2 %）使用厂商的人体曲线，未经牛血气标定，只用于本牛自身比较。R 与曲线无关。
- 质量不合格的采集不写入 SpO2 记录；在 `export_spo2_sources(..., audit=[])` 中记为 rejected，并附原因。
- 在 PPG 页签中，`multi_sensor.oximetry_note` 显示当前 PPG 的血氧、合格份数、脉率和灌注指数。

## 验证

- `tests/test_feature_spo2_433.py`，共 11 项。
- 牧场数据的统计与决策评估见 `4.3.3\血氧\README.md`。
