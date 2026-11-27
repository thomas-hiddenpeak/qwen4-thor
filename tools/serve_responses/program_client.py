"""Strict Responses JSON and SSE types for clients reading programmatic tools.

Pass ProgramResponse or Stream[ProgramStreamEvent] to the OpenAI client's
raw response parse(to=...) method. Both names resolve to the
SDK's own types when it already understands programs.
"""

from typing import Annotated, Literal, Union, get_args

from openai import BaseModel
from openai.types.responses import Response, ResponseOutputItem, ResponseStreamEvent, Tool
from openai.types.responses import response_output_item
from pydantic import Field, create_model


if hasattr(response_output_item, "ProgramOutput"):
    ProgramResponse = Response
    ProgramStreamEvent = ResponseStreamEvent
else:
    class Program(BaseModel):
        type: Literal["program"]
        id: str
        call_id: str
        code: str
        fingerprint: str

    class ProgramOutput(BaseModel):
        type: Literal["program_output"]
        id: str
        call_id: str
        result: str
        status: Literal["completed", "incomplete"]

    class ProgrammaticTool(BaseModel):
        type: Literal["programmatic_tool_calling"]

    class DirectCaller(BaseModel):
        type: Literal["direct"]

    class ProgramCaller(BaseModel):
        type: Literal["program"]
        caller_id: str

    def extend_union(original, replacements, additions=()):
        # Generated SDK unions are Annotated[Union[...], discriminator].
        # Select the concrete model by its wire tag so an invalid caller
        # cannot fall through to a legacy model with an unchecked extra field.
        members = get_args(get_args(original)[0])
        variants = tuple(replacements.get(model, model) for model in members)
        return Annotated[Union[variants + additions], Field(discriminator="type")]

    from openai.types.responses import (
        CustomTool, FunctionTool, ResponseCustomToolCall, ResponseFunctionToolCall,
    )

    from openai.types.responses.response import ToolChoice

    caller = Annotated[DirectCaller | ProgramCaller, Field(discriminator="type")]
    calls = {
        model: create_model(model.__name__, __base__=model, caller=(caller | None, None),
                            namespace=(str | None, None))
        for model in (ResponseFunctionToolCall, ResponseCustomToolCall)
    }
    definitions = {
        model: create_model(model.__name__, __base__=model,
                            allowed_callers=(list[Literal["direct", "programmatic"]] | None, None))
        for model in (FunctionTool, CustomTool)
    }
    OutputItem = extend_union(ResponseOutputItem, calls, (Program, ProgramOutput))
    ProgramTool = extend_union(Tool, definitions, (ProgrammaticTool,))

    class ProgramResponse(Response):
        output: list[OutputItem]
        tools: list[ProgramTool]
        tool_choice: ToolChoice | ProgrammaticTool

    # Resource lifecycle events embed the whole Response. The two output
    # item events embed just one item. Delta and other events keep SDK types.
    events = {}
    for event in get_args(get_args(ResponseStreamEvent)[0]):
        fields = {}
        if "response" in event.model_fields:
            fields["response"] = (ProgramResponse, ...)
        if "item" in event.model_fields:
            fields["item"] = (OutputItem, ...)
        if fields:
            events[event] = create_model(event.__name__, __base__=event, **fields)
    ProgramStreamEvent = extend_union(ResponseStreamEvent, events)
