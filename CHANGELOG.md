# Changelog

All notable changes to COWMATA Tail-Ring Annotator are documented here.

## Unreleased — 2026-09-07

- Clarify the four-repository boundaries and add system/decision navigation in both READMEs.

## [2.2.0] — 2026-09-02

### Added

- Imou/Dahua MPEG-PS playback support: content-scanned duration and keyframe seek indexes cross-validated with ffprobe packet scans and adjacent-file recording times, plus a timestamp-normalized virtual playback stream for precise seeking.
- Built-in MPEG-PS fallback scanner that recovers Imou/Dahua duration and seek indexes when ffprobe is missing, failing, empty, or timing out.
- Zero-filled video placeholder detection: empty or all-zero recorder slots are rejected before VLC opens them and are skipped during previous/next navigation.
- Pinned segment-clock continuation for adjacent Imou/Dahua files, with automatic fallback to the previous playhead-based behaviour when validation fails.
- Hikvision duration validation gate: unvalidated VLC raw durations are never published; seeking is disabled until the packet timeline is validated, and validation failure freezes playback with a persistent reason instead of keeping the untrusted duration.
- Protocol-v4 support for the `MANUAL_CALVING_ASSISTANCE` interval label, including bilingual display text, a stable machine code, and the `D` shortcut.
- In-progress intervals now appear immediately in the annotation table without being persisted, exported, or treated as completed events.

### Fixed

- Prevent copied-file mtime deltas from overriding complete Dahua/Imou frame scans, and invalidate duration caches created by the unsafe arbitration rule.
- Recover Dahua/Imou duration and seek indexes with the built-in MPEG-PS scanner when ffprobe is missing or fails.
- Reject empty or zero-filled video placeholders before VLC opens them, and skip them during previous/next navigation.
- Preserve pinned Dahua/Imou alignment across adjacent files with a validated relative segment clock instead of rebasing every file at the current playhead.
- Drain the active Dahua/Imou callback reader before replacing its VLC media, preventing native crashes during repeated seeks and source changes.
- Hide unvalidated Hikvision durations, publish only the corrected packet timeline, and disable seeking when timeline validation fails.
- Preserve unfinished annotation intervals while unpinning for recalibration; users can resume them after re-pinning or cancel them explicitly.

## [2.1.0] — 2026-08-21

### Added

- Root-level `START_ANNOTATOR.bat`: double-click setup on the first run and direct GUI launch afterwards.
- Two reproducible annotation-example images covering interval-boundary review and overlapping protocol-v4 layers.
- `scripts/capture_readme_screenshots.py` for regenerating UI examples from a local nine-axis JSON without publishing source data.

### Changed

- Expanded both READMEs with a one-click Windows path and a three-image product gallery.

## [2.0.0] — 2026-08-21

### Added

- Installable `cowmata_tailring` package and `cowmata-annotator` command.
- `--json` and `--video` startup arguments for reproducible local sessions.
- English and Simplified Chinese interfaces with stable persisted machine codes.
- Protocol-v4 label set, keyboard reference, human-review workflow, IRR, and validated export paths.
- CI across Windows/Linux and Python 3.10/3.12, structured issue forms, citation metadata, and security/contribution guidance.
- Authorized COWMATA brand assets and a screenshot from a real synchronized video/IMU test.

### Changed

- Replaced the legacy flat collection of entry-point/window files with a maintainable application, media, annotation, UI, and model-runtime package layout.
- Renamed the console entry point from the ambiguous `cowmata-tailring` to `cowmata-annotator`.
- Updated the window identity and default protocol display to COWMATA protocol v4.
- Rebuilt the bilingual README around quick start, usage example, workflow, shortcuts, protocol, exports, and companion-repository navigation.

### Removed

- Duplicate historical launch/window modules, checked-in production weights, generated QA screenshots, and other obsolete repository artifacts.
- Large/private example media from Git tracking; only local usage instructions remain.

[2.2.0]: https://github.com/zxq309/cattle-tail-ring-annotator/releases/tag/v2.2.0
[2.1.0]: https://github.com/zxq309/cattle-tail-ring-annotator/releases/tag/v2.1.0
[2.0.0]: https://github.com/zxq309/cattle-tail-ring-annotator/releases/tag/v2.0.0
