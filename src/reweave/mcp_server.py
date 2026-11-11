"""Optional stdio adapter for four registered Engine operations.

Only the operator's launch command can choose the store or import a factory.
Tool arguments never select executable code, a module, a shell or a database.
"""

import argparse
import importlib
from pathlib import Path
import re
from typing import Any

from .engine import Engine
from .examples import make_stages
from .types import ReweaveError


def create_server(engine: Engine) -> Any:
    """Wrap one long-lived, thread-safe Engine using the optional MCP 2.3 SDK."""
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
        from pydantic import BaseModel, ConfigDict, Field, JsonValue
        from typing import Annotated
    except ImportError as exc:
        raise ReweaveError("Install the optional SDK from the source checkout: python -m pip install '.[mcp]'") from exc

    # Keep evaluated annotations: MCP introspects these local handler signatures.
    # Adding postponed string annotations here would hide these local model types
    # from inspect.signature(eval_str=True).
    Key = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]

    class RunRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        stage_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$", max_length=64)]
        input: JsonValue
        parents: list[Key] = Field(default_factory=list, max_length=engine.limits.max_parents)

    class WorkRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        work_key: Key

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                             idempotent_hint=False, open_world_hint=True)
    server = MCPServer("Atlas Reweave", version="0.1.0a1", subscriptions=False,
                       instructions="Run only registered trusted stages. Acceptance is versioned; inspect is historical metadata only.")

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ReweaveError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=read_only, structured_output=True)
    def list_stages() -> dict[str, JsonValue]:
        """List registered stages, input examples and explicit producer/verifier revisions."""
        return {"stages": invoke(engine.list_stages)}

    @server.tool(annotations=writes, structured_output=True)
    def run_registered_stage(request: RunRequest) -> dict[str, JsonValue]:
        """Run one registered stage; only a currently accepted receipt contains output."""
        return invoke(engine.run, request.stage_name, request.input, parents=request.parents).to_dict()

    @server.tool(annotations=writes, structured_output=True)
    def recheck_work(request: WorkRequest) -> dict[str, JsonValue]:
        """Revalidate stored work and its dependency closure under this server's registry."""
        return invoke(engine.revalidate, request.work_key).to_dict()

    @server.tool(annotations=read_only, structured_output=True)
    def inspect_receipt(request: WorkRequest) -> dict[str, JsonValue]:
        """Read historical metadata only, without output or a current-acceptance claim."""
        return invoke(engine.inspect, request.work_key)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True, help="Operator-selected local SQLite store")
    parser.add_argument("--scope", default="mcp-local", help="Fixed work scope for this server process")
    parser.add_argument("--integer-only", action="store_true", help="Use the fixtures' stricter integer-only acceptance policy")
    parser.add_argument("--factory", help="Trusted operator-selected module:function returning registered Stage objects")
    args = parser.parse_args(argv)
    if args.factory:
        if args.integer_only:
            parser.error("A custom factory owns its policies; do not combine --factory with --integer-only")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", args.factory):
            parser.error("Factory must have the form trusted.module:function")
        module, function = args.factory.split(":")
        factory = getattr(importlib.import_module(module), function)
        if not callable(factory):
            parser.error("The configured factory is not callable")
        stages = factory()
    else:
        stages = make_stages(args.integer_only)
    try:
        with Engine(args.store, stages, scope=args.scope) as engine:
            create_server(engine).run(transport="stdio")
    except ReweaveError as exc:
        parser.exit(2, f"reweave MCP: {exc}\n")


if __name__ == "__main__":
    main()
