"""Safe structured observability without prompts, evidence, or credentials."""

import json
import logging
from typing import Protocol


class ResearchObserver(Protocol):
    def emit(self, event: str, attributes: dict[str, object]) -> None: ...


class NullObserver:
    def emit(self, event: str, attributes: dict[str, object]) -> None:
        pass


class LoggingObserver:
    """JSON event sink compatible with ordinary log collectors."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("lextrace.research")

    def emit(self, event: str, attributes: dict[str, object]) -> None:
        self.logger.info(
            json.dumps(
                {"event": event, **attributes},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
