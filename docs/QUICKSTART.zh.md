# 行为识别快速开始

## 快速开始

### 1. 克隆并创建环境

```bash
git clone https://github.com/zxq309/cowmata-tailring.git
cd cowmata-tailring
conda env create -f environment.yml
conda activate cowmata
```

从 [PyTorch 官方选择器](https://pytorch.org/get-started/locally/) 安装与目标机器匹配的 PyTorch 构建，然后在不替换该构建的前提下安装本项目：

```bash
python -m pip install -e . --no-deps
# 深度学习主机额外安装：
python -m pip install -e ".[deep]"
```

### 2. 验证克隆

```bash
pytest tests/test_contracts.py tests/test_pipelines.py   # 48 个测试，无需 torch
cowmata check-env --device cpu                            # 无 torch 也可运行
pytest tests/test_torch_contracts.py                      # 模型契约，需要 torch
```

### 3. 运行内置 60 秒演示

演示无需私有 1.29 GB 监督缓存：

```bash
cowmata predict \
  --cache-key demo_session_60s \
  --data-root examples/demo_data \
  --out runs/demo
```

预期行为：

- 2 Hz 下 120 个稠密预测点；
- `runs/demo/` 下两个 CSV 文件；
- 取决于配置阈值的零个或多个合并事件候选。

## Python API

```python
from cowmata import COWMATA

model = COWMATA("weights/deploy/gbdt_full.joblib")
result = model.predict(
    "<cache_key>",
    project="runs/predict",
    threshold=0.5,
)

print(result.dense.head())
print(result.candidates)
print(result.dense_path)
```

模型对象加载一次，即可对多个缓存会话进行预测，而无需重新加载序列化包。

## CLI 工作流

```bash
# 校验会话元数据、标签、缓存契约与奶牛级划分。
cowmata check-data --root .

# 读取每个本地缓存数组作为更强的完整性检查。
cowmata check-data --full-cache-scan

# 写出结构化数据集诊断报告。
cowmata diagnose --out runs/diagnostics

# 采集前估算缓存占用。
cowmata plan-storage --cows 200 --days 7

# 奶牛分组 k 折划分，验证集与训练集奶牛不相交。
cowmata make-splits --folds 5

# 从原始 JSON + 标签重建 schema-2 缓存。
cowmata build-cache --annotations ... --calibration-manifest ... --output-root ...

# 手工特征表（离线或因果窗口）。
cowmata build-features --samples ... --session-cache ... --out ... --offline

# 在奶牛不相交划分上训练 GBDT 并写入逐事件阈值。
cowmata train-gbdt --feature-table ... --backend xgboost --device cuda

# 在一个折上训练多阶段时序模型。
cowmata train --labels ... --cache-root ... --splits ... --fold 1 --out runs/fold1

# 从稠密预测构建人工复核队列。
cowmata mine --predictions runs/... --events URINATION,MOUNTED_BY --out runs/review_01

# 检查 CPU 或 CUDA 执行。
cowmata check-env --device cpu --precision fp32
```
