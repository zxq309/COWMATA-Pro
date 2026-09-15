# COWMATA Annotator 3.7.0

A Windows workstation for classifying and downloading cattle sensor records, synchronizing multiview video with continuous IMU, annotating behavior, and reviewing evidence. Python, Qt, VLC, FFmpeg and offline OCR models are bundled.

Clean delivery: source ZIP, extracted portable folder and installer. [3.7 release notes](docs/release-370.md).

Five versioned dataset workflows pair original Motion/PPG JSON with annotations. Existing labels can be reviewed and edited in place without creating duplicate events. Background classification uses bounded decoding/OCR and transfer pools with coalesced UI updates.

- **Tools / Data classification** offers one window for mixed JSON/video intake or adding videos to existing JSON records. Unchanged archived files are reused without repeating OCR, full hashing or copying. Missing destinations are repaired.
- **Pause and resume** retain the original task and verified partial copies. Live CSV records include file status, elapsed time and reuse.
- **Dates and directories** prefer native recording clocks and tolerate missing OCR punctuation. Videos are retained by their recording date even when no IMU record exists for that day. Unresolved dates remain explicit; no timestamps are invented.
- **Tools / Edge data download** supports manual, automatic and scheduled Motion/PPG JSON downloads, retries, late arrivals, multiple farms and optional SSH tunnels. [Download guide](docs/edge-download.md)
- **Clean releases** contain no local keys, user settings, download history, logs, caches or field datasets. Existing annotation, history, evidence export and dataset construction remain available.

Run `COWMATA.exe` inside the already extracted portable folder. Keep acquisition data outside the application folder.

Automatic and scheduled downloads require the application to remain running. The 3090 raw service supplies Motion only; PPG requires a pulse-data endpoint. Remote services have not been acceptance-tested; protocol and task controls are validated against a local HTTP test server.

Archive routing dates do not establish verified synchronization or ground-truth labels. [Existing annotation guide](docs/quick-start-illustrated.md)
