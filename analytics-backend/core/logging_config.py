"""
Structured logging configuration for Kapi Analytics Backend.
Outputs JSON in production, colored text in development.
"""
import logging
import json
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """JSON log formatter for production (parseable by log aggregators)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class ColorFormatter(logging.Formatter):
    """Colored text formatter for local development."""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = datetime.now().strftime("%H:%M:%S")
        name = record.name.replace("kapi.", "")
        msg = record.getMessage()
        base = f"{color}{ts} [{record.levelname:>7}]{self.RESET} {name}: {msg}"
        if record.exc_info and record.exc_info[1]:
            base += f"\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(debug: bool = False, json_output: bool = False) -> None:
    """Configure logging for the application."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # Remove existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)

    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(ColorFormatter())

    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for name in ("httpcore", "httpx", "urllib3", "sqlalchemy.engine", "multipart"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Uvicorn access logs — keep at INFO
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
