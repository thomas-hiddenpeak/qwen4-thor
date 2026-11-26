"""Typed standalone compaction windows for clients using the OpenAI Python SDK."""
from typing import Literal

from openai.types.responses import CompactedResponse, ResponseInputMessageItem, ResponseOutputItem


class RetainedMessage(ResponseInputMessageItem):
    """An input message retained as an identified resource in a compact window."""

    status: Literal["in_progress", "completed", "incomplete"] = ...


class CompactWindow(CompactedResponse):
    """The HTTP compact resource can contain both retained inputs and outputs."""

    output: list[RetainedMessage | ResponseOutputItem]
