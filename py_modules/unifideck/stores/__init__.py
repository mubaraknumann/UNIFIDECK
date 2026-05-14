"""Store backends — one subpackage per store (Epic, GOG, Amazon, Microsoft xCloud, Ubisoft Connect). Discovered at runtime by StoreRegistry."""

from .shared import StoreBase, StoreRegistry
__all__ = [
    "StoreBase",
    "StoreRegistry",
]