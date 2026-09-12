# COWMATA 3.4.3

Portable multiview video and nine-axis cattle annotation workstation. Launch `COWMATA.exe` or `START_ANNOTATOR.bat`. Private Python, Qt, VLC, FFmpeg, OCR weights and event models are included. The inherited startup update check requires a network connection; annotation can continue offline after startup.

Version 3.4 imports resources into `farm/category/{Motion,PPG,Video}/acquisition-date`. Categories are selected for each batch. Video clocks are checked against independent image OCR; cross-midnight files are indexed by real coverage and stored under their start date. Same-volume moves preserve file identity, cross-volume copies verify SHA-256, and saved import plans support recovery without overwriting files.

Device-ear-tag-field-mark identities preserve leading zeros and support unambiguous reversed field names. PPG directories and saved/exported fields are reserved; no PPG parser or waveform is supplied yet.

See [Chinese usage](README.zh-CN.md), `使用说明.txt`, and [release notes](docs/release-340.md). Source is in `cowmata_tailring`; regression tests are in `tests`. Historical project readers and task replay remain supported.

3.4.1 fixes Space after selecting speed/behavior, rejects zero-length intervals, selects new annotations, and restores direct label dragging. See [release notes](docs/release-341.md) and [illustrated tutorial](docs/quick-start-illustrated.md).

3.4.2: [旧标签迁移与算法数据集流程](docs/legacy-dataset-workflow.md) · [更新说明](docs/release-342.md) · [图文教程](docs/quick-start-illustrated.pdf)。

3.4.3: [六项修复说明](docs/release-343.md)；摄像头、核验、标签位置、排序与互斥以此版本为准。
