"""The one structured logger. `setup` once at start; `get_logger` for every
record the host writes.
"""

from forge.log import Logger, get_logger, setup

__all__ = ["Logger", "get_logger", "setup"]
