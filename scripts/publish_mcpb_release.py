#!/usr/bin/env python3
"""Publish/resume one exact alpha release with the job-scoped GitHub token."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = "Albertito-AI/atlas-reweave"
API = "https://api.github.com/repos/" + REPO


def api(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(API + path, body, method=method, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if method == "GET" and error.code == 404:
            return None
        raise RuntimeError(f"GitHub {method} {path} returned HTTP {error.code}") from None


def main(directory):
    receipt = json.loads((directory / "build-receipt.json").read_text())
    if (os.environ.get("GITHUB_REPOSITORY") != REPO or
            os.environ.get("GITHUB_REF") != "refs/heads/main" or
            os.environ.get("GITHUB_REF_PROTECTED") != "true"):
        raise ValueError("Publication requires the owner repository's protected main branch")
    sha = os.environ["GITHUB_SHA"]
    if receipt["source_commit"] != sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("The built source is not the selected workflow revision")
    main_branch = api("GET", "/branches/main")
    if not main_branch or not main_branch.get("protected") or main_branch["commit"]["sha"] != sha:
        raise ValueError("Publication requires the current protected main commit")
    tag, filename = receipt["tag"], receipt["filename"]
    version = receipt["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+-alpha\.\d+", version):
        raise ValueError("Only explicitly versioned alpha bundles are supported")
    if tag != "mcp-v" + version or filename != "atlas-reweave-" + version + ".mcpb":
        raise ValueError("Unexpected release identity")
    if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != receipt["bundle_sha256"]:
        raise ValueError("Bundle changed after build")
    release = api("GET", "/releases/tags/" + tag)
    existing_tag = api("GET", "/git/ref/tags/" + tag)
    if existing_tag and (existing_tag["object"]["type"] != "commit" or existing_tag["object"]["sha"] != sha):
        raise ValueError("Existing tag differs; no overwrite permitted")
    if release is None:
        release = api("POST", "/releases", {
            "tag_name": tag, "target_commitish": sha,
            "name": "Atlas Reweave MCP " + version,
            "body": (directory / "release-notes.md").read_text(),
            "draft": False, "prerelease": True, "make_latest": "false"})
    if release.get("draft") or release.get("prerelease") is not True:
        raise ValueError("Existing release state differs; no implicit alteration permitted")
    assets = {asset["name"]: asset for asset in release["assets"]}
    for name in (filename, "server.json", "SHA256SUMS", "build-receipt.json", "package-check.json"):
        data = (directory / name).read_bytes()
        if name in assets:
            with urlopen(assets[name]["browser_download_url"], timeout=30) as response:
                existing = response.read(8 * 1024 * 1024)
            if existing != data:
                raise ValueError("Existing asset differs; no overwrite permitted: " + name)
            continue
        url = f"https://uploads.github.com/repos/{REPO}/releases/{release['id']}/assets?name={name}"
        request = Request(url, data, method="POST", headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json", "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=60) as response:
            asset = json.load(response)
        if asset["name"] != name or asset["size"] != len(data):
            raise ValueError("Uploaded asset identity or size differs")
    with urlopen(f"https://github.com/{REPO}/releases/download/{tag}/{filename}", timeout=30) as response:
        published = response.read(8 * 1024 * 1024)
    if hashlib.sha256(published).hexdigest() != receipt["bundle_sha256"]:
        raise ValueError("Published bundle failed byte verification")
    published_tag = api("GET", "/git/ref/tags/" + tag)
    if not published_tag or published_tag["object"]["type"] != "commit" or published_tag["object"]["sha"] != sha:
        raise ValueError("Published tag does not identify the tested source")
    print(json.dumps({"release": release["html_url"], "source_commit": sha,
                      "bundle_sha256": receipt["bundle_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    main(parser.parse_args().dist)
