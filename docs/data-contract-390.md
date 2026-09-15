# 3.9 数据输入约定

牧场/类别/{Motion,PPG,Temp,Video}/YYYY-MM-DD/完整设备编号-五位牛耳标[-现场标号]/原始 JSON。
类别兼容产犊、正常、发情、疫病、怀孕及孕早/中/晚期、未分类、待核对。
设备编码为 12 位十六进制；CSV 内 4 位简写补 546C50CA；其他长度只报告核对，不推测。
现场标号支持中文、字母和数字，可省略；多个牛重复佩戴同一设备按采集时间和原始牛号分辨，不全局绑定。

- Motion：device、create_time 毫秒、version 0/1/2、imu Base64，分别每帧 18/20/22 字节。保留辅助温度和配置字段。
- PPG：device、create_time、data/ir_data/red_data 光学 Base64，可选 imu_data；携带 sample_rate_hz 或 configs 中 pulse_led_sr/pulse_led_avr/pulse_sample_time。配置不全时报告，不能猜时间轴。
- Temp：device、create_time，摄氏 temperature_c 或明确摄氏 data 标量。原始 ADC 或不明单位不用于决策。
- 本地缓存、校验、台账引用存入 .edge-download；不作为原始素材索引。
- 已标注数据按原模态镜像保存到 标注工程/Motion 或 PPG。数据集构建导出 Raw/*_raw.json 与 Label/*_label.json，原始内容 SHA-256 关联。
- 行为训练读取 Raw/Label；识别直接读取下载目录，按内容去重，不要求先建空标签。
- 决策训练使用综合证据与精确产犊时间 CSV；牛号、未来产犊时间和标签不作为预测输入特征。

不同模态的采样率和时间坐标独立。现场视频对齐继续使用一次对齐，原始采集时间不会替代人工视觉校准。


现场记录目录：`F:\牛舍\_现场记录`，只保存三个业务 CSV。同步锁、校验状态和 CSV 备份放在 `%LOCALAPPDATA%\COWMATA-Pro\site-records`。模型库：`F:\科牧特\_模型`。路径使用代码格式显示，避免转义造成多余反斜杠。
