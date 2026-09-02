## 🌟 Summary

This release brings playback compatibility for the three recorder formats used in the 2026 Gaoyou pasture deployment: Imou/Dahua, Hikvision, and a third OEM recorder whose files all masquerade as `.mp4` but are actually fixed-size MPEG program streams with broken or missing duration metadata. libVLC alone reports either `0` or multi-hour garbage for these files, so the annotator now probes each file's packet timeline before publishing any duration to the UI, adds a dedicated content scanner and virtual playback stream for Imou/Dahua files, validates adjacent-file continuity for pinned alignment, and guards against corrupted copy artifacts (broken mtimes, zero-filled placeholder slots). Verified end-to-end against the real recordings on Windows with real HWND playback: exact seeks (0 ms landing error), correct durations across all three formats, and no native crashes across repeated far seeks.

## 🚀 Features

- Imou/Dahua MPEG-PS support with content-scanned duration and keyframe seek indexes, cross-validated with ffprobe packet scans and adjacent-file recording times, in [`dahua_duration.py`](https://github.com/zxq309/cattle-tail-ring-annotator/blob/v2.2.0/cowmata_tailring/media/dahua_duration.py) and [`dahua_stream.py`](https://github.com/zxq309/cattle-tail-ring-annotator/blob/v2.2.0/cowmata_tailring/media/dahua_stream.py) ([`6c7af15`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/6c7af15))
- Built-in MPEG-PS fallback scanner that infers frame duration from decoded MPEG clock deltas when ffprobe is missing, failing, empty, or timing out, keeping Imou files fully playable without ffprobe ([`1cb621f`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/1cb621f))
- Zero-filled video placeholder detection: empty or all-zero 256 MiB recorder slots are rejected before VLC opens them and are skipped automatically during previous/next navigation, with a status-bar counter ([`08e532d`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/08e532d))
- Pinned segment-clock continuation for adjacent Imou files: when the user is pinned, switching recordings now computes the next file's exact wall-clock start from the validated mtime delta instead of rebasing every file at the current playhead, with automatic fallback to the previous behavior when validation fails ([`eb3cdeb`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/eb3cdeb))
- Hikvision duration validation gate: unvalidated VLC raw durations (e.g. `08:33:41` spans caused by orphan recorder tails) are never published; the UI shows `00:00:00.000` and disables seeking until the packet timeline is validated, and on validation failure playback and seeking are frozen with a persistent status-bar reason instead of silently keeping the untrusted duration ([`21d9c2c`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/21d9c2c))
- Protocol-v4 now includes the trainable `MANUAL_CALVING_ASSISTANCE` interval label, shown bilingually with a stable machine code and the `D` shortcut.
- In-progress intervals now appear immediately in the annotation table while remaining excluded from saved projects, exports, and plots until they are completed.

## 🐛 Bug Fixes

- Complete content scans are no longer overridden by copied-file mtime deltas; only genuine, mutually confirming adjacent-file times participate, and duration caches created by the old unsafe arbitration rule are invalidated and re-probed on load ([`e98f261`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/e98f261))
- The active Imou callback reader is now drained and quiesced before its VLC media is replaced, preventing native crashes during repeated seeks, file switches, and shutdown ([`8fd5928`](https://github.com/zxq309/cattle-tail-ring-annotator/commit/8fd5928))
- Dragging the video timeline commits the seek on mouse release via the new `seekCommitted` signal, and Imou drags defer seeks until release to avoid long-GOP decoder seek storms
- Early seeks queued before duration metadata arrives are no longer silently clipped by a late-arriving wrong duration
- Timeline probe threads catch unexpected exceptions at the thread boundary so the UI can never hang in a permanent "validating" state
- Unpinning for recalibration no longer discards or blocks an unfinished interval; it can be resumed after re-pinning or cancelled explicitly.

## 🧪 Tests

- The release candidate passes all 166 automated tests; coverage includes duration arbitration, the ffprobe fallback, placeholder health checks, segment-clock continuation, callback stream handoff, the Hikvision duration gate, early-seek gating, and unfinished-interval interactions
- Ruff is clean, and the release branch is committed-clean before publication
- Real-file verification on read-only deployment recordings: duration sequences `0 → 0 → 1,155,872 ms` with no garbage values published; paused and in-playback seeks land with 0 ms clock error across all three formats; 10 consecutive far seeks on Imou with no crash

## 📚 Documentation

- Added the playback compatibility audit ([`video-playback-compatibility-audit.md`](https://github.com/zxq309/cattle-tail-ring-annotator/blob/v2.2.0/docs/video-playback-compatibility-audit.md)) and a numbered change-record series under [`docs/change-records/`](https://github.com/zxq309/cattle-tail-ring-annotator/tree/v2.2.0/docs/change-records) with per-fix verification data and independent revert tags
- Added the remediation status document ([`20260826-07-playback-remediation-status.md`](https://github.com/zxq309/cattle-tail-ring-annotator/blob/v2.2.0/docs/change-records/20260826-07-playback-remediation-status.md)) and this release's version update notes ([`version-update-notes.md`](https://github.com/zxq309/cattle-tail-ring-annotator/blob/v2.2.0/docs/version-update-notes.md))
- New zh/en translation entries for the Imou and Hikvision validation messages

## What's Changed

**Full Changelog**: https://github.com/zxq309/cattle-tail-ring-annotator/compare/v2.1.0...v2.2.0
