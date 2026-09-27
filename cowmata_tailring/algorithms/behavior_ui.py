"""Compatibility proxy; implementation moved to the layered UI package."""
from importlib import import_module as _import_module

_impl = _import_module("cowmata_tailring.ui.algorithms.behavior_ui")
for _name, _value in vars(_impl).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__", "__file__"}:
        globals()[_name] = _value

__all__ = [name for name in vars(_impl) if not name.startswith("__")]

def __getattr__(name):
    return getattr(_impl, name)

def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))
