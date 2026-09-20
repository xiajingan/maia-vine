"""Token-aware checks of declared SQL bans; not a complete SQL compatibility parser."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mai_harness.runtime.domain.data_policy import is_business_id


@dataclass(frozen=True)
class Token:
    text: str
    kind: str
    line: int


def tokens(sql: str, line: int = 1) -> list[Token]:
    result: list[Token] = []
    pattern = re.compile(
        r"(?P<space>\s+)|(?P<comment>--[^\n]*|\#[^\n]*|/\*[\s\S]*?\*/)"
        r"|(?P<string>'(?:''|\\.|[^'\\])*'|\$\$[\s\S]*?\$\$|\$(?P<tag>[A-Za-z_][\w]*)\$[\s\S]*?\$(?P=tag)\$)"
        r'|(?P<identifier>`(?:``|[^`])*`|"(?:""|[^"])*")'
        r"|(?P<word>[A-Za-z_][\w$]*)|(?P<symbol>->>|->|.)",
        re.DOTALL,
    )
    cursor = 0
    while cursor < len(sql):
        match = pattern.match(sql, cursor)
        if not match:
            raise ValueError(f"SQL:{line}: 无法分词")
        text, kind = match.group(), match.lastgroup
        if text.startswith("/*!") or text.startswith("/*T!"):
            result.append(Token("TIDB_EXTENSION" if text.startswith("/*T!") else "EXECUTABLE_COMMENT", "marker", line))
            body = re.sub(r"^/\*(?:T!)?!\s*\d*", "", text) if text.startswith("/*!") else text[4:]
            result.extend(tokens(body[:-2], line))
        elif kind not in {"space", "comment", "string"}:
            value = text[1:-1] if kind == "identifier" else text
            result.append(Token(value, kind or "symbol", line))
        elif kind == "string":
            result.append(Token("<literal>", "string", line))
        if text[:1] in {"'", "`", '"'} and kind == "symbol":
            raise ValueError(f"SQL:{line}: 字符串或标识符未闭合")
        if text == "/" and sql[cursor : cursor + 2] == "/*":
            raise ValueError(f"SQL:{line}: 注释未闭合")
        line += text.count("\n")
        cursor = match.end()
    return result


def check_sql(sql: str, policy: dict, line: int = 1) -> list[tuple[int, str, str]]:
    parts = tokens(sql, line)
    issues: list[tuple[int, str, str]] = []
    objects = {v.upper() for v in policy.get("forbidden_database_objects", [])}
    functions = {v.upper() for v in policy.get("forbidden_sql_functions", [])}
    features = set(policy.get("forbidden_sql_features", []))
    for i, token in enumerate(parts):
        value = token.text.upper()
        following = parts[i + 1].text.upper() if i + 1 < len(parts) else ""
        if token.kind == "word" and value in {"CREATE", "ALTER"}:
            for candidate in parts[i + 1 :]:
                if candidate.text.upper() in {"TABLE", "VIEW", "INDEX", "DATABASE", "SCHEMA", "USER"}:
                    break
                if candidate.text in {";", "("}:
                    break
                if candidate.kind == "word" and candidate.text.upper() in objects:
                    issues.append((token.line, "G-9", f"禁止数据库对象 {candidate.text.upper()}"))
                    break
        if token.kind == "word" and value == "CALL" and "PROCEDURE" in objects:
            issues.append((token.line, "G-9", "禁止调用存储过程"))
        if token.kind in {"word", "identifier"} and value in functions and following == "(":
            issues.append((token.line, "G-10", f"禁止 SQL 函数 {value}"))
        feature = None
        if token.kind == "word" and value in {"FULLTEXT", "LATERAL"}:
            feature = value.lower()
        elif token.kind == "word" and value == "MATCH" and following == "(":
            feature = "fulltext"
        elif token.kind == "word" and value == "SKIP" and following == "LOCKED":
            feature = "skip_locked"
        elif token.kind in {"word", "marker"} and value in {
            "TIDB_EXTENSION",
            "AUTO_RANDOM",
            "TIFLASH",
            "SHARD_ROW_ID_BITS",
        }:
            feature = "tidb_extensions"
        elif token.kind == "symbol" and value in {"->", "->>"}:
            feature = "json_operators"
        if feature in features:
            issues.append((token.line, "G-10", f"禁止 SQL 能力 {feature}"))
    issues.extend(check_id_columns(parts, policy))
    return sorted(set(issues))


def check_id_columns(parts: list[Token], policy: dict) -> list[tuple[int, str, str]]:
    """Inspect CREATE column definitions and ALTER ADD/MODIFY/CHANGE, not query identifiers."""
    if not policy.get("business_ids"):
        return []
    issues: list[tuple[int, str, str]] = []
    ids = policy["business_ids"]
    ddl, in_columns, depth, start, changing = False, False, 0, False, False
    for i, token in enumerate(parts):
        word = token.text.upper()
        if word == ";":
            ddl, in_columns, depth, start, changing = False, False, 0, False, False
        if token.kind == "word" and word in {"CREATE", "ALTER"}:
            ddl = True
        if not ddl:
            continue
        if token.kind == "word" and word == "TABLE":
            in_columns = True
        if not in_columns:
            continue
        if word == "(":
            depth += 1
            start = in_columns and depth == 1
            continue
        if word == ")":
            depth -= 1
        if word == ",":
            start = depth == 1
            continue
        if token.kind == "word" and word in {"ADD", "MODIFY", "CHANGE", "COLUMN"} and depth == 0:
            start = True
            changing = changing or word == "CHANGE"
            continue
        if not start:
            continue
        if changing:
            changing = False  # CHANGE [COLUMN] old_name new_name type: inspect new_name.
            continue
        start = False
        if not is_business_id(token.text, policy) or i + 1 >= len(parts):
            continue
        column_type = parts[i + 1].text.upper()
        expected = ids["storage_type"].upper()
        if column_type != expected:
            issues.append((token.line, "G-11", f"业务 ID 列 {token.text} 必须使用 {expected}，当前 {column_type}"))
        column = []
        for following in parts[i + 1 :]:
            if following.text in {",", ";"}:
                break
            column.append(following.text.lower())
        for generator in ids["forbidden_generators"]:
            if generator.lower() in column:
                issues.append((token.line, "G-11", f"业务 ID 列 {token.text} 禁止生成方式 {generator}"))
    return issues
