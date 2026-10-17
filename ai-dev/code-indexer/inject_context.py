#!/usr/bin/env python3
"""Insert retrieved code into SWE-agent system_template blocks.

Stdlib only so the swe-agent container can run this without the indexer
dependencies. The same file is mounted from the swe-agent ConfigMap at
/config/inject_context.py.
"""

import os
import re
import sys
import json
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_MAX_INJECT_BYTES: int = 16384  # 16 KiB default byte budget
DEFAULT_MAX_INJECT_TOKENS: int = 2048  # Default token/word estimate budget


def redact_secrets(text: str) -> str:
    """Redact secret keys, tokens, credentials, and private keys from text."""
    if not text:
        return ""

    # 1. Private key blocks (full block: PKCS#1, PKCS#8, etc.)
    text = re.sub(
        r"-----BEGIN\s+(?:[A-Z0-9_]+\s+)*PRIVATE KEY(?:\s+BLOCK)?-----"
        r"[\s\S]*?-----END\s+(?:[A-Z0-9_]+\s+)*PRIVATE KEY(?:\s+BLOCK)?-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.IGNORECASE,
    )
    # Private key blocks split across chunks: BEGIN onward if no END in chunk
    text = re.sub(
        r"-----BEGIN\s+(?:[A-Z0-9_]+\s+)*PRIVATE KEY(?:\s+BLOCK)?-----[\s\S]*$",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.IGNORECASE,
    )
    # Private key blocks split across chunks: END tail without BEGIN in chunk
    text = re.sub(
        r"^[\s\S]*?-----END\s+(?:[A-Z0-9_]+\s+)*PRIVATE KEY(?:\s+BLOCK)?-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Certificate blocks (full, or split)
    text = re.sub(
        r"-----BEGIN\s+(?:[A-Z0-9_\s]*CERTIFICATE)-----[\s\S]*?-----END\s+(?:[A-Z0-9_\s]*CERTIFICATE)-----",
        "[REDACTED_CERTIFICATE]",
        text,
    )
    text = re.sub(
        r"-----BEGIN\s+(?:[A-Z0-9_\s]*CERTIFICATE)-----[\s\S]*$",
        "[REDACTED_CERTIFICATE]",
        text,
    )
    text = re.sub(
        r"^[\s\S]*?-----END\s+(?:[A-Z0-9_\s]*CERTIFICATE)-----",
        "[REDACTED_CERTIFICATE]",
        text,
    )

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
    text = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^:/\s@]*:)[^@/\s]+(@)",
        r"\g<1>\g<2>[REDACTED_SECRET]\g<3>",
        text,
    )
    text = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^:/\s@]{16,})(@)",
        r"\g<1>[REDACTED_SECRET]\g<3>",
        text,
    )
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*[a-z][a-z0-9_-]*\s+)[^\s\r\n,\"'`;}]+",
        r"\g<1>[REDACTED_TOKEN]",
        text,
    )
    # Standalone Bearer tokens
    text = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]{8,}\b",
        r"\g<1>[REDACTED_TOKEN]",
        text,
    )

    # OpenAI-style secret keys can occur outside key/value assignments.
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", text)

    # Structured documents are edited through YAML node source marks. This
    # preserves comments, quoting, flow style, block scalars, and whitespace.
    # JSON is YAML too, so the same node walk handles both while safe_load
    # confirms that aliases and scalar types have normal safe semantics.
    candidate = text.lstrip()
    looks_structured = candidate.startswith(("{", "[", "---")) or re.search(
        r"(?m)^\s*(?:-\s+)?(?:['\"][^'\"\n]+['\"]|[A-Za-z_][\w.-]*)\s*:",
        text,
    )
    try:
        if not looks_structured:
            raise ValueError("not a structured document")
        import yaml

        root = yaml.compose(text, Loader=yaml.SafeLoader)
        yaml.safe_load(text)
    except Exception:
        root = None
    if root is not None:
        secret_name = re.compile(
            r"(?i)(?:password|passwd|passphrase|pwd|secret|token|"
            r"api[-_]?key|[a-z0-9]+[_-](?:password|passwd|passphrase|pwd|secret|token)|"
            r"[a-z0-9]+_token|"
            r"[a-z0-9]+(?:Secret|Token)|secret_key_base|secret_key|aws_secret_access_key|"
            r"x-api-key|x_api_key)"
        )
        edits = []

        def add_scalar(value_node, marker="[REDACTED_SECRET]"):
            if (
                not isinstance(value_node, yaml.ScalarNode)
                or value_node.tag != "tag:yaml.org,2002:str"
                or not value_node.value
            ):
                return
            start, end = value_node.start_mark.index, value_node.end_mark.index
            raw = text[start:end]
            if raw.lstrip().startswith(("*", "&", "${")):
                return
            if re.match(
                r"(?i)^(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*[\[(]|"
                r"Optional\[|Union\[|list\[|dict\[)",
                raw.strip(),
            ):
                return
            style = value_node.style
            if style == '"':
                replacement = json.dumps(marker)
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
                replacement = marker
            edits.append((start, end, replacement))

        def visit(node):
            if isinstance(node, yaml.MappingNode):
                names = {
                    key.value.lower(): value
                    for key, value in node.value
                    if isinstance(key, yaml.ScalarNode)
                }
                name_node = names.get("name")
                value_node = names.get("value")
                if (
                    name_node is not None
                    and value_node is not None
                    and isinstance(name_node, yaml.ScalarNode)
                    and secret_name.fullmatch(name_node.value)
                ):
                    add_scalar(value_node)
                for key_node, value_node in node.value:
                    if (
                        isinstance(key_node, yaml.ScalarNode)
                        and key_node.tag == "tag:yaml.org,2002:str"
                        and secret_name.fullmatch(key_node.value)
                        and isinstance(value_node, yaml.ScalarNode)
                        and value_node.tag == "tag:yaml.org,2002:str"
                        and value_node.value
                    ):
                        add_scalar(value_node)
                    elif (
                        isinstance(key_node, yaml.ScalarNode)
                        and key_node.tag == "tag:yaml.org,2002:str"
                        and key_node.value.lower() == "authorization"
                        and isinstance(value_node, yaml.ScalarNode)
                        and value_node.tag == "tag:yaml.org,2002:str"
                        and value_node.value
                    ):
                        auth_value = value_node.value
                        scheme = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s+", auth_value)
                        marker = (
                            scheme.group(1) + " " if scheme else ""
                        ) + "[REDACTED_TOKEN]"
                        add_scalar(value_node, marker)
                    visit(key_node)
                    visit(value_node)
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    visit(child)

        visit(root)
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        if isinstance(root, (yaml.MappingNode, yaml.SequenceNode)):
            return text

    # A source file can mix shell/code with a complete JSON object on one line.
    # Parse those lines independently so valid JSON islands get the same
    # structural treatment without applying JSON rules to surrounding code.
    json_key = re.compile(
        r"(?i)(?:password|passwd|passphrase|pwd|secret|token|api[-_]?key|"
        r"[a-z0-9]+[_-](?:password|passwd|passphrase|pwd|secret|token)|"
        r"[a-z0-9]+(?:Secret|Token)|[a-z0-9]+_(?:KEY|TOKEN)|TOKEN_VALUE|"
        r"secret_key_base|secret_key|aws_secret_access_key|"
        r"x-api-key|x_api_key)"
    )

    def scrub_json(value):
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED_SECRET]"
                    if isinstance(key, str)
                    and json_key.fullmatch(key)
                    and isinstance(child, str)
                    and child
                    else (
                        (
                            re.sub(
                                r"^([A-Za-z][A-Za-z0-9_-]*)\s+.*$",
                                r"\1 [REDACTED_TOKEN]",
                                child,
                            )
                            if re.match(r"^[A-Za-z][A-Za-z0-9_-]*\s+", child)
                            else "[REDACTED_TOKEN]"
                        )
                        if isinstance(key, str)
                        and key.lower() == "authorization"
                        and isinstance(child, str)
                        and child
                        else scrub_json(child)
                    )
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [scrub_json(child) for child in value]
        return value

    changed_lines = []
    for line in text.splitlines(keepends=True):
        candidate = line.strip()
        if not candidate.startswith(("{", "[")):
            changed_lines.append(line)
            continue
        try:
            parsed_line = json.loads(candidate)
        except (ValueError, TypeError):
            changed_lines.append(line)
            continue
        scrubbed = scrub_json(parsed_line)
        if scrubbed != parsed_line:
            leading = line[: len(line) - len(line.lstrip())]
            ending = "\n" if line.endswith("\n") else ""
            changed_lines.append(
                leading + json.dumps(scrubbed, separators=(",", ":")) + ending
            )
        else:
            changed_lines.append(line)
    text = "".join(changed_lines)

    # Targeted code and shell forms. Quoted literals may contain escaped quotes;
    # bare values are redacted only when they look like literals, not names or
    # expressions. Separators and closing syntax stay outside the match.
    credential_names = (
        r"(?:password|passwd|passphrase|pwd|secret|token|SECRET_KEY|"
        r"secret_key_base|aws_secret_access_key|aws_access_key_id|"
        r"[A-Za-z0-9]+[_-](?:PASSWORD|SECRET|TOKEN|KEY)|"
        r"[A-Za-z0-9]+_(?:TOKEN|KEY)|TOKEN_VALUE|[A-Za-z0-9]+Secret|[A-Za-z0-9]+Token|"
        r"api[-_]?key|x-api-key)"
    )
    quoted_value = r"""(?P<q>['"`])(?:\\.|(?!\1)[^\\])*?\1"""

    def replace_literal(match):
        value = match.group("value")
        if value.startswith(("*", "&", "${")):
            return match.group(0)
        if value[0] in "'\"`":
            if len(value) == 2:
                return match.group(0)
            quote = value[0]
            return match.group("prefix") + quote + "[REDACTED_SECRET]" + quote
        # Expressions and identifiers are source code, not literal credentials.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value):
            key = (
                match.group("prefix").split("=", 1)[0].split(":", 1)[0].strip().lower()
            )
            if value.lower() == key or "." in value:
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
        if value in {"|", ">", "True", "False", "None", "null", "true", "false"}:
            return match.group(0)
        return match.group("prefix") + "[REDACTED_SECRET]"

    kv = re.compile(
        rf"(?i)(?P<prefix>(?:\b{credential_names})\b[ \t]*(?:=(?!=)|:[ \t]*))[ \t]*(?P<value>"
        rf"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|[^\s,;,)}}\[{{(]+))"
    )
    text = kv.sub(replace_literal, text)

    # Preserve block scalar headers while replacing only their indented bodies
    # in mixed snippets that are not a complete YAML document.
    block_pattern = re.compile(
        r"(?im)^(?P<indent>[ \t]*)(?P<key>password|passwd|passphrase|pwd|secret|token|"
        r"[A-Za-z0-9]+[_-](?:password|passwd|passphrase|pwd|secret|token))"
        r"[ \t]*:[ \t]*(?P<indicator>[|>][-+]?(?:[1-9])?)[ \t]*(?:#.*)?\r?\n"
        r"(?P<body>(?:(?:[ \t]+[^\r\n]*|[ \t]*)\r?\n)*)"
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

    text = block_pattern.sub(replace_block, text)

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

    text = re.sub(
        r"(?i)\bPGPASSWORD[ \t]*=[ \t]*([^\s;]+)",
        r"PGPASSWORD=[REDACTED_SECRET]",
        text,
    )

    # Short -p forms and common client-specific password switches.
    text = re.sub(
        r"(?i)(?<![\w-])-p(?P<value>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[^\s]+)",
        lambda m: "-p"
        + (
            m.group("value")[0] + "[REDACTED_SECRET]" + m.group("value")[0]
            if m.group("value")[0] in "'\""
            else "[REDACTED_SECRET]"
        ),
        text,
    )
    text = re.sub(
        r"(?i)(sshpass\s+-p\s+|docker\s+login\b[^\n]*?\s-p\s+)([^\s]+)",
        r"\1[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?i)(curl\s+[^\n]*?-u\s+[^:\s]+:)([^\s'\"`]+)", r"\1[REDACTED_SECRET]", text
    )
    text = re.sub(
        r"(?im)(^\s*(?:machine\s+\S+\s+)?(?:login\s+\S+\s+)?password[ \t]+)(?![=])([^\s#]+)",
        r"\1[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?i)(IDENTIFIED\s+BY\s+)(['\"])(?:\\.|(?!\2)[^\\])*\2",
        lambda m: m.group(1) + m.group(2) + "[REDACTED_SECRET]" + m.group(2),
        text,
    )

    # Authorization header literals retain the scheme and surrounding syntax.
    text = re.sub(
        r"""(?i)(authorization\s*:\s*)([A-Za-z][A-Za-z0-9_-]*\s+)([^\s\r\n,"'`;}]+)""",
        r"\1\2[REDACTED_TOKEN]",
        text,
    )
    text = re.sub(
        r"""(?i)(authorization\s*:\s*)(?!\[REDACTED_TOKEN\])(?![A-Za-z][A-Za-z0-9_-]*\s+)([^\s\r\n,"'`;}]+)""",
        r"\1[REDACTED_TOKEN]",
        text,
    )
    text = re.sub(
        r"""(?i)(authorization['"`]?\s*(?:\]\s*)?[:=][ \t]*)(['"`])(?:\\.|(?!\2)[^\\])*?\2""",
        lambda m: m.group(1) + m.group(2) + "[REDACTED_TOKEN]" + m.group(2),
        text,
    )
    text = re.sub(
        r"(?i)(authorization\s*=\s*)([A-Za-z][A-Za-z0-9_-]*\s+)?([^\s,;]+)",
        lambda m: m.group(1) + (m.group(2) or "") + "[REDACTED_TOKEN]",
        text,
    )
    text = re.sub(
        r"(?i)(curl\s+[^\n]*?-H\s+['\"]Authorization:\s*[A-Za-z]+\s+)[^'\"]+",
        r"\1[REDACTED_TOKEN]",
        text,
    )
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
