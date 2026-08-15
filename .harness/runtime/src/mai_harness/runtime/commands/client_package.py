"""Publish immutable client packages through a configured artifact adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.infrastructure.adapters import FileArtifactStore, HttpArtifactStore, publish_client_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("source", type=Path)
    publish.add_argument("--store", type=Path)
    publish.add_argument("--endpoint")
    publish.add_argument("--token-env", default="")
    publish.add_argument("--key", required=True)
    publish.add_argument("--signature", type=Path, required=True)
    publish.add_argument("--sbom", type=Path, required=True)
    publish.add_argument("--channel", default="")
    args = parser.parse_args()
    if bool(args.store) == bool(args.endpoint):
        parser.error("--store 与 --endpoint 必须且只能指定一个")
    store = FileArtifactStore(args.store) if args.store else HttpArtifactStore(args.endpoint, args.token_env)
    result = publish_client_bundle(
        store,
        package=args.source,
        signature=args.signature,
        sbom=args.sbom,
        immutable_key=args.key,
        channel=args.channel,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
