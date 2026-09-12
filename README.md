# COWMATA 3.5.0

Windows multiview video and nine-axis annotation workstation. Setup, portable ZIP and clean source are published in [Releases](https://github.com/zxq309/cattle-tail-ring-annotator/releases/latest).

- Confirm a farm, category and one recording day. The first waveform and video appear while remaining indexing continues in the background.
- Open one IMU recording with its day’s videos, or pair one video with a specific IMU file across drives. Single-file Save always asks for a destination.
- Recursively organize mixed inputs or add videos to an already classified IMU project. Stream clocks are preferred and checked with image OCR.
- Keep one dated working annotation; construct behavior datasets with ear-tag filenames and shared cow-disjoint splits. Unknown and conflicting truth are excluded from training.
- Resource filenames contain only start seconds. Dataset filenames add ear tag and device; behavior starts keep milliseconds. Internal timestamps and hashes remain intact.
- PPG and pregnancy stages are represented explicitly; PPG waveform parsing remains reserved.

Launch `COWMATA.exe` after extracting the complete portable folder, or install the setup. Private Python, Qt, VLC, FFmpeg, OCR weights and reviewed event models are bundled. The inherited startup update check requires network access; annotation can continue offline after startup.

[中文说明](README.zh-CN.md) · [Illustrated guide](docs/daily-project-guide.html) · [Workflow](docs/legacy-dataset-workflow.md) · [3.5 release notes](docs/release-350.md)

Source and tests are in `cowmata_tailring` and `tests`. The source archive excludes runtimes, model binaries, customer recordings and generated caches; binary dependencies are supplied by the portable release.
