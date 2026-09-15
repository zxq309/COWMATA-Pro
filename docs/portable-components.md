# Portable components

The release is assembled from local, pinned dependencies; the user launcher does
not invoke pip, uv, a system Python, or a system VLC. Offline OCR model files live
inside `assets/ocr/ppocrv6_medium`. The older engine remains only for explicit
maintainer comparison; it is not a silent fallback when the v6 package is broken.

| Component | Bundled input / provenance |
|---|---|
| Python | Official CPython 3.13.15 Windows x64 embeddable archive, python.org |
| Python packages | Exact top-level pins in requirements-portable.txt; all installed distribution metadata/licenses retained |
| VLC | Complete private VLC distribution copied from the development machine; version recorded by portable self-test; COPYING.txt retained |
| FFmpeg/FFprobe | Gyan release essentials 9.0.1, linked from ffmpeg.org; LICENSE and README retained |
| OCR | RapidOCR 3.9.2, PP-OCRv6 medium detection and recognition, ONNX Runtime 1.29.0; exact model URLs and SHA-256 in assets/ocr/ppocrv6_medium/models.json |

FFmpeg input archive SHA-256:
`fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9`.
Every shipped file has a size and SHA-256 in package-manifest.json. Keep runtime
and vendor directories together with the launcher. Third-party software retains
its own notices and licenses; this project does not relicense those components.

Maintainer: prepare local runtime/vendor inputs, then run scripts/build_portable.py
with a fresh --out directory. Build refuses an existing destination. Runtime
acceptance is executed using the copied embedded interpreter with a minimal PATH.

The v6 medium adapter verifies model hashes and uses embedded recognition
dictionaries. Classification is disabled for timestamps, but the constructor's
required v4 classifier is included locally. The maintainer fetch script downloads
only the three fixed upstream assets and checks their official hashes; the user
launcher never runs that script. CPU detection is capped at a 1280-pixel long side
so a narrow timestamp crop is not inflated to a huge detection image.

3.9 removes the historical Python 3.8 event runtime and all trained behavior, calving decision, and temperature scoring weights from the distribution. The private Python 3.13 runtime includes scikit-learn and XGBoost 3.4.1; users train or manually import external numeric models. Generic OCR resources remain runtime components. Historical event runtime details are archived in earlier releases.
