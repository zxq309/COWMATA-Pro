# Contributing

Thank you for improving COWMATA Tail-Ring Annotator. Keep pull requests focused, reproducible, and safe for research data.

## Before opening a pull request

1. Create a branch from `main`.
2. Install the development environment with `pip install -e ".[dev]"`.
3. Add or update tests for behavior changes.
4. Run:

   ```powershell
   ruff check cowmata_tailring tests
   pytest -q
   ```

5. If the UI changed, test with a local JSON/video pair and attach a screenshot that contains no private paths, people, device identifiers, or restricted data.

## Design constraints

- Preserve continuous raw IMU data and absolute timestamps; create learning windows downstream.
- Keep persisted machine codes stable across interface languages.
- Do not allow unaligned video/IMU annotation.
- Keep model output in a human-review queue; never auto-accept a prediction.
- Maintain compatibility with existing project JSON and event CSV exports when practical.

## Data and large files

Do not commit real farm videos, raw sensor exports, model weights, generated caches, or private datasets. The `examples/` directory is local-only except for its instructions. If a tiny fixture is necessary, use synthetic or explicitly cleared data and explain its provenance in the pull request.

## Bug reports

Include the operating system, Python version, VLC version, launch command, traceback or log excerpt, and minimal reproduction steps. Do not include confidential recordings or credentials.
