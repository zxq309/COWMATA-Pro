"""COWMATA headless algorithm engine: behaviour recognition + calving decision.

This package has no Qt dependency. The desktop UI and any external front-end call it only
through :mod:`cowmata_engine.api` (JSON in, JSON out) or ``python -m cowmata_engine``.
"""

ENGINE_API = "cowmata-engine-1"
__version__ = "4.3.3"
