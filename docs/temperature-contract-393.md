# 温度数据约定 3.9.3

共享约定：cowmata-temperature-1；值字段 data；单位 celsius；时间 unix_ms；旧编码 base64_int16_le_x0.01。

每个独立温度文件包含 configs、cow_id、create_by、create_time、data、device、log、uid、update_by、update_time、vbat。保持下载服务器提供的原始值。data 必须为单个有限数值，不能是数组、字符串、布尔或 ADC 对象。缺少的信息不猜测。

早期 Motion 的 temperature 是小端有符号 int16 序列。第 i 个温度桶的估计时间为 create_time + first_frame_elapsed_ms + (i + 0.5) × duration_ms / count，四舍六入五成双取整到毫秒。v0/v1 首帧偏移为 0；v2 使用首帧 uint32 计数。目录和文件名采用北京时间，JSON 中保留毫秒。

提取文件额外保留 _temperature：schema、unit、source_kind、time_basis、source_sha256、source_uid、source_create_time、sample_index、sample_count、sample_interval_ms、raw_int16、sample_id。time_basis=imu_bucket_midpoint_estimate 明确为估计时间；不会把按桶分配的估计时刻声称为设备单点实测时钟。原始 Motion 仍是复核传感值与时序质量的依据。

数据集保持原 Raw/Label 命名，以维持标注配对；独立 Temp 使用 Temp/日期/设备-耳标-现场记号/时间戳.json。相同观测复用已有文件；真实同秒冲突进入问题清单并保留原件。清单的 temperature.contract 与综合决策模型的 temperature_contract 使用相同字段。

原始内嵌温度与其提取副本不是两个独立观测。母标签决策导出按来源哈希去重，融合证据按牛、设备、现场记号、采样时间及数值去重。update_time 保留接收时间含义，晚到值不会追溯进入更早的决策。

纯行为九轴识别模型的输入特征不因拆出温度而变化。温度辅助模型继续接受历史 Motion，同时接受规范标量 Temp；若模型显式声明不兼容温度契约，则拒绝读取。模型文件缺少契约时仅作为历史摄氏度版本兼容。
