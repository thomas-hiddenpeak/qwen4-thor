"""Typed standalone compaction windows for clients using the OpenAI Python SDK."""
from typing import Literal

from openai import BaseModel
from openai.types import responses
from openai.types.responses import ResponseInputMessageItem, ResponseOutputItem, ResponseUsage
from openai.types.responses.response_output_item import McpCall


# Saved windows can predate the reader SDK. Preserve both wire forms of the
# error, including the literal string in a window signed for an older client.
if hasattr(responses, "McpToolCallError"):
    StructuredError = responses.McpToolCallError
else:
    class StatusError(BaseModel):
        type: Literal["mcp_protocol_error", "http_error"]
        code: int
        message: str

    class ExecutionError(BaseModel):
        type: Literal["mcp_tool_execution_error"]
        content: object

    StructuredError = StatusError | ExecutionError


class StoredMcpCall(McpCall):
    error: str | StructuredError | None = None


class RetainedMessage(ResponseInputMessageItem):
    """An input message retained as an identified resource in a compact window."""

    status: Literal["in_progress", "completed", "incomplete"] = ...


if hasattr(responses, "CompactedResponse"):
    class CompactWindow(responses.CompactedResponse):
        """The compact resource can contain both retained inputs and outputs."""

        output: list[RetainedMessage | StoredMcpCall | ResponseOutputItem]
else:
    # Before the SDK added /responses/compact, it also had no output types
    # for the sealed summary or the retained approval decision.
    class CompactionItem(BaseModel):
        id: str
        type: Literal["compaction"]
        encrypted_content: str
        created_by: str | None = None

    class ApprovalDecision(BaseModel):
        id: str
        type: Literal["mcp_approval_response"]
        approval_request_id: str
        approve: bool
        reason: str | None = None

    class CompactWindow(BaseModel):
        id: str
        object: Literal["response.compaction"]
        created_at: int
        output: list[RetainedMessage | StoredMcpCall | CompactionItem |
                     ApprovalDecision | ResponseOutputItem]
        usage: ResponseUsage
