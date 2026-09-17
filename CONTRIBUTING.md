# Contributing to COWMATA Pro

## Development

1. Branch from `main`.
2. Install Python 3.12+ and `pip install -e ".[dev]"`; see [README](README.md) for media, OCR and model configuration.
3. Update behavior tests for functional changes.
4. Run `ruff check cowmata_tailring tests` and `pytest -q`.
5. For interface changes, verify the corresponding window and use synthetic data for public screenshots.

## Data and model contracts

- Preserve original Motion, PPG and Temp JSON content, identities and timestamps.
- Keep machine event codes stable across interface languages.
- Confirm video/signal alignment before accepting synchronized labels; drafts retain their unconfirmed state.
- Model candidates enter human review and must not overwrite confirmed labels.
- Keep training and recognition separate. Trained behavior and decision models remain outside source and program packages.
- Separate cows between decision training and evaluation; record missing inputs and actual grouping units.
- Preserve existing projects and unknown user files during updates.

Do not commit real farm recordings, private CSVs, credentials, trained weights or generated caches. Tiny test fixtures must be synthetic or explicitly cleared.

## Documentation

The current [README](README.md) is the single project overview. Keep the changelog, release notes and current tutorial consistent with delivered behavior. Historical repository explanations are indexed in [repository consolidation](docs/project/repository-consolidation.md); preserve their provenance and licenses. Do not import old distribution bundles as documentation.

## Bug reports

Include software version, operating system, reproduction steps, error text and a minimal sample with private information removed. Reports should distinguish a functional failure from model evaluation results.
