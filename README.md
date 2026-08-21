<div align="center">

<a href="https://www.cowmata.com/"><img src="assets/brand/cowmata-logo.svg" alt="COWMATA" width="300"></a>

# COWMATA Tail-Ring Annotator

**Human-in-the-loop video and nine-axis IMU annotation for cattle behaviour and calving research**

[![CI](https://github.com/zxq309/cattle-tail-ring-annotator/actions/workflows/ci.yml/badge.svg)](https://github.com/zxq309/cattle-tail-ring-annotator/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[English](README.md) · [简体中文](README.zh-CN.md) · [Algorithm repository](https://github.com/zxq309/cowmata-tailring)

</div>

![COWMATA Tail-Ring Annotator showing synchronized barn video and nine-axis IMU waveforms](assets/screenshots/annotator-overview.jpg)

<p align="center"><sub>Real integration test: a 59 min 59.859 s, 179,378-sample, 50 Hz IMU record synchronized with cattle video. Source media stays local and is not committed.</sub></p>

## Overview

COWMATA Tail-Ring Annotator is a Windows desktop workstation for reviewing synchronized cattle video and continuous nine-axis tail-ring IMU data. It combines a shared timeline, protocol-v4 labels, safe project persistence, review tools, and research-ready exports in one interface.

> [!IMPORTANT]
> This repository is the **data annotation and review tool**. Model training, evaluation, and the current algorithm engineering baseline live in [COWMATA Tail-Sensor Intelligence](https://github.com/zxq309/cowmata-tailring). The two repositories are designed to be used together.

### Why this tool

- **Video and IMU in one timeline** — seek, play, zoom, and inspect nine channels without switching applications.
- **Alignment before annotation** — formal annotation remains locked until the video and sensor timelines are pinned.
- **Protocol v4 built in** — 15 behaviour/calving labels plus a non-trainable synchronization anchor.
- **Human-controlled model assistance** — candidates enter a review queue and never become labels without confirmation.
- **Traceable output** — stable machine codes, display labels, timestamps, provenance fields, and validation-aware exports.
- **Bilingual UI** — English and Simplified Chinese affect display only; persisted label codes remain stable.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- [VLC media player 3.x](https://www.videolan.org/vlc/) installed on the system for video playback
- Git
- Optional: FFmpeg/ffprobe for unusual surveillance-video probing or remux workflows

The application source is cross-platform Python/Qt, but the current video integration and test target are Windows-first.

## Quick start

Open PowerShell:

```powershell
git clone https://github.com/zxq309/cattle-tail-ring-annotator.git
cd cattle-tail-ring-annotator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
cowmata-annotator --mode basic
```

The equivalent module command is:

```powershell
python -m cowmata_tailring --mode basic
```

For model-assisted review:

```powershell
pip install -e ".[model]"
cowmata-annotator --mode model-assist
```

Windows users can also run `scripts\install.bat` once and then double-click `scripts\launch.bat`. Use `scripts\launch-debug.bat` when console diagnostics are needed.

## Usage example

Place your own sensor JSON and matching video under the local `examples/` directory, then open both at startup:

```powershell
cowmata-annotator --mode basic --lang zh `
  --json "examples\2026-08-08 10_44_34.json" `
  --video "examples\hiv00102.mp4"
```

The media used for the screenshot is intentionally excluded from Git because the video is 256 MiB and the sensor JSON is about 5 MiB. See [examples/README.md](examples/README.md) for the local-data policy.

## Annotation workflow

1. **Open IMU JSON** — confirm sample count, frequency, time range, gaps, and the rendered nine-axis waveforms.
2. **Open video** — load the matching recording and inspect its real duration.
3. **Align and pin** — seek both timelines to the same observable event, then choose **Pin**. Annotation is enabled only after alignment.
4. **Create labels** — press a label shortcut. Point labels are placed at the playhead; interval labels are created by dragging over the waveform.
5. **Review and save** — adjust boundaries, add notes, undo/redo, save the project, and export only after validation succeeds.

Project saves are written atomically so an interrupted write does not replace a valid project with a partial file.

## Keyboard shortcuts

### Editing and navigation

| Action | Shortcut |
| --- | --- |
| Play / pause | `Space` |
| Save project | `Ctrl+S` |
| Undo | `Ctrl+Z` |
| Redo | `Ctrl+Y` or `Ctrl+Shift+Z` |
| Delete selected event | `Delete` or `Backspace` |
| Previous / next frame | `[` / `]` |
| Move playhead by 100 ms | `←` / `→` |
| Previous / next activity | `P` / `N` |
| Show the full signal range | `F` |
| Video fullscreen | `F11` or double-click video |
| Exit fullscreen / cancel pending interval | `Esc` |
| Calibrate selected model interval | `Ctrl+E` (model-assist mode) |

### Protocol-v4 labels

| Shortcut | Label | Machine code | Type |
| --- | --- | --- | --- |
| `1` | Standing | `STANDING` | Interval |
| `2` | Lying | `LYING` | Interval |
| `3` | Walking | `WALKING` | Interval |
| `4` | Straining onset | `STRAINING_ONSET` | Point |
| `5` | Straining bout | `STRAINING_BOUT` | Interval |
| `6` | Amniotic sac first visible | `AMNIOTIC_SAC_FIRST_VISIBLE` | Point |
| `7` | First fetal part visible | `FETAL_PART_FIRST_VISIBLE` | Point |
| `8` | Calf fully expelled (T0) | `CALF_FULLY_EXPELLED` | Point |
| `9` | Fetal membranes fully expelled | `FETAL_MEMBRANES_FULLY_EXPELLED` | Point |
| `Q` | Tail raised | `TAIL_RAISED` | Interval |
| `W` | Tail wagging | `TAIL_WAGGING` | Interval |
| `E` | Standing up | `STANDING_UP` | Interval |
| `R` | Lying down | `LYING_DOWN` | Interval |
| `A` | Urination | `URINATION` | Interval |
| `S` | Defecation | `DEFECATION` | Interval |
| `0` | Synchronization anchor | `SYNC_ANCHOR` | Point, non-trainable |

## Model-assisted review

Model files are not tracked in this repository. Select a local package through **Model assist → Model package settings…**:

| File | Required | Role |
| --- | --- | --- |
| `gbdt_full.joblib` | Yes | Six event classes and posture/walking fallback |
| `best.pt` | No | `OfflineMultiTaskTCN` for standing, lying, and walking |
| `inference_config.json` | No | Sensor-scaling overrides for JSON outside the package |

`xgboost` is pinned to `3.2.0` for artifact compatibility. Suggestions are imported as **Pending review** and retain their original class, boundaries, scores, and later human edits. See [Model-assisted annotation](docs/model-assist.md) for the full contract. For current training and evaluation workflows, use the [algorithm repository](https://github.com/zxq309/cowmata-tailring).

## Inputs, projects, and exports

| Item | Purpose |
| --- | --- |
| Nine-axis JSON | Continuous accelerometer, gyroscope, magnetometer, and timestamp data |
| Video | MP4 and other VLC-supported cattle recordings |
| Project JSON | Alignment, source references, protocol, labels, events, notes, and review state |
| Events CSV + metadata JSON | Flat event exchange with `label`, `code`, and `en` fields |
| Aggregated / BORIS CSV | Interoperable interval/point event summary |
| Training-sample CSV | Timestamped multi-hot samples; set the real `cow_id` before export |
| IRR CSV | Inter-rater reliability report |

The interface language never rewrites stored annotations. Machine keys such as `STANDING` and `TAIL_RAISED` remain unchanged across English and Chinese sessions.

## Repository layout

```text
cattle-tail-ring-annotator/
├── cowmata_tailring/       # Application, UI, media, annotation, and inference code
├── assets/                 # Authorized brand assets and README screenshot
├── docs/                   # Usage, model-assist, architecture, and packaging notes
├── examples/               # Local-only sample data instructions
├── scripts/                # Windows and shell launch helpers
├── tests/                  # Automated tests
└── .github/                # CI, issue forms, and pull-request template
```

## Development and verification

```powershell
pip install -e ".[dev]"
ruff check cowmata_tailring tests
pytest -q
python -m cowmata_tailring --version
```

The release candidate shown above was integration-tested on Windows with Python 3.12.13, VLC 3.0.23, the real example pair, and the automated test suite.

## Related repositories

| Repository | Role |
| --- | --- |
| [zxq309/cattle-tail-ring-annotator](https://github.com/zxq309/cattle-tail-ring-annotator) | This desktop annotation and human-review tool |
| [zxq309/cowmata-tailring](https://github.com/zxq309/cowmata-tailring) | COWMATA tail-sensor algorithms, experiments, and engineering baseline |

## Contributing, security, and citation

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
- Report vulnerabilities through the process in [SECURITY.md](SECURITY.md), not a public issue.
- Cite the software with [CITATION.cff](CITATION.cff).
- Use [GitHub Issues](https://github.com/zxq309/cattle-tail-ring-annotator/issues) for reproducible bugs and scoped feature requests.

## Team

- **Xiangqing Zhang** — CTO, Yangling Yuanshangyuan Intelligent Technology Co., Ltd.; Yan'an University
- **Yalong Zhang** — Founder, Yangling Yuanshangyuan Intelligent Technology Co., Ltd.
- **Tengyu Jiao** — Yan'an University
- **Yachen Zhao** — Yan'an University

## License and brand assets

Source code is released under the [MIT License](LICENSE). The COWMATA names and logo files in `assets/brand/` are company brand assets and are not relicensed by MIT; see [NOTICE](NOTICE) and [assets/README.md](assets/README.md).
