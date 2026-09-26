# COWMATA 算法引擎接口 `cowmata-engine-1`（4.3.4）

`cowmata_engine` 是独立、无 Qt 依赖的算法包，把 **行为识别** 与 **产犊综合决策与预测** 单独拎出来，
供桌面端和外部前端界面统一做数据输入输出对接。桌面端“健康与繁殖 → 产犊”窗口也只通过它调用算法。

```
cowmata_engine/
  api.py            唯一入口 handle(request) -> response（JSON 进、JSON 出）
  __main__.py       命令行：python -m cowmata_engine run request.json -o result.json / serve / info
  server.py         本机 HTTP JSON 服务（仅标准库，默认 127.0.0.1:8765）
  behavior/         行为识别门面：catalog / predict / train
  features/         7 项决策特征插件（心率、血氧、活动量、温度、躺卧占比、努责占比、角速度频谱熵）
  decision/         决策数据集、模型库、训练评价、推理
```

## 1. 调用方式

| 方式 | 用法 |
|---|---|
| Python | `from cowmata_engine.api import handle; handle({"action": "decision.predict_windows", ...})` |
| 命令行 | `runtime\python.exe -m cowmata_engine run 请求.json -o 结果.json`（进度写到 stderr） |
| HTTP | `runtime\python.exe -m cowmata_engine serve --port 8765`，然后 `POST http://127.0.0.1:8765/api/<action>` |

HTTP 细节：`GET /api/info`；`POST /api/<action>` 的 body 为请求字段（不含 action）；
body 加 `"async": true` 时立即返回 `{"ok": true, "job": id}`，用 `GET /api/jobs/<id>` 查询进度（done/total/message）与最终 `response`，
`POST /api/jobs/<id>/cancel` 请求取消。服务默认只监听本机，不做鉴权，请勿直接暴露到公网。

所有响应统一为：
```json
{"api": "cowmata-engine-1", "action": "…", "ok": true, "result": {…}}
{"api": "cowmata-engine-1", "action": "…", "ok": false, "error": "类型: 说明", "trace": "…"}
```
时间一律为 UTC epoch 毫秒；显示北京时间由前端换算（+8 h）。

## 2. 操作一览

| action | 必填字段 | 可选字段 | 返回 |
|---|---|---|---|
| `engine.info` | — | — | 版本、操作、特征键、算法键 |
| `features.catalog` | — | — | 7 项特征模块状态、版本、列、产前规律声明 |
| `features.extract` | `root`（原始 JSON 目录）、`output` | `keys`、`workers` | 每项特征写 `output/<key>/windows.csv` |
| `decision.algorithms` | — | — | 决策算法库说明 |
| `decision.output_fields` | — | — | 推理输出字段中文说明 |
| `decision.build_dataset` | `output` 与 `features_root` 或 `raw_root` 之一 | `ledger`（产犊登记 CSV）、`calving_dataset`（娩出标签数据集）、`keys`、`workers`、`lookback_days` | 决策数据集摘要（含产前规律、单特征区分度） |
| `decision.train` | `dataset`（决策数据集目录）、`output` | `algorithms`、`horizon`（6/12/24/48）、`folds`、`persistence` | 完整训练报告（排行榜、曲线、事件级评价……） |
| `decision.predict_folder` | `folder`、`model`（含 decision.json 的目录）、`output` | `workers`、`features_root` | 逐时刻预测、连续预警时段、数据覆盖 |
| `decision.predict_windows` | `windows`、`model` | — | 前端直接给特征窗口时的预测结果 |
| `behavior.catalog` | — | — | 6 类行为算法能力矩阵 |
| `behavior.predict` | `code`、`source`（Motion JSON）、`model_dir` | `threshold` | 待人工复核的行为候选（不是标签） |
| `behavior.train` | `dataset`（Raw/Label 成对数据集）、`output` | `codes`、`modality`、`cache` | 行为模型套件 suite.json 与评价 |

## 3. 前端直接推送特征：`decision.predict_windows`

```json
{
  "action": "decision.predict_windows",
  "model": "D:/COWMATA Pro/data/models/产犊决策/runs/train-20260926-150000-ab12cd",
  "windows": {
    "temperature": [
      {"cow_id": "23291", "device_id": "0C3D5EA22DD3", "start_epoch_ms": 1787800200000,
       "end_epoch_ms": 1787800800000, "available_epoch_ms": 1787800800000, "coverage": 0.98,
       "temperature_c": 38.41}
    ],
    "activity": [ … ], "lying_ratio": [ … ], "straining_ratio": [ … ],
    "heart_rate": [ … ], "spo2": [ … ], "gyro_spectral_entropy": [ … ]
  }
}
```
- 每个窗口 10 min，`start_epoch_ms` 必须是 600000 的整数倍；列名与 `features.catalog` 中该特征的 `columns` 一致。
- 缺少某项特征时直接不传该键；算不出的值传 `null`，不要传 0。
- 建议每头牛至少带 24 h（最好 72 h）历史，否则基线类特征不可用，结果会标注“参考历史不足”。

## 4. 推理输出（`result.rows[*]`）

| 字段 | 含义 |
|---|---|
| `cow_id`、`devices` | 牛耳标、参与计算的设备 |
| `decision_epoch_ms` | 预测时刻（整点），只使用此刻之前已可用的数据 |
| `risk` | `{"6h","12h","24h","48h"}` 内产犊的校准概率，单调不减 |
| `risk_primary`、`threshold` | 主提前量概率与其预警阈值 |
| `warning_level`、`advice` | 正常 / 关注 / 高度关注 / 临产 / 数据不足，及处置建议 |
| `hours_to_calving_p10/p50/p90` | 预计距产犊小时数（80% 区间）；`predicted_calving_epoch_ms` 为 P50 对应时刻 |
| `drivers` | 前 3 个驱动特征：`feature, title, contribution, z, direction, pushes` |
| `feature_coverage` | 7 项特征近 6 h 覆盖率 |
| `missing_features` | 模型使用但当前缺失的特征 |
| `history_hours`、`history_status` | 本牛可用参考历史 |
| `model_version` | 决策模型版本 |

`result.alert_episodes`：连续 ≥2 个整点超过阈值形成的预警时段（首次超阈、确认、最后预警、峰值、预计产犊时刻、关注截至）。
`result.coverage`：每头牛的数据跨度、最大缺口与特征覆盖率。

## 5. 训练报告（`decision.train` 返回，亦写入 `决策训练报告.json`）

- `leaderboard[*]`：每个算法的 `metrics`（roc_auc 与按牛自助 95% CI、pr_auc、brier、log_loss、ece、threshold、sensitivity、specificity、precision、npv、f1、mcc、confusion_matrix）、
  `events`（event_sensitivity、lead_time_median_h、lead_time_iqr_h、false_alerts_per_cow_day、lead_hist）、`curves`（roc、pr、calibration、risk_by_hours）、`fold_curves`（XGBoost 逐轮 train/valid logloss 与 AUC）。
- `group_importance`：7 项特征的置换重要性；`column_importance`：最终模型单列重要性。
- `learning_curve`、`time_to_calving_eval`（MAE、80% 区间覆盖率）、`per_event`（逐次产犊检出与提前量）、`profile`（各特征产前规律）、`univariate`。

## 6. 特征插件接口 `cowmata-decision-feature-1`

见 `cowmata_engine/features/base.py`：每个模块提供 `SPEC` 与 `extract(source, window_ms=600000)`（或 `extract_series`），
输出绝对对齐 10 min 窗口，`available_epoch_ms ≥ end_epoch_ms + lookahead_ms`，缺失为 `None`，不读取任何标签。
