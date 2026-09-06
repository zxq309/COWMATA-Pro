<div align="center">

> **Windows offline app:** ordinary users download the Setup EXE or portable ZIP from
> [Releases](https://github.com/zxq309/cattle-tail-ring-annotator/releases).
> No Python, pip, VLC, CUDA toolkit, or model setup is required. The automatically
> generated **Source code.zip is not the runnable application**.

<img src="assets/brand/cowmata-logo.svg" alt="COWMATA" width="300">

# COWMATA Tail-Ring Annotator

**Human-in-the-loop video and nine-axis IMU annotation for cattle behaviour and calving research**

[![CI](https://github.com/zxq309/cattle-tail-ring-annotator/actions/workflows/ci.yml/badge.svg)](https://github.com/zxq309/cattle-tail-ring-annotator/actions/workflows/ci.yml)
[![Release](https://img.shields.io/badge/release-3.1.0--rc.1-0A7EA4)](https://github.com/zxq309/cattle-tail-ring-annotator/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[English](README.md) · [简体中文](README.zh-CN.md) · [Project overview](https://github.com/zxq309/cowmata)

</div>

![COWMATA Tail-Ring Annotator showing synchronized barn video and nine-axis IMU waveforms](assets/screenshots/annotator-overview.jpg)

<p align="center"><sub>Real integration test: a 59 min 59.859 s, 179,378-sample, 50 Hz IMU record synchronized with cattle video. Source media stays local and is not committed.</sub></p>

## Overview

COWMATA Tail-Ring Annotator is a Windows desktop workstation for reviewing synchronized cattle video and continuous nine-axis tail-ring IMU data. It combines a shared timeline, protocol-v4 labels, safe project persistence, review tools, and research-ready exports in one interface.

### Why this tool

- **Video and IMU in one timeline** — seek, play, zoom, and inspect nine channels without switching applications.
- **Alignment before annotation** — formal annotation remains locked until the video and sensor timelines are pinned.
- **Protocol v4 built in** — 16 behaviour/calving labels plus a non-trainable synchronization anchor.
- **Human-controlled model assistance** — candidates enter a review queue and never become labels without confirmation.
- **Traceable output** — stable machine codes, display labels, timestamps, provenance fields, and validation-aware exports.
- **Bilingual UI** — English and Simplified Chinese affect display only; persisted label codes remain stable.

## Annotation examples

<table>
  <tr>
    <td width="50%" align="center">
      <img src="assets/screenshots/annotation-interval-example.jpg" alt="Selected standing interval over real nine-axis waveforms"><br>
      <strong>Interval boundary review</strong><br>
      <sub>A selected interval is shaded across all nine channels, with draggable start/end boundaries and a traceable event-table row.</sub>
    </td>
    <td width="50%" align="center">
      <img src="assets/screenshots/annotation-multilabel-example.jpg" alt="Overlapping protocol-v4 labels over real nine-axis waveforms"><br>
      <strong>Multi-layer protocol-v4 annotation</strong><br>
      <sub>Body state, tail action, posture transition, and a synchronization point coexist on one absolute timeline.</sub>
    </td>
  </tr>
</table>

> [!NOTE]
> These two images use the real 50 Hz waveform renderer. Their labels and notes are illustrative UI fixtures, not scientific ground truth for the source recording. Reproduce them locally with `python scripts/capture_readme_screenshots.py <sensor.json>`.

## Requirements

- Windows 10/11 **x64**, with the normal graphics driver installed.
- Enough local space for the application (approximately 2 GB extracted), project indexes and optional caches.
- Hardware decoding is automatically attempted; software compatibility mode remains available.

The source and pure logic tests also run outside Windows, but native playback and the delivered executable are Windows-only. See [distribution and build instructions](docs/windows-distribution.md).

## Quick start

### One-click Windows launch

Download `COWMATA-...-Setup.exe`, install into a new application directory, then launch COWMATA from the Start menu. Alternatively, fully extract `COWMATA-...-Windows-x64.zip` and run `COWMATA.exe`. Do not run inside the ZIP. Keep all runtime/vendor folders together. `START_ANNOTATOR.bat` remains a fallback and does not install anything.

The application provides A/B/C layouts, 1–8 selectable views, GPU playback, cross-file continuation, exact original-frame review, offline RapidOCR PP-OCRv6 medium, five versioned 20260906 candidate models, self-contained IMU annotation export, and independent history review. Predictions are candidates, never automatic ground truth. See [usage](使用说明.txt).

### Source development (not required for annotators)

The Git repository intentionally excludes runtimes, model weights, local recordings, sensor data, caches and installers. For complete offline workspace development, use the matching Release runtime as described in [the developer guide](docs/windows-distribution.md). For the legacy basic interface only, install Python 3.10+ and VLC 3.x, then:

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

The scripts under `scripts/` remain available for model-assisted and diagnostic launches. Use `scripts\launch-debug.bat` when console output is needed.

## Project-workspace workflow

Open the existing data-project folder → let the background index discover IMU devices and video views → choose the device/cow and 1–8 views → verify clock anchors → review actual video and label the original IMU interval → export a self-contained annotation JSON. Open that JSON independently later; original recording identities and calibration locate the video, not the annotation filename.

While four or more views and candidate inference run together, main-camera priority protects interaction; auxiliary frames are clearly marked previews, not current evidence. Pause to verify exact original frames across views. See [Windows distribution and GPU details](docs/windows-distribution.md) and [OCR integration](docs/ocr-lightweight-integration.md).

The following single-video examples, shortcuts and legacy model-package contract remain for compatibility with `--mode basic` / `--mode model-assist`; the EXE defaults to the project workspace.

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
| `D` | Manual calving assistance | `MANUAL_CALVING_ASSISTANCE` | Interval |
| `0` | Synchronization anchor | `SYNC_ANCHOR` | Point, non-trainable |

## Model-assisted review

Model files are not tracked in this repository. Select a local package through **Model assist → Model package settings…**:

| File | Required | Role |
| --- | --- | --- |
| `gbdt_full.joblib` | Yes | Six event classes and posture/walking fallback |
| `best.pt` | No | `OfflineMultiTaskTCN` for standing, lying, and walking |
| `inference_config.json` | No | Sensor-scaling overrides for JSON outside the package |

`xgboost` is pinned to `3.2.0` for artifact compatibility. Suggestions are imported as **Pending review** and retain their original class, boundaries, scores, and later human edits. See [Model-assisted annotation](docs/model-assist.md) for the full contract.

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
├── START_ANNOTATOR.bat     # Offline backup launcher; never installs dependencies
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

This branch prepares 3.1.0-rc.1. Current release artifacts and acceptance evidence are in [Releases](https://github.com/zxq309/cattle-tail-ring-annotator/releases); older 2.x records remain in [CHANGELOG.md](CHANGELOG.md). Source-only CI deliberately skips the five binary-pack integrity cases; those are required in portable-package acceptance.

## Contributing, security, and citation

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
- Report vulnerabilities through the process in [SECURITY.md](SECURITY.md), not a public issue.
- Cite the software with [CITATION.cff](CITATION.cff).
- Use [GitHub Issues](https://github.com/zxq309/cattle-tail-ring-annotator/issues) for reproducible bugs and scoped feature requests.

## License and brand assets

Source code is released under the [MIT License](LICENSE). The COWMATA names and logo files in `assets/brand/` are company brand assets and are not relicensed by MIT; see [NOTICE](NOTICE) and [assets/README.md](assets/README.md).

## Latest update

**2026-09-07** — Prepared 3.1.0-rc.1: offline EXE, multiview GPU playback, self-contained annotation history, reviewed 20260906 candidate models and OCR lightweight v2. Kept this page focused on component usage and preserved the documentation-link cleanup. [Full changelog](CHANGELOG.md).
