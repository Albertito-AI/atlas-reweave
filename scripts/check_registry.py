#!/usr/bin/env python3
"""Read the exact published metadata; exit 2 only for an absent Registry version."""
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen


def check(path):
    expected = json.loads(path.read_text())
    url = ("https://registry.modelcontextprotocol.io/v0.1/servers/" +
           quote(expected["name"], safe="") + "/versions/" + quote(expected["version"], safe=""))
    try:
        with urlopen(url, timeout=30) as response:
            actual = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return 2
        raise
    server = actual.get("server", actual)
    for field, value in expected.items():
        if server.get(field) != value:
            raise ValueError("Published Registry metadata differs: " + field)
    state = actual.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
    if state.get("status") != "active":
        raise ValueError("Registry version is not active")
    print(json.dumps({"url": url, "server": server, "registry_state": state}, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", type=Path)
    raise SystemExit(check(parser.parse_args().server))
