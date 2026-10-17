#!/usr/bin/env python3
"""Insert retrieved code into SWE-agent system_template blocks.

Stdlib only so the swe-agent container can run this without the indexer
dependencies. The same file is mounted from the swe-agent ConfigMap at
/config/inject_context.py.
"""

import os
import re
import sys
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
        r"(?i)\b(authorization\s*:\s*[a-z][a-z0-9_-]*\s+)[^\s\r\n]+",
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

    # 7. Key-value secrets
    # Catches:
    # - bare token, secret, password, passwd, apikey, api_key, api_token
    # - aws_secret_access_key, aws_access_key_id
    # - prefixed KEY-style names (*_TOKEN=, *_PASSWORD=, *_SECRET, *_KEY, etc.)
    # - JSON keys with quotes ("password":, 'api_token':)
    key_names = (
        r"(?:password|passwd|passphrase|pwd|pass|key|token|secret|api[-_]?key|"
        r"[a-z0-9]+(?:[_-](?:token|secret|key|password|passwd|passphrase|pwd))|"
        r"[a-z0-9]+(?:Token|Secret|Key)|token_[a-z0-9]+|"
        r"aws_secret_access_key|aws_access_key_id|x-api-key|x_api_key)"
    )
    credential_value = re.compile(
        r"(?i)^(?:[a-z0-9]{24,}|(?=.{5,}$)(?=.*\d).+|(?=.{6,}$)(?=.*[^a-z0-9_]).+)$"
    )

    def _replace_kv(m: re.Match) -> str:
        prefix = m.group(1)
        key = m.group(2)
        sep = m.group(3)
        quote = m.group(4)
        val = m.group(5) if quote else m.group(6)
        if val and val.startswith("[REDACTED_"):
            return m.group(0)
        if quote:
            return f"{prefix}{key}{sep}{quote}[REDACTED_SECRET]{quote}"
        if val in {"True", "False", "None"} or not credential_value.match(val or ""):
            return m.group(0)
        return f"{prefix}{key}{sep}[REDACTED_SECRET]"

    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        match = re.match(
            rf"^([ \t]*)(['\"]?(?:{key_names})['\"]?)\s*:\s*[|>][-+]?\s*(?:#.*)?\r?\n$",
            lines[index],
            re.IGNORECASE,
        )
        if not match:
            index += 1
            continue
        base_indent = len(match.group(1).expandtabs(8))
        end = index + 1
        content_indent = None
        while end < len(lines):
            line = lines[end]
            indent = len(line) - len(line.lstrip(" \t"))
            if line.strip() and len(line[:indent].expandtabs(8)) <= base_indent:
                break
            if line.strip() and content_indent is None:
                content_indent = indent
            end += 1
        if content_indent is None:
            index += 1
            continue
        newline = "\n" if lines[index + 1].endswith("\n") else ""
        lines[index + 1 : end] = [" " * content_indent + "[REDACTED_SECRET]" + newline]
        index += 2
    text = "".join(lines)

    kv_pattern = (
        rf"""(?i)(^|[^a-zA-Z0-9_-])(['"]?(?:{key_names})['"]?)(\s*\]?\s*[:=]\s*)"""
        r"""(?:(['"])(.*?)\4|([A-Za-z0-9_:+/=-]+))"""
    )
    text = re.sub(kv_pattern, _replace_kv, text)

    # Authorization values in mappings and header subscript assignments.
    text = re.sub(
        r"(?i)(authorization['\"]?\s*(?:\]\s*)?[:=]\s*['\"]?)([a-z][a-z0-9_-]*\s+)([^'\"\s,}]+)",
        r"\g<1>\g<2>[REDACTED_TOKEN]",
        text,
    )

    # CLI password options have no equals sign; YAML block scalars have no inline value.
    text = re.sub(
        r"(?i)(--(?:password|passwd|passphrase|pwd|pass|token|secret|api[-_]?key)\s+)([^\s\r\n]+)",
        r"\g<1>[REDACTED_SECRET]",
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
            return ("".join(kept_lines).rstrip() + close_str).strip()
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
