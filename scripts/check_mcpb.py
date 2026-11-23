#!/usr/bin/env python3
"""Verify reproducible bytes and exercise the extracted bundle's real stdio tools."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import ZipFile

import anyio
from mcp import Client, StdioServerParameters

from package_mcpb import ROOT, build, payloads

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
TOOLS = {
    "list_stages": (True, False, True, False),
    "run_registered_stage": (False, False, False, True),
    "recheck_work": (False, False, False, True),
    "inspect_receipt": (True, False, True, False),
}


async def exercise(bundle, outside, store, environment):
    manifest = json.loads((bundle / "manifest.json").read_text())
    config = manifest["server"]["mcp_config"]
    assert config["command"] == "uv"
    substitutions = {"${__dirname}": str(bundle),
                     "${user_config.store_directory}": str(store),
                     "${user_config.scope}": "bundle-smoke"}
    arguments = []
    for argument in config["args"]:
        for name, value in substitutions.items():
            argument = argument.replace(name, value)
        assert "${" not in argument, argument
        arguments.append(argument)
    parameters = StdioServerParameters(command=shutil.which("uv"), args=arguments,
                                       cwd=outside, env=environment)
    work_key = None
    observed = []
    for mode, protocol in (("auto", "2026-07-28"), ("legacy", "2025-11-25")):
        with anyio.fail_after(40):
            # The second process reopens the same durable local store.
            async with Client(parameters, mode=mode, read_timeout_seconds=10, cache=None) as client:
                assert client.protocol_version == protocol, client.protocol_version
                response = (await client.list_tools()).model_dump(
                    mode="json", by_alias=True, exclude_unset=True)
                tools = {tool["name"]: tool for tool in response["tools"]}
                assert set(tools) == set(TOOLS), tools
                for name, values in TOOLS.items():
                    hints = tools[name].get("annotations", {})
                    assert all(type(hints.get(hint)) is bool for hint in HINTS), hints
                    assert tuple(hints[hint] for hint in HINTS) == values, (name, hints)
                listed = await client.call_tool("list_stages", {})
                assert not listed.is_error, listed.content
                stages = {stage["name"]: stage for stage in listed.structured_content["stages"]}
                assert set(stages) == {"linear.solve", "linear.aggregate", "text.excerpt"}
                request = {"stage_name": "linear.solve", "input": stages["linear.solve"]["input_example"]}
                run = await client.call_tool("run_registered_stage", {"request": request})
                assert not run.is_error, run.content
                receipt = run.structured_content
                assert receipt["status"] == "accepted", receipt
                if work_key is not None:
                    assert receipt["work_key"] == work_key, receipt
                    assert receipt["generation_reused"] is True, receipt
                work_key = receipt["work_key"]
                inspected = await client.call_tool("inspect_receipt", {"request": {"work_key": work_key}})
                assert not inspected.is_error, inspected.content
                assert "output" not in inspected.structured_content
                assert "status" not in inspected.structured_content
                rechecked = await client.call_tool("recheck_work", {"request": {"work_key": work_key}})
                assert not rechecked.is_error, rechecked.content
                assert rechecked.structured_content["status"] == "accepted"
                rejected = await client.call_tool("run_registered_stage", {
                    "request": {**request, "factory": "untrusted:code"}})
                assert rejected.is_error and rejected.structured_content is None
                observed.append({"protocol": protocol, "tools": sorted(tools),
                                 "status": receipt["status"], "restart_reused": mode == "legacy"})
    assert (store / "reweave.sqlite3").is_file()
    return observed


def check(directory):
    assert metadata.version("mcp") == "2.3.0"
    assert shutil.which("uv"), "UV is required to test the actual manifest command"
    receipt = json.loads((directory / "build-receipt.json").read_text())
    archive = directory / receipt["filename"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["bundle_sha256"]
    with tempfile.TemporaryDirectory(prefix="reweave-bundle-") as temporary:
        root = Path(temporary).resolve()
        assert not root.is_relative_to(ROOT)
        second = root / "rebuilt"
        build(second)
        for name in (receipt["filename"], "server.json", "SHA256SUMS"):
            assert (second / name).read_bytes() == (directory / name).read_bytes(), name
        extracted = root / "extracted"
        expected = payloads()
        with ZipFile(archive) as package:
            names = package.namelist()
            assert len(names) == len(set(names))
            assert set(names) == set(expected) | {"SOURCE-MANIFEST.json"}, names
            for name, data in expected.items():
                assert package.read(name) == data, name
            hashes = json.loads(package.read("SOURCE-MANIFEST.json"))
            assert hashes == {name: hashlib.sha256(data).hexdigest() for name, data in expected.items()}
            # Extract only after checking the complete explicit filename allowlist.
            package.extractall(extracted)
        outside, store = root / "outside-checkout", root / "private-work"
        outside.mkdir()
        store.mkdir()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.pop("VIRTUAL_ENV", None)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["UV_PYTHON_DOWNLOADS"] = "never"
        # Resolve the frozen runtime before the bounded protocol handshake.
        subprocess.run(["uv", "sync", "--frozen", "--project", str(extracted)],
                       cwd=outside, env=environment, check=True, timeout=180)
        observed = anyio.run(exercise, extracted, outside, store, environment)
    result = {"bundle_sha256": receipt["bundle_sha256"], "reproducible": True,
              "outside_checkout": True, "mcp_version": "2.3.0", "observed": observed}
    (directory / "package-check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    check(parser.parse_args().dist.resolve())
