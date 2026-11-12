"""Logger adapter that carries jobId and stage context."""

import logging
from typing import Any, Dict, Tuple


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that injects jobId and stage into every log.

    Used to carry jobId and stage as context so they are automatically
    included in every log emitted through this adapter instance. A per-call
    `extra` overrides the adapter's values for the same key.
    """

    def process(
        self, msg: str, kwargs: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """Merge the adapter's context into the record's extra fields.

        Args:
            msg: The log message.
            kwargs: Keyword arguments passed to the logger. A per-call `extra` mapping
                takes precedence over the adapter's `jobId`/`stage` for the same key.

        Returns:
            Tuple of (msg, kwargs) where `kwargs["extra"]` holds the merged fields.
        """
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs
