# COWMATA Annotator 3.5.3

A Windows workstation for organizing cattle recordings, synchronizing multiview video with continuous nine-axis IMU, annotating behavior, and reviewing evidence. Offline Python, Qt, VLC, FFmpeg and OCR dependencies are bundled.

[Setup](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.5.3/COWMATA-Annotator-3.5.3-Setup.exe) · [Portable ZIP](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.5.3/COWMATA-Annotator-3.5.3-Portable.zip) · [Illustrated manual](docs/quick-start-illustrated.md) · [56-page PDF](https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.5.3/COWMATA-3.5.3-Manual.pdf) · [中文](README.zh-CN.md)

Version 3.5.3 fixes the workflow that displayed planned destination names without transferring files. Start organization directly: bounded parallel recognition feeds verified file transfers, and each completed file appears on disk before the remaining batch finishes.

- Add multiple directories with independent view01–view08 assignments. Inputs are searched recursively in both video-only and mixed IMU/video workflows.
- Filter video extensions first. Prefer supported native absolute timestamps, then first-frame OCR and bounded opening-frame retries. Unresolved files remain in place while other files continue.
- Store video under `Video/date/view/start-time`, without binding shared footage to a single cow. IMU naming and annotation references retain their existing conventions.
- Closing the organization window stops its worker and permits normal main-window shutdown. Updates show independent progress, restart the verified application before old-file cleanup, and skip the redundant post-update network gate.
- Retain daily projects, cross-drive file pairing, layered annotations, historical review, evidence export and cow-disjoint dataset construction. PPG waveform parsing remains reserved.

Close the old version and run Setup, or extract the entire portable ZIP and launch `COWMATA.exe`. For the first migration from an older updater, run this installer directly. Explicit preview mode does not transfer files.

Naming timestamps and opening-frame estimates do not certify continuous synchronization. Model candidates and demonstration labels are not field-validation evidence. See [release notes](docs/release-353.md) for verification and limits.
