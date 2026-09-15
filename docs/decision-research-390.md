# 决策算法的研究依据与使用范围

检索日期：2026-09-16。未发现适用于所有乳牛健康任务、所有传感器和所有牧场的统一“最新决策算法标准”。3.9 将近期产犊研究使用的梯度提升树加入可复现训练流程，并保留决策树与随机森林比较。

1. 日本 NARO：Evaluation of Approaches to Cattle Calving Prediction Based on Vaginal Temperature: Machine Learning-Versus Threshold-Based Models（2026）。使用 XGBoost 比较温度产犊预测方法。其温度来自阴道传感器；不能把论文性能或阈值直接移用于本应用尾环温度。https://pmc.ncbi.nlm.nih.gov/articles/PMC13494690/ ，https://pubmed.ncbi.nlm.nih.gov/42625501/
2. MmCows: A Multimodal Dataset for Dairy Cattle Monitoring，NeurIPS 2024 Datasets and Benchmarks。Purdue、UW-Madison、Iowa State 团队提供同步多模态奶牛监测数据与基准；支持以时间对齐、模态可用性和真实标签组织数据，但本身不提供本牧场可直接部署的产犊模型。https://papers.nips.cc/paper_files/paper/2024/file/6d8f3f71b22f9d2e9320d7bdb73acea7-Paper-Datasets_and_Benchmarks_Track.pdf
3. XGBoost 官方稳定版本 3.4.1（2026-08-15），部署使用官方 JSON 模型持久化，保留 SHA-256 和特征清单。https://pypi.org/project/xgboost/3.4.1/ ，https://xgboost.readthedocs.io/en/stable/tutorials/saving_model.html
4. scikit-learn 的 GroupKFold 按组隔离训练与验证；本应用对产犊决策按牛分组，填补缺失值仅在训练折学习。https://scikit-learn.org/stable/modules/cross_validation.html#group-k-fold

实现边界：训练窗口仅用预测时点前已可获取的信息，行为上下文产生 20 秒延迟；未来产犊时间只构造训练标签。不把无人审核的事件候选当作负例。不声称已完成跨牧场前瞻验证或概率校准。HR 和 SpO2 暂不从未经标定的光学振幅推造。训练报告保留原始输入引用、分组、评价曲线、缺失情况及全局重要性。
