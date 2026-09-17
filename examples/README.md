# Local examples

Keep real sensor recordings and videos in this directory for local testing. They are intentionally ignored by Git because they may be large, private, or subject to dataset agreements.

Expected local layout:

```text
examples/
├── 2026-08-08 10_44_34.json
└── hiv00102.mp4
```

Launch both files together:

```powershell
cowmata-annotator --mode basic --lang zh `
  --json "examples\2026-08-08 10_44_34.json" `
  --video "examples\hiv00102.mp4"
```

Do not force-add source media, raw sensor exports, model weights, or personally identifiable data to Git. Publish only a small, explicitly cleared fixture if a future automated test genuinely requires one.
