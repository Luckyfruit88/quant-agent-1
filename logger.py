import json
import logging
import sys
from datetime import datetime
from typing import Any, Dict, Optional


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("quant-bot")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "level": level.upper(),
        "msg": message,
    }
    if data is not None:
        payload["data"] = data

    log_method = getattr(logger, level.lower(), logger.info)
    log_method(json.dumps(payload, default=str))
