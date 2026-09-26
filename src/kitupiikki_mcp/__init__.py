"""kitupiikki-mcp – an MCP/agent interface for Kitsas (kitupiikki) bookkeeping files."""
from .book import KitsasBook, KitsasError

__all__ = ["KitsasBook", "KitsasError"]
__version__ = "0.1.0"
