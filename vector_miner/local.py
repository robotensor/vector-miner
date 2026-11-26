"""Old import path of vector_miner.skills.bin_picking.expert.local."""
import sys

from .skills.bin_picking.expert import local as _module


def __getattr__(name):
    return getattr(_module, name)


sys.modules[__name__] = _module
