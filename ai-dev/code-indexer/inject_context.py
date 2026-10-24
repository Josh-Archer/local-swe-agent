#!/usr/bin/env python3
"""Insert retrieved code into SWE-agent system_template blocks.

Uses optional PyYAML for structural YAML redaction and falls back to
syntax-aware text rules when it is unavailable. The same file is mounted
from the swe-agent ConfigMap at /config/inject_context.py.
"""

import os
import re
import sys
import json
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_MAX_INJECT_BYTES: int = 16384  # 16 KiB default byte budget
DEFAULT_MAX_INJECT_TOKENS: int = 2048  # Default token/word estimate budget


def _sub_if(needles, pattern, repl, text):
    """re.sub that skips the scan when no literal needle is present.

    Needles are lowercase literals every match of ``pattern`` must contain
    (patterns using them are case-insensitive), so skipping is exact.
    """
    # re.IGNORECASE also matches dotless U+0131 against "i"; casefold keeps it.
    lowered = text.casefold().replace("\u0131", "i")
    if isinstance(needles, str):
        needles = (needles,)
    if not any(needle in lowered for needle in needles):
        return text
    return re.sub(pattern, repl, text)


def redact_secrets(text: str) -> str:
    """Redact secret keys, tokens, credentials, and private keys from text."""
    if not text:
        return ""

    def apply_edits(source, edits, replacement):
        if not edits:
            return source
        pieces = []
        cursor = 0
        for start, end in sorted(edits):
            if start < cursor:
                continue
            pieces.extend((source[cursor:start], replacement))
            cursor = end
        pieces.append(source[cursor:])
        return "".join(pieces)

    go_tag_lines = []

    def protect_go_tag(match):
        token = f"__CODE_INDEXER_GO_TAG_{len(go_tag_lines)}__"
        go_tag_lines.append(match.group(0))
        return token

    if '`json:"' in text:
        text = re.sub(
            r'(?m)^.*\b[A-Z][A-Za-z0-9_]*\s+[A-Za-z0-9_*]+\s+`json:"[^"\n]+"`.*$',
            protect_go_tag,
            text,
        )

    # Scan key markers once. A lazy BEGIN-to-END regex retries from every
    # unterminated BEGIN marker and becomes quadratic on damaged chunks.
    key_marker = re.compile(
        r"-----((?:BEGIN|END)\s+(?:[A-Z0-9_]+\s+)*PRIVATE KEY(?:\s+BLOCK)?)-----",
        re.IGNORECASE,
    )
    key_edits = []
    key_start = None
    for marker in key_marker.finditer(text):
        if marker.group(1).upper().startswith("BEGIN"):
            if key_start is None:
                key_start = marker.start()
        elif key_start is not None:
            key_edits.append((key_start, marker.end()))
            key_start = None
        elif not key_edits and marker.start() > 0:
            # A chunk containing only the tail of a split block is sensitive.
            key_edits.append((0, marker.end()))
    if key_start is not None:
        key_edits.append((key_start, len(text)))
    text = apply_edits(text, key_edits, "[REDACTED_PRIVATE_KEY]")

    # Certificate blocks use the same marker scan. An END-only chunk is
    # redacted only when its prefix resembles encoded certificate data, so a
    # source constant such as PEM_FOOTER remains indexable.
    cert_marker = re.compile(
        r"-----((?:BEGIN|END)\s+[A-Z0-9_\s]*CERTIFICATE)-----", re.IGNORECASE
    )
    cert_edits = []
    cert_start = None
    end_only_checked = False
    for marker in cert_marker.finditer(text):
        if marker.group(1).upper().startswith("BEGIN"):
            if cert_start is None:
                cert_start = marker.start()
        elif cert_start is not None:
            cert_edits.append((cert_start, marker.end()))
            cert_start = None
        elif not cert_edits and not end_only_checked and marker.start() > 0:
            end_only_checked = True
            prefix = text[: marker.start()]
            encoded_lines = [
                line.strip() for line in prefix.splitlines() if line.strip()
            ]
            if encoded_lines and all(
                re.fullmatch(r"[A-Za-z0-9+/=]+", line) for line in encoded_lines
            ):
                cert_edits.append((0, marker.end()))
    if cert_start is not None:
        cert_edits.append((cert_start, len(text)))
    text = apply_edits(text, cert_edits, "[REDACTED_CERTIFICATE]")

    # 3. AWS access keys
    text = re.sub(r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]", text)

    # 4. GitHub tokens
    text = re.sub(
        r"\b(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        "[REDACTED_GITHUB_TOKEN]",
        text,
    )

    # 5. Slack tokens
    text = re.sub(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b", "[REDACTED_SLACK_TOKEN]", text)

    # 6. Credentials embedded in URLs and authorization headers.
    text = _sub_if(
        "://",
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^:/\s@]*:)[^@\s]+(@)",
        r"\g<1>\g<2>[REDACTED_SECRET]\g<3>",
        text,
    )
    text = _sub_if(
        "://",
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^:/\s@]{16,})(@)",
        r"\g<1>[REDACTED_SECRET]\g<3>",
        text,
    )
    text = _sub_if(
        "authorization",
        r"(?i)\b(authorization\s*:\s*)(?!str\s*=|string\s*=)([a-z][a-z0-9_-]*\s+)[^\s\r\n,\"'`;}]+",
        r"\g<1>\g<2>[REDACTED_TOKEN]",
        text,
    )
    # Standalone Bearer tokens
    text = _sub_if(
        "bearer",
        r"(?i)\b(bearer\s+)(?!authentication\b)[A-Za-z0-9_\-\.]{8,}\b",
        r"\g<1>[REDACTED_TOKEN]",
        text,
    )

    # OpenAI-style secret keys can occur outside key/value assignments.
    text = re.sub(
        r"\bsk-(?!toggleable(?:__|\b))[A-Za-z0-9_-]{16,}\b",
        "[REDACTED_API_KEY]",
        text,
    )

    # YAML snippets are often embedded in retrieved Markdown. Redact each
    # fenced YAML body through the same structural path as a standalone file.
    yaml_fence = re.compile(
        r"(?ims)^(?P<open>[ \t]*```(?:yaml|yml)[ \t]*\r?\n)(?P<body>.*?)(?P<close>^[ \t]*```[ \t]*$)"
    )
    text = yaml_fence.sub(
        lambda match: match.group("open")
        + redact_secrets(match.group("body"))
        + match.group("close"),
        text,
    )

    # Structural passes preserve JSON types and YAML syntax. They are an
    # additional layer: code and shell patterns below always run afterward.
    secret_name_re = re.compile(
        r"(?i)(?:password|passwd|passphrase|pwd|secret|token|"
        r"(?:[a-z0-9]+[._-])*[a-z0-9]*(?:password|passwd|passphrase|pwd|secret|token)|"
        r"(?:[a-z0-9]+[._-])*api[-_]?key|(?:[a-z0-9]+[._-])*x[-_]api[-_]key|"
        r"(?:[a-z0-9]+[._-])*[a-z0-9]*_key|"
        r"secret_key_base|aws_secret_access_key|aws_access_key_id)"
    )

    def is_secret_name(name):
        normalized = name.replace("\ufeff", "") if isinstance(name, str) else ""
        if normalized.lower() in {
            "existingsecret",
            "pagetoken",
            "primary_key",
            "routing-key",
            "cache_key",
        }:
            return False
        return bool(secret_name_re.fullmatch(normalized))

    def auth_redacted(value):
        scheme = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s+", value)
        return (scheme.group(1) + " " if scheme else "") + "[REDACTED_TOKEN]"

    candidate = text.lstrip()
    yaml_structural_ok = False
    if candidate.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            stack = [parsed]
            changed = False
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    for key, value in list(item.items()):
                        normalized_key = (
                            key.replace("\ufeff", "") if isinstance(key, str) else key
                        )
                        secret_key = is_secret_name(normalized_key)
                        if (
                            isinstance(key, str)
                            and key.lower() == "name"
                            and isinstance(value, str)
                            and is_secret_name(value)
                            and isinstance(item.get("value"), str)
                            and item["value"]
                        ):
                            item["value"] = "[REDACTED_SECRET]"
                            changed = True
                        if (
                            isinstance(key, str)
                            and key.lower() == "authorization"
                            and isinstance(value, str)
                            and value
                        ):
                            item[key] = auth_redacted(value)
                            changed = True
                        elif (
                            isinstance(key, str)
                            and secret_key
                            and isinstance(value, str)
                            and value
                        ):
                            item[key] = "[REDACTED_SECRET]"
                            changed = True
                        elif secret_key and isinstance(value, (dict, list)):
                            # Keep the structure valid; redact string leaves only.
                            leaves = [value]
                            while leaves:
                                node = leaves.pop()
                                pairs = (
                                    node.items()
                                    if isinstance(node, dict)
                                    else enumerate(node)
                                )
                                for sub_key, sub_value in list(pairs):
                                    if isinstance(sub_value, str) and sub_value:
                                        node[sub_key] = "[REDACTED_SECRET]"
                                        changed = True
                                    elif isinstance(sub_value, (dict, list)):
                                        leaves.append(sub_value)
                        elif isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(item, list):
                    if (
                        item
                        and isinstance(item[0], str)
                        and item[0].lower() == "sshpass"
                    ):
                        for index, arg in enumerate(item[:-1]):
                            if (
                                arg == "-p"
                                and isinstance(item[index + 1], str)
                                and item[index + 1]
                            ):
                                item[index + 1] = "[REDACTED_SECRET]"
                                changed = True
                    stack.extend(
                        value for value in item if isinstance(value, (dict, list))
                    )
            if changed:
                newline_indent = re.search(r"\n([ \t]+)\"", text)
                pretty = newline_indent is not None
                indent = len(newline_indent.group(1)) if pretty else None
                text = json.dumps(
                    parsed,
                    ensure_ascii=False,
                    indent=indent,
                    separators=(",", ": ") if pretty else (",", ":"),
                )
        except (ValueError, TypeError, RecursionError):
            pass
    else:
        yaml_input = text.replace("\ufeff", "")
        looks_yaml = not re.search(r'`[A-Za-z_][\w-]*:"[^"\n]*"`', text) and (
            candidate.startswith("---")
            or re.search(
                r"(?m)^\s*(?:-\s+)?(?:['\"][^'\"\n]+['\"]|[A-Za-z_][\w.-]*)\s*:",
                yaml_input,
            )
        )
        if looks_yaml:
            try:
                import yaml

                # libyaml ignores U+FEFF in mark offsets. Strip every one for
                # parsing and retain a boundary map so edits still target the
                # original text. Keep the C loader's linear-time performance.
                offset_map = None
                bom_prefix = None
                if "\ufeff" in text:
                    clean_chars = []
                    offset_map = []
                    bom_prefix = []
                    bom_count = 0
                    for index, char in enumerate(text):
                        if char == "\ufeff":
                            bom_count += 1
                        else:
                            clean_chars.append(char)
                            offset_map.append(index)
                            bom_prefix.append(bom_count)
                    ytext = "".join(clean_chars)
                else:
                    ytext = text
                loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
                roots = list(yaml.compose_all(ytext, Loader=loader))
                yaml_structural_ok = True
                edits = []
                seen = set()

                def add_scalar(
                    node, marker="[REDACTED_SECRET]", allow_non_string=False
                ):
                    if (
                        not isinstance(node, yaml.ScalarNode)
                        or (
                            node.tag != "tag:yaml.org,2002:str" and not allow_non_string
                        )
                        or not node.value
                    ):
                        return
                    clean_start = node.start_mark.index
                    clean_end = node.end_mark.index
                    raw = ytext[clean_start:clean_end]
                    anchor = re.match(r"^([ \t]*&[A-Za-z0-9_-]+[ \t]+)", raw)
                    if raw.lstrip().startswith(("*", "${", "!ENV")):
                        return
                    if node.value.lower() in {
                        "required",
                        "the",
                        "string",
                        "str",
                        "optional",
                        "none",
                        "true",
                        "false",
                        "number",
                        "secretstr",
                    }:
                        return
                    if re.fullmatch(r"(?i)(?:number|secretstr);?", node.value):
                        return
                    if re.match(
                        r"(?i)^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*(?:\(|\[)", raw.strip()
                    ):
                        return
                    # Preserve BOM characters that fell inside the replaced
                    # scalar. Their placement within a redacted value has no
                    # meaning, so retain them inside the replacement scalar.
                    if offset_map is None:
                        start, end = clean_start, clean_end
                        retained_boms = ""
                    else:
                        start = offset_map[clean_start]
                        end = offset_map[clean_end - 1] + 1
                        retained_boms = "\ufeff" * (
                            bom_prefix[clean_end - 1] - bom_prefix[clean_start]
                        )
                    marker += retained_boms
                    style = node.style
                    if style == '"':
                        replacement = json.dumps(marker, ensure_ascii=False)
                    elif style == "'":
                        replacement = "'" + marker + "'"
                    elif style in ("|", ">"):
                        header, _, body = raw.partition("\n")
                        indent = re.match(r"[ \t]*", body).group(0)
                        replacement = (
                            header
                            + "\n"
                            + indent
                            + marker
                            + ("\n" if raw.endswith("\n") else "")
                        )
                    else:
                        replacement = json.dumps(marker, ensure_ascii=False)
                    if anchor:
                        replacement = anchor.group(1) + replacement
                    edits.append((start, end, replacement))

                def redact_yaml_leaves(value):
                    if isinstance(value, yaml.ScalarNode):
                        add_scalar(value)
                    elif isinstance(value, yaml.MappingNode):
                        for child_key, child_value in value.value:
                            if isinstance(
                                child_key, yaml.ScalarNode
                            ) and child_key.value.lower() in {
                                "value",
                                "key",
                                "data",
                                "stringdata",
                            }:
                                redact_yaml_leaves(child_value)
                            else:
                                visit(child_value)
                    elif isinstance(value, yaml.SequenceNode):
                        for child in value.value:
                            redact_yaml_leaves(child)

                def visit(node):
                    ident = id(node)
                    if ident in seen:
                        return
                    seen.add(ident)
                    if isinstance(node, yaml.MappingNode):
                        name_value = next(
                            (
                                v
                                for k, v in node.value
                                if isinstance(k, yaml.ScalarNode)
                                and k.value.replace("\ufeff", "").lower() == "name"
                            ),
                            None,
                        )
                        value_value = next(
                            (
                                v
                                for k, v in node.value
                                if isinstance(k, yaml.ScalarNode)
                                and k.value.lower() == "value"
                            ),
                            None,
                        )
                        if (
                            isinstance(name_value, yaml.ScalarNode)
                            and isinstance(value_value, yaml.ScalarNode)
                            and is_secret_name(name_value.value)
                        ):
                            add_scalar(value_value, allow_non_string=True)
                        for key, value in node.value:
                            if isinstance(key, yaml.ScalarNode) and is_secret_name(
                                key.value
                            ):
                                redact_yaml_leaves(value)
                            elif (
                                isinstance(key, yaml.ScalarNode)
                                and key.value.lower() == "authorization"
                                and isinstance(value, yaml.ScalarNode)
                                and value.value
                                and not re.match(
                                    r"(?i)^(?:str|string|header)\b", value.value
                                )
                            ):
                                add_scalar(value, auth_redacted(value.value))
                            visit(key)
                            visit(value)
                    elif isinstance(node, yaml.SequenceNode):
                        if (
                            node.value
                            and isinstance(node.value[0], yaml.ScalarNode)
                            and node.value[0].value.lower() == "sshpass"
                        ):
                            for index, child in enumerate(node.value[:-1]):
                                if (
                                    isinstance(child, yaml.ScalarNode)
                                    and child.value == "-p"
                                ):
                                    add_scalar(node.value[index + 1])
                        for child in node.value:
                            visit(child)

                for root in roots:
                    if root is not None:
                        visit(root)
                if edits:
                    pieces = []
                    cursor = 0
                    for start, end, replacement in sorted(set(edits)):
                        pieces.append(text[cursor:start])
                        pieces.append(replacement)
                        cursor = end
                    pieces.append(text[cursor:])
                    text = "".join(pieces)
            except Exception:
                pass

    # Targeted code and shell forms. Quoted literals may contain escaped quotes;
    # bare values are redacted only when they look like literals, not names or
    # expressions. Separators and closing syntax stay outside the match.
    credential_names = (
        r"(?:password|passwd|passphrase|pwd|secret|token|SECRET_KEY|"
        r"secret_key_base|aws_secret_access_key|aws_access_key_id|"
        r"(?:[A-Za-z0-9]+[._-])*[A-Za-z0-9]*(?:PASSWORD|PASSWD|PASSPHRASE|PWD|SECRET|TOKEN)|"
        r"(?:[A-Za-z0-9]+[._-])*api[-_]?key|(?:[A-Za-z0-9]+[._-])*x[-_]api[-_]key|"
        r"(?:[A-Za-z0-9]+[._-])*[A-Za-z0-9]*_KEY|TOKEN_VALUE|"
        r"[A-Za-z0-9]+(?:Password|Passwd|Passphrase|Pwd|Secret|Token)|"
        r"api[-_]?key|apiKey|x-api-key)"
    )
    expression_call = re.compile(r"\s*[\[(]")
    text = _sub_if(
        "!!binary",
        rf"(?i)(?P<prefix>(?:\b{credential_names})\b[ \t]*:[ \t]*)!!binary[ \t]+[^\s,;}}]+",
        lambda match: match.group("prefix") + json.dumps("[REDACTED_SECRET]"),
        text,
    )
    identifier_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
    json_scalar_re = re.compile(
        r"(?:-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null)"
    )
    # PyYAML's YAML 1.1 bool/null/int/float implicit resolvers (case-sensitive).
    yaml_scalar_re = re.compile(
        r"(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|"
        r"on|On|ON|off|Off|OFF|~|null|Null|NULL|"
        r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|"
        r"\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|"
        r"[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|"
        r"[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)|"
        r"[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|"
        r"[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)"
    )

    def replace_literal(match):
        value = match.group("value")
        keyname = (
            match.group("prefix")
            .split("=", 1)[0]
            .split(":", 1)[0]
            .strip()
            .strip("'\"`")
            .lower()
        )
        if keyname in {"primary_key", "routing-key", "cache_key"}:
            return match.group(0)
        if keyname == "pagetoken" and value.lower() == "nextpagetoken":
            return match.group(0)
        if keyname == "existingsecret":
            return match.group(0)
        if value == "$" and text.startswith("{", match.end()):
            return match.group(0)
        if value.startswith(("*", "&", "${", "!")):
            return match.group(0)
        if re.fullmatch(r"[|>][-+]?(?:[1-9])?", value):
            return match.group(0)
        if ":" in match.group("prefix") and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*;", value
        ):
            return match.group(0)
        if value[0] in "'\"`":
            inner = value[1:-1]
            if inner.startswith("${"):
                return match.group(0)
            if len(value) == 2:
                return match.group(0)
            quote = value[0]
            return match.group("prefix") + quote + "[REDACTED_SECRET]" + quote
        # Expressions and identifiers are source code, not literal credentials.
        if identifier_re.fullmatch(value):
            key = (
                match.group("prefix")
                .split("=", 1)[0]
                .split(":", 1)[0]
                .strip()
                .lower()
                .split(".")[-1]
            )
            if value.lower() == key or value.startswith(("self.", "args.", "os.")):
                return match.group(0)
            if ":" in match.group("prefix") and value.lower() in {
                "the",
                "str",
                "string",
                "optional",
                "none",
                "true",
                "false",
            }:
                return match.group(0)  # common prose and type annotation values
        if value in {
            "|",
            ">",
            "True",
            "False",
            "None",
            "null",
            "true",
            "false",
            "string",
            "int",
            "bool",
            "required",
            "number",
            "SecretStr",
        }:
            return match.group(0)
        if value[0] not in "'\"`":
            # Keep JSON/YAML scalar types intact, but only for ":" mappings.
            # "=" assignments in code, shell, env and compose text are strings.
            if ":" in match.group("prefix") and (
                json_scalar_re.fullmatch(value) or yaml_scalar_re.fullmatch(value)
            ):
                return match.group(0)
        if expression_call.match(text, match.end()):
            return match.group(0)
        marker = (
            json.dumps("[REDACTED_SECRET]")
            if ":" in match.group("prefix")
            else "[REDACTED_SECRET]"
        )
        return match.group("prefix") + marker

    kv = re.compile(
        rf"(?i)(?P<prefix>(?<![\w'\"])(?:\b{credential_names})\b[ \t]*"
        r"(?:(?<![:=])=(?!=)[ \t]*|(?<!:):[ \t]*(?![=])))[ \t]*(?P<value>"
        rf"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|[^\s,;,)}}\[{{('`\"(]+))"
    )

    # Redact typed literals in Python and TypeScript assignments while keeping
    # the annotation intact (the untyped pattern below intentionally skips it).
    typed_secret = re.compile(
        rf"(?i)(?P<prefix>(?:let[ \t]+|const[ \t]+|var[ \t]+)?(?:{credential_names})"
        r"[ \t]*:[ \t]*(?:str|string|SecretStr|number)[ \t]*=[ \t]*)"
        r"(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    )
    lowered_text = text.casefold()
    if any(
        marker in lowered_text
        for marker in (": str =", ": string =", ": secretstr =", ": number =")
    ):
        text = typed_secret.sub(
            lambda m: m.group("prefix")
            + m.group("value")[0]
            + "[REDACTED_SECRET]"
            + m.group("value")[0],
            text,
        )
    go_secret_decl = re.compile(
        rf"(?i)(?P<prefix>\b(?:{credential_names})[ \t]*:=[ \t]*)(?P<quote>['\"])(?P<value>(?:\\.|(?!\2)[^\\])*)\2"
    )
    go_short_declarations = []

    def protect_go_secret_decl(match):
        marker = f"__GO_SHORT_DECL_{len(go_short_declarations)}__"
        go_short_declarations.append(
            match.group("prefix")
            + match.group("quote")
            + "[REDACTED_SECRET]"
            + match.group("quote")
        )
        return marker

    if ":=" in text:
        text = go_secret_decl.sub(protect_go_secret_decl, text)
    if any(
        marker in lowered_text
        for marker in (
            "password",
            "passwd",
            "passphrase",
            "pwd",
            "secret",
            "token",
            "api_key",
            "api-key",
            "apikey",
        )
    ):
        text = kv.sub(replace_literal, text)
        embedded_assignment = re.compile(
            rf"(?i)(?P<prefix>(?P<quote>['\"])(?:{credential_names})[ \t]*=[ \t]*)"
            r"(?P<value>[^\s,;}}\]]+?)(?P<close>['\"])(?=[,}}\]]|$)"
        )
        text = embedded_assignment.sub(
            lambda m: m.group("prefix") + "[REDACTED_SECRET]" + m.group("close"),
            text,
        )

    # Quoted keys occur in partial JSON and inline code dictionaries. Match
    # their literal value independently of whether the surrounding chunk parses.
    quoted_key = re.compile(
        rf"(?i)(?P<prefix>(?P<keyquote>['\"])(?:{credential_names})(?P=keyquote)[ \t]*:[ \t]*)(?P<value>"
        rf"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|(?![{{\[])[^\s,;}}\]]+))"
    )
    if '"' in text or "'" in text:
        text = quoted_key.sub(replace_literal, text)
    for index, declaration in enumerate(go_short_declarations):
        text = text.replace(f"__GO_SHORT_DECL_{index}__", declaration)

    # Preserve block scalar headers while replacing only their indented bodies
    # in mixed snippets that are not a complete YAML document.
    block_pattern = re.compile(
        r"(?im)^(?P<indent>[ \t]*)(?P<key>password|passwd|passphrase|pwd|secret|token|"
        r"[A-Za-z0-9]+[_-](?:password|passwd|passphrase|pwd|secret|token))"
        r"[ \t]*:[ \t]*(?P<indicator>[|>][-+]?(?:[1-9])?)[ \t]*(?:#.*)?\r?\n"
        r"(?P<body>(?:(?:[ \t]*?(?P=indent)[ \t]+[^\r\n]*|[ \t]*)\r?\n)*)"
    )

    def replace_block(match):
        lines = match.group("body").splitlines(keepends=True)
        nonblank = [line for line in lines if line.strip()]
        if not nonblank:
            return match.group(0)
        indent = re.match(r"[ \t]*", nonblank[0]).group(0)
        return (
            match.group("indent")
            + match.group("key")
            + ": "
            + match.group("indicator")
            + "\n"
            + indent
            + "[REDACTED_SECRET]\n"
        )

    if not yaml_structural_ok and re.search(
        r"(?im)^[ \t]*(?:password|passwd|passphrase|pwd|secret|token|"
        r"[A-Za-z0-9_-]+(?:PASSWORD|SECRET|TOKEN))[ \t]*:[ \t]*[|>]",
        text,
    ):
        text = block_pattern.sub(replace_block, text)

    # Cover Kubernetes name/value pairs when YAML is fenced, multi-document,
    # or malformed enough that PyYAML cannot build a node tree.
    if not yaml_structural_ok and "name:" in text and "value:" in text:
        text = re.sub(
            rf"(?im)(?P<prefix>^[ \t\ufeff]*-?[ \t]*name[ \t]*:[ \t]*)"
            rf"(?P<quote>['\"]?)(?P<name>{credential_names})(?P=quote)[ \t]*\r?\n"
            r"(?P<gap>[ \t\ufeff]+value[ \t]*:[ \t]*)(?P<vquote>['\"]?)"
            r"(?P<value>[^\s#,'\"]+)(?P=vquote)",
            lambda m: m.group("prefix")
            + m.group("quote")
            + m.group("name")
            + m.group("quote")
            + "\n"
            + m.group("gap")
            + m.group("vquote")
            + "[REDACTED_SECRET]"
            + m.group("vquote"),
            text,
        )

    # Compose permits quoted environment entries in sequence form.
    text = re.sub(
        rf"(?im)(^[ \t]*-[ \t]*['\"])(?P<name>{credential_names})=(?P<value>[^'\"\r\n]+)(['\"][ \t]*$)",
        lambda m: m.group(1) + m.group("name") + "=[REDACTED_SECRET]" + m.group(4),
        text,
    )

    # Quoted dictionary/subscript keys and long CLI options.
    text = re.sub(
        r"(?i)(\[['\"](?:password|passwd|passphrase|pwd|secret|token|api[-_]?key)['\"]\]\s*=\s*)"
        r"(['\"`])(?:\\.|(?!\2)[^\\])*?\2",
        lambda m: m.group(1) + m.group(2) + "[REDACTED_SECRET]" + m.group(2),
        text,
    )
    text = re.sub(
        r"(?i)(--(?:password|passwd|passphrase|pwd|secret|token|api[-_]?key)\s+)([^\s]+)",
        r"\1[REDACTED_SECRET]",
        text,
    )

    text = _sub_if(
        "pgpassword",
        r"(?i)\bPGPASSWORD[ \t]*=[ \t]*(?P<value>[^\s;]+)",
        lambda match: (
            match.group(0)
            if "${" in match.group("value")
            else "PGPASSWORD=[REDACTED_SECRET]"
        ),
        text,
    )

    # Short -p forms and common client-specific password switches.
    text = re.sub(
        r"(?i)(\bsshpass\s+(?:[^\s]+\s+)*-p\s+|\bdocker\s+login\b[^\n]*?\s-p\s+)"
        r"(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[^\s]+)",
        lambda m: m.group(1)
        + (
            m.group("value")[0] + "[REDACTED_SECRET]" + m.group("value")[0]
            if m.group("value")[0] in "'\""
            else "[REDACTED_SECRET]"
        ),
        text,
    )
    text = _sub_if(
        ("sshpass", "docker"),
        r"(?i)(sshpass\s+-p\s+|docker\s+login\b[^\n]*?\s-p\s+)([^\s]+)",
        r"\1[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:mysql|mysqldump|mariadb|mariadb-dump)\b[^\n]*?"
        r"(?<![\w-])-p(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[^\s]+)",
        lambda m: "-p"
        + (
            m.group("value")[0] + "[REDACTED_SECRET]" + m.group("value")[0]
            if m.group("value")[0] in "'\""
            else "[REDACTED_SECRET]"
        ),
        text,
    )
    text = _sub_if(
        "curl",
        r"(?i)(curl\s+[^\n]*?(?:--user|-u)\s+[^:\s]+:)([^\s'\"`]+)",
        r"\1[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?im)(^\s*(?:machine\s+\S+\s+)?(?:login\s+\S+\s+)?password[ \t]+)"
        r"(?![=])(?!is\b|required\b|must\b|was\b)([^\s#]+)",
        r"\1[REDACTED_SECRET]",
        text,
    )
    text = _sub_if(
        "identified",
        r"(?i)(IDENTIFIED\s+BY\s+)(['\"])(?:\\.|(?!\2)[^\\])*\2",
        lambda m: m.group(1) + m.group(2) + "[REDACTED_SECRET]" + m.group(2),
        text,
    )

    # Authorization header literals retain the scheme and surrounding syntax.
    text = _sub_if(
        "authorization",
        r"""(?i)(authorization\s*:\s*)(?!str\s*=|string\s*=|Header\s*\()"""
        r"""([A-Za-z][A-Za-z0-9_-]*\s+)([^\s\r\n,"'`;}]+)""",
        r"\1\2[REDACTED_TOKEN]",
        text,
    )
    text = _sub_if(
        "authorization",
        r"""(?i)(authorization\s*:\s*)(?!\[REDACTED_TOKEN\])"""
        r"""(?![ \t]*(?:str|string|Header)[ \t]*=)(?![A-Za-z][A-Za-z0-9_-]*\s+)"""
        r"""([^\s\r\n,"'`;}]+)""",
        r"\1[REDACTED_TOKEN]",
        text,
    )
    text = _sub_if(
        "authorization",
        r"""(?i)(authorization['"`]?\s*(?:\]\s*)?[:=][ \t]*)(['"`])(?P<auth>(?:\\.|(?!\2)[^\\])*?)\2""",
        lambda m: m.group(1) + m.group(2) + auth_redacted(m.group("auth")) + m.group(2),
        text,
    )
    text = _sub_if(
        "authorization",
        r"(?i)(authorization\s*=\s*)([A-Za-z][A-Za-z0-9_-]*\s+)?([^\s,;]+)",
        lambda m: (
            m.group(0)
            if "(" in m.group(3) or m.group(3) in {"get_auth_header", "Header(None)"}
            else m.group(1) + (m.group(2) or "") + "[REDACTED_TOKEN]"
        ),
        text,
    )
    text = _sub_if(
        "curl",
        r"(?i)(curl\s+[^\n]*?-H\s+['\"]Authorization:\s*[A-Za-z]+\s+)[^'\"]+",
        r"\1[REDACTED_TOKEN]",
        text,
    )
    for index, source_line in enumerate(go_tag_lines):
        text = text.replace(f"__CODE_INDEXER_GO_TAG_{index}__", source_line)
    return text


def _find_unclosed_fence(lines: List[str]) -> Optional[Tuple[str, str]]:
    """Return (indent, fence_marker) if lines end inside an unclosed markdown fence."""
    open_fence: Optional[Tuple[str, str]] = None
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(\s*)(`{3,}|~{3,})", line)
        if m:
            indent = m.group(1)
            marker = m.group(2)
            is_closing_fence = stripped == marker
            if open_fence is None:
                open_fence = (indent, marker)
            elif is_closing_fence and marker.startswith(open_fence[1]):
                open_fence = None
    return open_fence


def enforce_context_budget(
    context: str,
    max_bytes: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Trim context to satisfy byte and token budgets while preserving newlines and closing fences."""
    if not context or not context.strip():
        return ""

    if max_bytes is None:
        env_bytes = os.environ.get("RAG_MAX_BYTES")
        max_bytes = (
            int(env_bytes)
            if env_bytes and env_bytes.isdigit()
            else DEFAULT_MAX_INJECT_BYTES
        )
    if max_tokens is None:
        env_tokens = os.environ.get("RAG_MAX_TOKENS")
        max_tokens = (
            int(env_tokens)
            if env_tokens and env_tokens.isdigit()
            else DEFAULT_MAX_INJECT_TOKENS
        )

    if max_bytes <= 0 or max_tokens <= 0:
        return ""

    # Fast path if within limits and fences are closed
    encoded = context.encode("utf-8")
    words = context.split()
    lines = context.splitlines(keepends=True)
    if (
        len(encoded) <= max_bytes
        and len(words) <= max_tokens
        and _find_unclosed_fence(lines) is None
    ):
        return context

    kept_lines: List[str] = []
    current_bytes = 0
    current_words = 0
    truncated = False

    trunc_comment = "\n\n<!-- Context truncated to stay within budget -->"

    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        line_words = len(line.split())
        if (current_bytes + line_bytes > max_bytes) or (
            current_words + line_words > max_tokens
        ):
            truncated = True
            if not kept_lines:
                # Truncate oversized single line to fit
                max_slice = max(0, max_bytes - len(trunc_comment.encode("utf-8")) - 10)
                raw = line.encode("utf-8")[:max_slice].decode("utf-8", errors="ignore")
                if raw:
                    kept_lines.append(raw)
            break
        kept_lines.append(line)
        current_bytes += line_bytes
        current_words += line_words

    if not truncated and len(encoded) <= max_bytes and len(words) <= max_tokens:
        open_fence = _find_unclosed_fence(kept_lines)
        if open_fence:
            close_str = f"\n{open_fence[0]}{open_fence[1]}\n"
            closed = ("".join(kept_lines).rstrip() + close_str).strip()
            if (
                len(closed.encode("utf-8")) <= max_bytes
                and len(closed.split()) <= max_tokens
            ):
                return closed
            # Closing the fence can exceed a tight budget. Fall through to the
            # trimming path, which budgets both the fence and truncation note.
            truncated = True
        else:
            return context

    # Truncated: ensure any open code fence is closed and budget is respected
    while kept_lines:
        open_fence = _find_unclosed_fence(kept_lines)
        close_fence = f"\n{open_fence[0]}{open_fence[1]}" if open_fence else ""
        suffix = f"{close_fence}{trunc_comment}"
        suffix_bytes = len(suffix.encode("utf-8"))
        suffix_words = len(suffix.split())

        current_bytes = len("".join(kept_lines).encode("utf-8"))
        current_words = len("".join(kept_lines).split())

        if (current_bytes + suffix_bytes <= max_bytes) and (
            current_words + suffix_words <= max_tokens
        ):
            trimmed = "".join(kept_lines).rstrip() + suffix
            return trimmed
        kept_lines.pop()

    note = trunc_comment.strip()
    if len(note.encode("utf-8")) > max_bytes or len(note.split()) > max_tokens:
        return ""
    return note


def _context_already_present(block_text: str, context: str) -> bool:
    """True when context's non-empty lines already appear, in order, in the block."""
    wanted = [line.strip() for line in context.splitlines() if line.strip()]
    if not wanted:
        return True
    have = [line.strip() for line in block_text.splitlines()]
    span = len(wanted)
    for index in range(len(have) - span + 1):
        if have[index : index + span] == wanted:
            return True
    return False


def inject_context_into_system_template(
    config_text: str,
    context: str,
    max_bytes: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Append context inside every ``system_template: |`` literal block.

    Modern SWE-agent reads ``agent.templates.system_template``. This project
    also keeps a flat ``agent.system_template``. Updating every literal block
    feeds the retrieved snippets to whichever key the agent actually loads.
    The rest of the document, including comments, is preserved.
    """
    cleaned = redact_secrets(context or "")
    snippet = enforce_context_budget(
        cleaned, max_bytes=max_bytes, max_tokens=max_tokens
    ).strip("\n")
    if not snippet.strip():
        return config_text

    if not config_text.endswith("\n"):
        config_text += "\n"

    lines = config_text.splitlines(keepends=True)
    inserts: List[Tuple[int, int]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.rstrip("\n").rstrip("\r")
        prefix_len = len(stripped) - len(stripped.lstrip(" "))
        body = stripped.lstrip(" ")
        if not body.startswith("system_template:") or "|" not in body:
            index += 1
            continue
        # Accept `|`, `|-`, and `|+` and ignore a trailing comment.
        after_pipe = body.split("|", 1)[1].lstrip("+-").strip()
        if after_pipe and not after_pipe.startswith("#"):
            index += 1
            continue

        key_indent = prefix_len
        content_indent = None
        cursor = index + 1
        end = index + 1
        while cursor < len(lines):
            raw = lines[cursor]
            if raw.strip() == "":
                end = cursor + 1
                cursor += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if content_indent is None:
                if indent > key_indent:
                    content_indent = indent
                    end = cursor + 1
                    cursor += 1
                    continue
                break
            if indent >= content_indent:
                end = cursor + 1
                cursor += 1
                continue
            break

        if content_indent is None:
            content_indent = key_indent + 2

        block_text = "".join(lines[index + 1 : end])
        if not _context_already_present(block_text, snippet):
            inserts.append((end, content_indent))
        index = end if end > index else index + 1

    if not inserts and not any(
        "system_template:" in item and "|" in item for item in lines
    ):
        raise ValueError("system_template literal block not found")

    for end, content_indent in reversed(inserts):
        prefix = " " * content_indent
        block = [prefix + "\n"]
        for context_line in snippet.splitlines():
            block.append(prefix + context_line + "\n")
        block.append("\n")
        lines[end:end] = block

    return "".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Write retrieved markdown into a SWE-agent config file."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        sys.stderr.write("Usage: inject_context.py CONFIG_PATH CONTEXT_PATH\n")
        return 2

    config_path = Path(args[0])
    context_path = Path(args[1])
    context = context_path.read_text(encoding="utf-8")
    original = config_path.read_text(encoding="utf-8")
    updated = inject_context_into_system_template(original, context)
    if updated != original:
        config_path.write_text(updated, encoding="utf-8")
        print("Injected RAG context into agent system_template.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
