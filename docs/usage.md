# Usage

## Launching

```bash
cowmata-annotator                         # model-assisted mode (default)
cowmata-annotator --mode basic            # annotation only, no model loaded
cowmata-annotator --lang en               # force English; --lang zh for Chinese
python -m cowmata_tailring --mode basic   # equivalent module form
```

On Windows, run `scripts\install.bat` once, then double-click `scripts\launch.bat`.
Use `scripts\launch-debug.bat` when you need console output.

Open source files at startup when their pairing is already known:

```powershell
cowmata-annotator --mode basic `
  --json "examples\sensor.json" `
  --video "examples\video.mp4"
```

## Workflow

1. **Open the nine-axis JSON** — the real waveform is drawn once a source JSON is loaded.
2. **Open the video** — use the toolbar, or the previous/next buttons to walk through a folder sorted by filename.
3. **Align the two timelines** — while unpinned, video and IMU move independently. Adjust until the frame matches the signal, then click **Pin**. Once pinned, both timelines are linked in both directions and formal annotation is enabled.
4. **Annotate** — press a label shortcut, then drag on the curve to create an interval. Point events are recorded at the playhead.
5. **Save or export** — projects are written atomically; the events CSV carries `label`, `code` and `en` columns.

Annotation is deliberately blocked while unpinned: an unaligned annotation is worse than a missing one, because the error is invisible downstream.

## Calibration notes

- Hikvision recordings are provisionally synced at zero offset until you calibrate them, either by entering the frame overlay time (`HH:MM:SS.mmm`) or by pinning manually.
- If a surveillance video reports an abnormal duration, the timeline is verified and corrected before playback.
- Where video and IMU share no covered time, seeking and playback are refused rather than silently showing a wrong frame.

## Keyboard

| Action | Key |
| --- | --- |
| Select label | Label shortcut (`1`–`9`, `Q`, `W`, `E`, `R`, `A`, `S`, `0`) |
| Play / pause | `Space` |
| Save project | `Ctrl+S` |
| Undo / redo | `Ctrl+Z` / `Ctrl+Y` or `Ctrl+Shift+Z` |
| Delete selected event | `Delete` or `Backspace` |
| Previous / next frame | `[` / `]` |
| Move playhead 100 ms | `Left` / `Right` |
| Previous / next activity | `P` / `N` |
| Show all signals | `F` |
| Fullscreen | `F11` or double-click the video |
| Exit fullscreen | `Esc`, `F11`, or double-click |
| Calibrate selected interval | `Ctrl+E` |

## Data and language

Interface language never affects stored data. `code` (`STANDING`, `TAIL_RAISED`, …) is the machine
key; `name` and `en` are display strings only. Projects and CSV files preserve stable machine codes
across English and Chinese sessions.
