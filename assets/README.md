# Assets

## Brand

The files under `brand/` are reused from the companion [COWMATA algorithm repository](https://github.com/zxq309/cowmata-tailring/tree/main/assets/brand) with project-owner authorization:

- `cowmata-logo.svg` — original company source: <https://www.cowmata.com/assets/dist/img/logo_cowmata.svg>
- `cowmata-company-logo.png` — original company source: <https://www.cowmata.com/assets/dist/img/logo.png>

The COWMATA names, marks, and logo artwork remain company brand assets. Their inclusion here does not place them under the repository's MIT source-code license.

## Screenshots

`screenshots/annotator-overview.jpg` was captured from a real local integration test of this application. The underlying video and sensor files are not included in the repository.

`screenshots/annotation-interval-example.jpg` and `screenshots/annotation-multilabel-example.jpg` are reproducible Qt renders of the real 50 Hz waveform and production annotation widgets. Their injected labels are illustrative UI fixtures, not scientific ground truth. Regenerate them locally with:

```powershell
python scripts\capture_readme_screenshots.py "examples\sensor.json"
```
