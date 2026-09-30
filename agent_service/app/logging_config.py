"""Application-level logging.

Requests that reach this service are NOT covered by n8n's execution log for whatever
happens inside the service, so we log request lifecycle, agent runs, retries and errors.

Privacy note: at INFO level we log sizes and token counts, never prompt or response
content (program data may be sensitive). Content is logged only at DEBUG.
"""

import logging
import sys
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx logs every outbound request URL at INFO; keep it quieter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
