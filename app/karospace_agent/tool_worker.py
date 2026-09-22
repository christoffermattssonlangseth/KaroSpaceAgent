"""Execute one allowlisted tool in an interruptible local process.

JSON lines on stdout carry local progress and a raw local report. The parent
must run the report through privacy.Boundary before sending a model response.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Annotated, get_args, get_origin

import jsonschema
from pydantic import TypeAdapter

from . import commands, tools
from .privacy import PRIVACY_INSTRUCTIONS


def tool_specs() -> list[dict]:
    specs = []
    for tool in tools.ALL_TOOLS:
        properties = {}
        for name, annotation in tool.input_schema.items():
            schema = TypeAdapter(annotation).json_schema()
            if get_origin(annotation) is Annotated:
                descriptions = [x for x in get_args(annotation)[1:] if isinstance(x, str)]
                if descriptions:
                    schema["description"] = descriptions[0]
            properties[name] = schema
        specs.append({
            "type": "function", "name": tool.name, "description": tool.description + PRIVACY_INSTRUCTIONS,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(properties), "additionalProperties": False},
        })
    return specs


def main() -> None:
    lock = threading.Lock()

    def emit(event):
        with lock:
            print(json.dumps(event), flush=True)

    commands.set_progress_sink(
        lambda stream, line: emit({"kind": "progress", "stream": stream, "line": line})
    )
    try:
        request = json.loads(sys.stdin.readline())
        name, arguments = request["tool"], request["arguments"]
        tool = next(t for t in tools.ALL_TOOLS if t.name == name)
        spec = next(s for s in tool_specs() if s["name"] == name)
        jsonschema.validate(arguments, spec["inputSchema"])
        result = asyncio.run(tool.handler(arguments))
    except Exception as exc:
        result = {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}
    emit({"kind": "result", "result": result})


if __name__ == "__main__":
    main()
