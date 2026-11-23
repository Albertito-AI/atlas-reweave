#!/usr/bin/env python3
"""Build a deterministic, review-pinned local MCPB without modifying a release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from zipfile import ZIP_STORED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
NAME = "io.github.Albertito-AI/atlas-reweave"
REPO = "Albertito-AI/atlas-reweave"
VERSION = re.compile(r"\d+\.\d+\.\d+-alpha\.\d+\Z")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def payloads(root=ROOT):
    mapping = {
        "manifest.json": "mcp/manifest.json", "pyproject.toml": "mcp/pyproject.toml",
        "uv.lock": "mcp/uv.lock", "server.py": "mcp/server.py",
        "README.md": "mcp/README.md", "LICENSE": "LICENSE",
        "docs/MCP.md": "docs/MCP.md", "docs/SECURITY.md": "SECURITY.md",
    }
    mapping.update({"reweave/" + p.name: str(p.relative_to(root))
                    for p in sorted((root / "src/reweave").glob("*.py"))})
    if len([n for n in mapping if n.startswith("reweave/")]) != 11:
        raise ValueError("Review the explicit module count before changing the bundle scope")
    return {name: (root / source).read_bytes() for name, source in sorted(mapping.items())}


def source_digest(root=ROOT):
    # Include the build/publish implementation in the reviewed release identity.
    data = payloads(root)
    for name in ("scripts/package_mcpb.py", "scripts/check_mcpb.py",
                 "scripts/publish_mcpb_release.py", "scripts/check_registry.py",
                 ".github/workflows/mcp-registry.yml"):
        data["review/" + name] = (root / name).read_bytes()
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in sorted(data.items())}
    return hashlib.sha256(encoded(hashes)).hexdigest()


def build(directory, root=ROOT):
    request = json.loads((root / "mcp/release-request.json").read_text())
    manifest = json.loads((root / "mcp/manifest.json").read_text())
    version = request["version"]
    if not VERSION.fullmatch(version) or manifest["version"] != version:
        raise ValueError("Release and manifest must name the same alpha bundle version")
    if request["registry_name"] != NAME or request["source_sha256"] != source_digest(root):
        raise ValueError("Release request does not match the reviewed source inputs")
    data = payloads(root)
    data["SOURCE-MANIFEST.json"] = encoded({name: hashlib.sha256(value).hexdigest()
                                           for name, value in data.items()})
    directory.mkdir(parents=True, exist_ok=True)
    filename = "atlas-reweave-" + version + ".mcpb"
    archive = directory / filename
    with ZipFile(archive, "w", compression=ZIP_STORED) as bundle:
        for name, value in sorted(data.items()):
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, value)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    tag = "mcp-v" + version
    url = f"https://github.com/{REPO}/releases/download/{tag}/{filename}"
    server = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": NAME, "title": "Atlas Reweave: Albert Tan Sin Hon",
        "description": "Versioned acceptance and durable receipts for trusted local agent computations.",
        "repository": {"url": "https://github.com/" + REPO, "source": "github"},
        "version": version, "websiteUrl": "https://alberttansinhon.com/#research-lab",
        "packages": [{"registryType": "mcpb", "identifier": url, "fileSha256": digest,
                      "transport": {"type": "stdio"}}],
        "_meta": {"io.modelcontextprotocol.registry/publisher-provided": {
            "com.alberttansinhon": {"author": "Albert Tan Sin Hon",
                "contact": "mailto:info@alberttansinhon.com", "sourceVersion": "0.1.0a1",
                "scope": "Local evaluation alpha; two tools can write to the selected SQLite store"}}},
    }
    (directory / "server.json").write_bytes(encoded(server))
    commit = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A full source commit is required")
    receipt = {"version": version, "source_version": "0.1.0a1", "registry_name": NAME,
               "tag": tag, "filename": filename, "source_commit": commit,
               "source_sha256": request["source_sha256"], "bundle_sha256": digest}
    (directory / "build-receipt.json").write_bytes(encoded(receipt))
    (directory / "SHA256SUMS").write_text(digest + "  " + filename + "\n")
    (directory / "release-notes.md").write_text(
        f"Atlas Reweave MCP bundle {version}, source version 0.1.0a1.\n\n"
        "Four tools through a local stdio process, using frozen MCP 2.3.0 dependencies. "
        "Running or rechecking work may write local SQLite records. Built-in stages are "
        "deterministic software fixtures. MIT licensed. No hosted service or model key is required.\n\n"
        "Read mcp/README.md for installation, tool scope and verification limits. A .mcpb file "
        "uses an MCPB/UV-aware client or the documented extracted-bundle command, not npx.\n\n"
        "Author: [Albert Tan Sin Hon](https://alberttansinhon.com/). "
        "Enquiries: info@alberttansinhon.com.\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-digest", action="store_true")
    args = parser.parse_args()
    if args.source_digest:
        print(source_digest())
    elif args.output:
        print(json.dumps(build(args.output), indent=2))
    else:
        parser.error("Specify --output or --source-digest")
