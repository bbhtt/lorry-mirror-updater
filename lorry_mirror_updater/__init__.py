from importlib import metadata

__version__ = metadata.version(__package__ or "lorry_mirror_updater")
del metadata
