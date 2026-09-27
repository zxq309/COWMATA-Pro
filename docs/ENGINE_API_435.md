# COWMATA Engine 4.3.5 API

`decision.predict_windows` 和 `decision.predict_folder` 使用 4.3.5 多提前量清单。
训练输入窗口固定为 `T0-12h..T0`；结果 `schema` 为 `cowmata-decision-result-4.3.5`，
每行 `risk` 同时包含 `1h`、`2h`、`3h`、`6h`、`12h` 键：

```json
{"risk": {"1h": 0.12, "2h": 0.19, "3h": 0.24, "6h": 0.31, "12h": 0.37}, "risk_primary": 0.37,
 "warning_level": "关注", "hours_to_calving_p50": 5.6}
```

输入特征必须带 `available_epoch_ms`，且不得晚于决策时刻。引擎只使用决策时刻之前已
收到的数据；长基线派生列在 4.3.5 推理层会被置空。行为识别接口和标签类别不变。
