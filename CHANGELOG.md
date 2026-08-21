# Changelog

All notable changes to COWMATA Tail-Ring Annotator are documented here.

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

[2.0.0]: https://github.com/zxq309/cattle-tail-ring-annotator/releases/tag/v2.0.0
