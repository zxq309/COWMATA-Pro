# Changelog

## [3.3.1] - 2026-09-10

- Recover playback when a surveillance audio stream lacks its sample rate: retry video-only stream copy for that error, retain healthy audio and source files, and validate the derived timeline.
- Prepare playback caches after the paused original frame appears. Share preparation across play and repeated seeks, keep GUI lookups responsive, cancel abandoned work and ignore results from an old project.
- Show the active action, start time and correct end key. Restore numeric shortcuts while the behavior selector has focus; add explicit cancellation for unfinished actions.
- Connect video drafts to independent IMU boundary dragging and timestamp editing with 0.1-second controls. Keep draft previews separate from confirmed labels and require new evidence after edits.
- Apply the playback recovery to all five behavior candidate review routes. Deliver a complete portable ZIP, installer and clean source archive; existing users upgrade directly to 3.3.1.

See [release notes](docs/release-331.md), [validation](docs/release-331-validation.md) and the updated [illustrated manual](docs/quick-start-illustrated.pdf).

## [3.3.0] - 2026-09-10

- Establish organize → annotate/save → review/export. Add a background organization window for single-file, single-device, single-day and multi-day nested sources; normalize view01–08, preview moves, protect active projects, quarantine only classified junk, and resume interrupted work.
- Require full-device-ID / ear-tag / field-mark identity before new organization. Keep numeric ear tags and case-sensitive field marks intact. Preserve historical malformed sources and labels; show suggestions without guessing a rename. Reused devices are associated with each record and its capture date, not deduplicated across cows or dates.
- Add seven stable collection categories: healthy, estrus, pregnancy_early, pregnancy_mid, pregnancy_late, calving and disease. Carry category and identity through save/reopen, drafts, review, undo, team returns and exports. Behaviors remain independently selectable.
- Fix pending-only startup regression: stable device grouping, failed-opening hint fallback, later-frame OCR, legacy timing routes, changed-source retry, and continued background search when Play is pressed before video coverage exists. Refresh the material index dialog while work progresses.
- Fix legacy SQLite view normalization, nested active-project protection and interrupted organization journals. Preserve original assets and retained human work.
- Fix export evidence paths, real timestamp columns, incoming category UI refresh and team conflict detection. Move large return/training-export I/O off the GUI thread.
- Use mapped date/time throughout IMU waveforms, annotation/history lists and algorithm candidates; refresh after synchronization edits and avoid overlapping axis labels. Preserve internal sample coordinates.
- Retry transient Windows sharing conflicts during atomic saves, retain failed work and make interval completion idempotent across records. Defer native video-window shows across Qt turns to reduce eight-view history stalls.
- Repair corrupt partial-update retry and rollback cleanup; serialize update workers for each install location. Startup still requires the latest release check and direct update to the newest version.
- Replace the illustrated operation manual with 32 pages and 38 actual 3.3 screenshots, reproducible sources, bookmarks and short steps; add its entry to About. Keep all executable/package versions at 3.3.0.

See [validation](docs/release-330-validation.md) and [scope/limits of the audit](docs/code-audit-330.md).

### 3.2.1 timestamp and latest-update correction — 2026-09-09

- Require a startup release check before constructing any annotation/history window or opening project files. Newer releases automatically download, verify, install and restart; errors stay at a retry/exit gate. This mandatory startup rule ignores prior optional background-check preferences.
- Offer only the newest release in the selected channel, with one reminder per package across restarts. Recheck before downloading, after downloading and before preparing installation; superseded packages cannot remain ready for installation. Cancelling the save/close step cancels the queued installation, and the GitHub entry opens the latest release directly.

- Keep the public version at 3.2.1. Display the existing shared video/IMU reference clock on every record's waveform axis, hover readout and editable position field, including date and milliseconds where appropriate. Refresh the display when records or calibration change; preserve original sample and annotation coordinates.
- Rebuild the offline installer and refresh the existing v3.2.1 release assets and checksums. Earlier 3.2.1 clients require a manual download because their updater compares version numbers. Build identity: `project-load-playback-resume-r2-timestamps-startup-update-20260909`.

## [3.2.1] — 2026-09-09 (local validation build)

- Discover explicit view folders below nested data directories and keep camera identities stable across resolution changes. Preserve manually calibrated old view identities. Search native recording times sparsely around the selected IMU window; do not probe every video before showing the first matching original frame.
- Automatically choose a covering view when saved choices are stale or unavailable, while respecting explicit deselection. Display a successful-load banner after the actual first frame arrives, and retain 500 complete status messages in a resizable, copyable log window.
- Cancel scanning and video probes without blocking Qt during exit. Clean only owned temporary metadata from unsuccessful new loads; preserve successful projects, human work and unknown files. Retain a recovery marker until cleanup succeeds.
- Add a permanent per-view progress bar with elapsed/total time. Coalesce drag requests until release, use the main decoder's actual clock, and avoid repeated corrective seeks on that main view. Native PS playback uses a bounded, validated stream-copy cache with its own timeline; exact paused frames still come from the original recording.
- Distinguish active, unfinished, new and completed records by both text and color. Autosave on switching and restore the last position and labels. Confirm saving before closing and when completing a record. Saved labels remain editable/deletable after reopening; edits reopen completed records and require review, with undo preserved.
- Build a clean offline package from reviewed source plus an existing matching component bundle. No dependency installation or changes to original data are required.

See [3.2.1 workflow and validation](docs/release-321-validation.md).

## [3.2.0] — 2026-09-09

- Align the desktop UI, installer, EXE, shortcuts and running-window icon with the official COWMATA green C favicon and wordmark. Preserve the website's green/blue accent palette, readable checked controls and optional glass effects. Notify Windows about this application's changed icons without resetting the global icon cache.
- Add top-level Behavior Recognition and Health & Reproduction menus before Help. All twelve entries enforce one switchable camera at the shared reference time; restore the previous annotation layout when leaving the inspection.
- Run the five registered 20260906 behavior models through a separate, cancellable background inspection panel. Retain versioned results separately from the existing candidate workflow and human labels; stale results cannot attach to another record. Missing defecation, mounting, straining and health algorithms remain explicitly unavailable.
- Append MOUNTING without changing historic label indices. Resolve keyboard labels by stable code rather than new-default indices, including old and reordered projects. Choosing an algorithm's human label never creates or confirms an annotation.
- Remove audited unused Qt WebEngine/developer payloads and ffplay from the offline distribution. Retain both private runtimes, VLC codecs, FFmpeg/ffprobe, OCR and all five event models. Use independent LZMA compression blocks with a zlib fallback option.
- Include a ten-page illustrated quick start based on actual application operations, accessible from Help. Verify current-record progress/resume, original-frame evidence extraction, label export and read-only history in the new UI.

See [algorithm boundaries](docs/algorithm-inspection.md) and [3.2.0 validation](docs/release-320-validation.md). Real demo alignment and labels illustrate the UI, not scientific ground truth.

## [3.1.2] — 2026-09-09

- Match the selected IMU acquisition window to video-native recording clocks before OCR. Add strict, sample-validated Hikvision HK1 and Shenmo PES2 readers with explicit timezone handling; unknown layouts, discontinuities and conflicting image checks remain on the OCR/manual path. File creation dates and numeric filename order are not recording-time evidence.
- Publish native-time candidates for immediate review before deferred visual checks. Validate all parsed packet clocks, retain SHA-256 source identity and require independent image agreement before verified evidence. Bind cached timing to content-verified relocated files.
- Seek preallocated MPEG-PS recordings by bounded keyframe byte ranges, excluding invalid recorder tails. Preserve original packet timing in memory rather than compressing preroll. No video edits, recuts or full-size proxies.
- Include the unpublished 3.1.1 on-demand/progress improvements below. Open any original IMU with Ctrl+J, retain arbitrary order, skip explicitly completed records and resume unfinished positions. Do not infer completion from the existence of a few labels.
- Add team return settings: a dedicated inbox, two stable observations, content/range/image checks, optional receipt of explicitly completed conflict-free records, automatic placement inside existing project metadata, duplicate detection and preserved conflicting variants. Raw data and project-wide camera calibration are never silently overwritten.
- Simplify File/Edit/View/Tools/Help commands, group exports, collaboration, indexing, synchronization and evidence, keep descriptions in hover tips, and label the About entry simply About. Preserve color, icons, visible checkmarks and glass effects.
- Default new projects to single-view priority: prepare the main view first, then load auxiliary stills one at a time. Click a tile's play control to promote it exclusively; other views remain paused with explicit image timestamps. Global pause refreshes all exact original frames at the shared review time. Keep periodically refreshed previews and full multistream playback as alternatives.
- Add high-contrast, translucent hover controls per video: exclusive play/pause, shared-timeline ±5 seconds, rate, enlarge/restore. Preserve audio only on the main view, IMU synchronization, original frame rates and evidence guards.
- Move native VLC open/seek/pause/poll operations into owned decoder threads, coalesce obsolete seek commands, bound per-decoder CPU threads, and tag snapshots by source/seek generation. Fix native-media picture counter resets and prevent pre-seek observations from confirming a new frame.
- On Windows, replace the selected file's unconditional stability delay with before/after write-handle and stamp checks. Reuse its parsed IMU and avoid rebuilding unchanged record lists on each background video result.
- Preserve non-finite optional model-summary statistics as explicitly unavailable values so empty-result audits can be saved; invalid candidate positions/scores still fail validation. No weights, predictions or labels are fabricated.

Limits: native layouts are validated on supplied samples, not a universal camera specification. First discovery still depends on disk speed, unknown-format OCR and directory size. Multiple hardware decoders can exhaust driver buffers; no claim of eight full-rate streams on every computer.

## [3.1.1] — 2026-09-08 (local validation build)

- Open large projects with metadata-only file reconciliation. Decode and hash one selected IMU record, then search only the requested capture-time window. Do not automatically run full-project OCR or hash every video on startup.
- Cache stamp-bound OCR routing hints separately from SHA-256 evidence assets. Search card-copy batches using sparse natural-order probes, include overlapping boundary clips, and fully validate candidates before exposing verified intervals. Hints and filename order cannot certify missing footage.
- Bound exploratory work, allow guided searches to finish the requested window, and offer explicit expansion, pause and optional full background indexing. Cancel stale hashing/OCR work on record changes; reuse decoded opening frames without promoting unverified routing readings.
- Save explicit record completion, unfinished position and a compact progress summary inside the existing project metadata directory. Reopen unfinished work or select the next unfinished record without starting completed records' videos. Existing saved work is treated as in progress, never assumed complete merely because labels exist.
- Add a completion/next/save-exit choice at record end, guarded against stale queued prompts. Keep all original sensor files and media unchanged.
- Restore a visible File/Edit/View/Tools/Help menu bar; add a searchable hover/pinnable source list, clear checkmarks and dropdown arrows, stronger control text and readable glass menus. Move explanatory text to tooltips, keep warnings visible, and place update controls in About while retaining automatic notifications.
- Default new presentation preferences to main-camera playback with auxiliary previews; preserve user-selected playback settings, view choices and all evidence safeguards.

## [3.1.0] — 2026-09-07

- Save one original-resolution JPEG per selected camera at one aligned, human-reviewed time. A waveform-deviation suggestion can be adjusted; missing/uncertain views stay explicit. Two bounded background decoders keep screenshot extraction independent of low-rate previews.
- Keep screenshot checksums, original video identity and actual frame PTS, cow/IMU/label coordinates and frozen clock provenance. Export labels with one flat sibling evidence folder; screenshots are excluded from nine-axis model inputs and training metadata.
- Reopen labels, embedded original IMU/snippets and evidence images without local recordings; inspect each view at full resolution. Check image integrity and identify stale label/synchronization context.
- Verify external video archive copies using full SHA-256, including renamed files; flag incomplete indexes and missing/mismatched copies. Preserve archive lookup clues and relink read-only without rebuilding the archive. Never automatically delete recordings.
- Preserve previously confirmed labels when verified videos are offline, without allowing screenshots or archive receipts to confirm new truth.
- Consolidate the acquisition-time compatibility, About/company information, native application/taskbar icons, installer path/shortcut/uninstall fixes and safe background updater from local rc2/r3 maintenance into the public stable line. Older clients without an updater require one manual installation; thereafter checks run at startup and every 30 minutes, download in the background, and install after normal saved exit.

## [3.1.0rc2] — 2026-09-07 (local build, not published)

- Apply the developer-confirmed protocol: create_time is acquisition start; update_time is server receipt. V2 absolute sample time includes the first-frame counter without shifting existing parent-relative labels. Keep v0/v1 readers with an explicit estimated-origin quality.
- Automatically link newly opened IMU to candidate video using device capture time and the project timezone. Existing manual maps retain priority; device clocks never manufacture confirmed truth or bypass calibrated, same-cow continuation rules.
- Refresh obsolete IMU index metadata without rerunning video OCR or deleting human work. Export capture provenance and preserve parent coordinates/epochs through standalone snippet history.
- Add startup/30-minute GitHub update checks, stable/preview channels, optional background download, validated resume, GitHub SHA-256 verification, and save/exit-controlled in-place installation.
- Extract and verify updates beside the installation, import-test the private runtime, then swap directories. Installation/registration failure rolls back; unknown customer files, redirected paths, insufficient space and busy applications prevent unsafe replacement.
- Keep complete offline dependencies. The installer builder also produces cowmata-update.json for publication with the EXE and checksum. HTTPS/GitHub digest verification is not a commercial code signature; older clients need one manual bootstrap.

## Local maintenance r3 — 2026-09-07 (not published)

- Use COWMATA Annotator for the default application folder, installer title and shortcuts.
- Add an About dialog with verified company contact, application/build versions, source/brand/third-party notices and an explicitly manual Release-page link. Existing MIT attribution is preserved.
- Assign Cowmata.Annotator to the hosted Windows GUI, launcher and installed shortcuts so the taskbar no longer groups the workstation as Python.
- Pass -B explicitly to both portable launchers: isolated Python ignores PYTHONDONTWRITEBYTECODE. Clean generated bytecode matching shipped sources and the application's own log during uninstall.
- Uninstall refuses redirected component directories and a busy launcher/runtime; unknown user files are preserved with an explanation. No recursive removal of data projects.
- Automatic checks/downloads/in-place upgrades are NOT included in r3. Existing r2 and older clients require a manual transition to a future updater-enabled build.

## Local installer revision r2 — 2026-09-07 (not published)

- Select a parent location and editable application folder name; Setup creates the folder automatically, while preserving occupied directories.
- Default-on desktop shortcut, matching installer/uninstaller/application icon and Windows Installed Apps icon. Vendored Phosphor cow artwork includes its MIT license and provenance.
- Explicit UTF-8 input for NSIS and C# compilation; localized Chinese/English completion text and Chinese-capable wizard font.
- Speed-oriented non-solid zlib packaging and reduced per-file UI log updates. Larger download than the original solid-LZMA build; no runtime/model removal or security exclusions.
- Installer-only identity 3.1.0-rc.1-r2; application/algorithms remain 3.1.0rc1. Local testing and size/timing evidence are delivered separately; no GitHub release change.

## Repository responsibility cleanup — 2026-09-07

- Removed repeated project/team/navigation sections from both READMEs; kept one project-overview link and direct component functionality, usage and validation.

All notable changes to COWMATA Tail-Ring Annotator are documented here.

## Documentation presentation — 2026-09-07

- Added dated latest-update summaries to both READMEs so main-branch maintenance is visible alongside the latest software release.
- Repaired relative source-document links in historical update notes and the playback audit without rewriting their historical dates.


## Main-branch maintenance — 2026-09-07

- Clarify the four-repository boundaries and add system/decision navigation in both READMEs.
- Integrate frozen OCR lightweight v2, reject cached-profile date conflicts, and queue obsolete automatic indexes without deleting human work.

## [3.1.0rc1] — 2026-09-07

- Replace the README's legacy examples with actual modern-workstation recordings: three-camera layouts/continuation, real five-model inference, traceable label export and independent history. Clearly mark demonstration alignment and labels as non-ground-truth; keep videos in Release assets, not Git source history.
- Make the self-contained Setup.exe the primary end-user download: guided, per-user, offline installation with optional launch on completion. The compressed installer contains the complete private runtime, not an online bootstrapper.
- Fix Python 3.10 SQLite corrupt-index recovery without misclassifying I/O or locking errors as corruption; preserve original recordings and human annotations.
- Batch modern multiview geometry updates without replacing native video handles or changing the shared playhead.
- Use the bundled D3D9 display backend for embedded multiview windows while retaining automatic GPU decoding. The former D3D11 display path showed intermittent layout/reopen stalls in repeated native tests; rendering and decoding are configured separately.
- Avoid toggling native-window redraw during full multiview playback. Paused layouts still batch their repaints.
- Coalesce same-turn live layout/main-camera changes and stop scaling hidden precise-frame pixmaps during native playback; paused and preview frames still render from their original images.
- Keep restored or jumped IMU positions visible in the waveform while preserving the user's zoom; recenter only when the playhead leaves the current view.
- Offline Windows executable launcher and per-user installer; source-only Git layout and separate Release binary assets. No first-run pip, system Python or CUDA toolkit setup.
- Full original IMU JSON bytes accompany a single annotation export; original identity, parent offsets, calibration versions, video index clues and model provenance remain traceable.
- Five reviewed 20260906 event CLI models use a bundled, isolated compatible runtime. Human review is mandatory; empty predictions and quality rejection never create negative truth.
- GPU video decode remains enabled by default. Lossless BMP in-memory frame transport and a bounded exact-frame RAM cache reduce repeated review work; changed sources/clocks cannot reuse stale frames. OCR input transport is unchanged.
- Multiview background-inference load protection, fixed decoder/preview worker limits, independent history review and A/B/C glass-style layouts are retained.
- Removed the separate data-organization document and packaging references. The tool reads existing projects without moving raw data.
- This is a release candidate, not a claim of zero defects or universal eight-stream performance. Native test scope and limitations are published with the release.

## [3.1.0a4] — 2026-09-06

- Default export is one portable annotation JSON, with parent-source identity, calibrated time and relevant video index snapshot. Training-format batch export is now explicitly optional.
- Selected IMU snippets retain exact v0/v1/v2 frames and parent-relative timestamps; unknown server clock semantics never manufacture synchronization.
- Independent read-only history review supports source relocation by SHA-256, archived calibration, multiview playback across recording boundaries, and end-of-snippet pause. Original files and active human work remain unchanged.
- Added export guards, missing/changed-source warnings, corrupt-index snapshot fallback and history regression/native playback checks. Preserved the a3 interface and adaptive playback policies.
- The five 20260906 candidate models are not yet integrated or included in this phased a4 release.

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
