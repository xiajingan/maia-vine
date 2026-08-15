#!/usr/bin/env python3
import argparse
import json

from mai_harness.runtime.application.required_secrets import resolve_required_secrets
from mai_harness.runtime.infrastructure.deploy_config import load_environments_compat


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("env")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--plain", action="store_true")
    output.add_argument("--json", action="store_true")
    args = parser.parse_args()
    entry = load_environments_compat().get("environments", {}).get(args.env)
    if not entry:
        raise SystemExit(f"environments.{args.env} 未在 deploy.yml 声明")
    secrets = resolve_required_secrets(args.env, entry)
    source = entry.get("secrets_source", "(未声明)")
    if args.json:
        print(json.dumps({"env": args.env, "secrets_source": source, "secrets": secrets}, ensure_ascii=False, indent=2))
    elif args.plain:
        print("\n".join(secrets))
    else:
        print(f"# {args.env} 环境必须配置的 {len(secrets)} 个 secret\n# 文件: {source}\n\n" + "\n".join(secrets))


if __name__ == "__main__":
    main()
