"""Load packaged prompt text resources."""

from importlib.resources import files


def load(name: str) -> str:
    """Read a prompt resource as UTF-8 text."""
    return files(__package__).joinpath(name).read_text(encoding="utf-8").strip()
