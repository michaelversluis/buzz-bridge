"""buzz-bridge — connect any agent or LLM to a Buzz channel."""
__version__ = "0.1.0"
from .bridge import Bridge
from .config import Config

__all__ = ["Bridge", "Config", "__version__"]
