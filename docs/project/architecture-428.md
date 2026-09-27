# CoWmata Pro 4.2.8 architecture

4.2.8 uses three explicit layers:

- cowmata_tailring/algorithms contains feature extraction, model loading, training and inference contracts. It has no research cache or generated figures.
- cowmata_tailring/workspace and algorithms/runner.py provide dataset scanning, model versioning, cancellable workers, candidate review queues and decision outputs.
- cowmata_tailring/ui and the workspace windows provide tabs, tables, curves and review actions. They call services instead of importing research folders.

The catalog in algorithms/behavior_library.py covers standing, lying, straining, urination, fetal-part-first-visible and calf-fully-expelled. A versioned suite in the external model home is the only runtime model input. The same suite hash is attached to prediction, evidence and decision output.

## External state

Set COWMATA_ALGORITHM_HOME to the shared model directory, for example the configured behavior recognition directory under the customer model root. Set COWMATA_DATA_HOME for caches and user state, and COWMATA_DATASET_HOME for training datasets. Portable deployments can set COWMATA_PORTABLE_DATA=1 to keep state beside the application. No source module contains a developer drive or a research checkout.

Generated model files, validation tables, plots, OOF files and review queues belong in the algorithm home versions, experiments and runs directories. They are never copied into the source distribution.

## Candidate semantics

CandidateWindow and algorithms/candidate_service.py write only pending candidate records. A candidate is not a label, a probability, or a negative example. The queue includes the source digest, suite digest, model version and manual review status. Only a human-confirmed draft can enter annotation data.

## Reproduction and health

The calving evidence and decision tabs use the same external behavior suite and retain model version/hash in their reports. Event detection does not claim pregnancy or disease status; those health contracts remain separate and must be registered before they can generate a health conclusion.