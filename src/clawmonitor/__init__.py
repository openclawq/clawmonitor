from __future__ import annotations

__all__ = ["__version__"]

try:
    from importlib.metadata import PackageNotFoundError, version
except ImportError:  # pragma: no cover
    from importlib_metadata import PackageNotFoundError, version  # type: ignore

try:
    __version__ = version("clawmonitor")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"
