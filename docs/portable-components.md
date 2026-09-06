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

The a5 event pack additionally carries a separate official CPython 3.8.10 x64
embeddable runtime for the user's scikit-learn 0.24.1 serialized models. Its
archive is from https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip
with SHA-256 `abbe314e9b41603dde0a823b76f5bbbe17b3de3e5ac4ef06b759da5466711271`.
Pinned worker dependencies are in requirements-events-20260906.txt. It is an
offline, CPU-limited local worker, not the GUI runtime or an arbitrary-code
sandbox; Python 3.8 is end-of-life. No changes to the user's system environment
are needed. User-supplied reviewed event sources/models retain exact content
hashes in assets/event_models/20260906/pack.json; no training dataset is bundled.
