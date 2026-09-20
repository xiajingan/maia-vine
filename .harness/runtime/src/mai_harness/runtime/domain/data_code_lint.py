"""Golden Rule decisions over language-specific syntax facts."""

from __future__ import annotations

from mai_harness.runtime.domain.data_policy import is_business_id
from mai_harness.runtime.domain.data_sql_lint import check_sql


def check_fact(fact: dict, policy: dict) -> list[dict]:
    issues: list[tuple[int, str, str]] = []
    kind, line = fact["kind"], fact["line"]
    ids = policy.get("business_ids")
    if kind == "sql" and policy.get("database"):
        issues.extend(check_sql(fact["text"], policy, line))
    elif kind == "parse_error":
        issues.append((line, "G-10", f"源文件语法错误：{fact['message']}"))
    elif kind == "dynamic_sql" and policy.get("database"):
        issues.append((line, "G-10", "动态原生 SQL 无法静态检查；使用参数化 SQL builder 或固定 SQL 文件"))
    elif kind == "relation" and policy.get("forbid_drizzle_relations"):
        message = "关系查询 options 无法静态展开" if fact["dynamic"] else "禁止 Drizzle 自动关系加载 with"
        issues.append((line, "G-10", message + "；使用显式 select/join"))
    elif ids and (
        is_business_id(fact.get("name", ""), policy)
        or is_business_id(fact.get("resolvedName", ""), policy)
        or (kind == "id_builder" and is_business_id(fact.get("columnName", ""), policy))
    ):
        if kind == "numeric_id" and (ids["storage_type"].lower() == "bigint" or ids["wire_type"] == "string"):
            issues.append((line, "G-11", f"业务 ID {fact['name']} 禁止 {fact['operation']}"))
        if kind == "id_builder":
            if (
                fact["builder"] == "bigint"
                and (policy.get("database") or {}).get("orm") == "drizzle"
                and ids["storage_type"].lower() == "bigint"
                and fact.get("mode") != "bigint"
            ):
                issues.append((line, "G-11", f"业务 ID {fact['name']} 的 Drizzle bigint 必须显式使用 mode: bigint"))
            if fact["numeric"] and (ids["storage_type"].lower() == "bigint" or ids["wire_type"] == "string"):
                issues.append((line, "G-11", f"业务 ID {fact['name']} 不得映射为 JS number/数值 Schema"))
            generators = {g.lower() for g in fact["generators"]}
            for generator in ids["forbidden_generators"]:
                if generator.lower() in generators:
                    issues.append((line, "G-11", f"业务 ID {fact['name']} 禁止生成方式 {generator}"))
    return [{"file": fact["file"], "line": pos, "rule": rule, "message": message} for pos, rule, message in issues]
