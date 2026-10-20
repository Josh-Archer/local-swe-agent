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
        r"-----BEGIN\s+(?:[A-Z0-9_\s]*PRIVATE KEY)-----[\s\S]*?-----END\s+(?:[A-Z0-9_\s]*PRIVATE KEY)-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
    )
    # Private key blocks split across chunks: BEGIN onward if no END in chunk
    text = re.sub(
        r"-----BEGIN\s+(?:[A-Z0-9_\s]*PRIVATE KEY)-----[\s\S]*$",
        "[REDACTED_PRIVATE_KEY]",
        text,
    )
    # Private key blocks split across chunks: END tail without BEGIN in chunk
    text = re.sub(
        r"^[\s\S]*?-----END\s+(?:[A-Z0-9_\s]*PRIVATE KEY)-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
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

    # 6. Authorization headers (token or Bearer)
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer|token)\s+)[^\s\r\n]+",
        r"\g<1>[REDACTED_TOKEN]",
        text,
    )
    # Standalone Bearer tokens
    text = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]{8,}\b",
        r"\g<1>[REDACTED_TOKEN]",
        text,
    )

    # 7. Key-value secrets
    # Catches:
    # - bare token, secret, password, passwd, apikey, api_key, api_token
    # - aws_secret_access_key, aws_access_key_id
    # - prefixed KEY-style names (GITHUB_TOKEN=, DB_PASSWORD=, *_SECRET, *_KEY, etc.)
    # - JSON keys with quotes ("password":, 'api_token':)
    key_names = (
        r"token|secret|password|passwd|apikey|api_key|api_token|"
        r"secret_key|client_secret|access_token|auth_token|private_key|"
        r"aws_secret_access_key|aws_access_key_id|"
        r"[a-zA-Z0-9_]+_(?:token|secret|password|passwd|key|apikey|keys)"
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
        return f"{prefix}{key}{sep}[REDACTED_SECRET]"

    kv_pattern = (
        rf"""(?i)(^|[^a-zA-Z0-9_])(['"]?(?:{key_names})['"]?)(\s*[:=]\s*)"""
        r"""(?:(['"])(.*?)\4|([^\s"',;\r\n]+))"""
    )
    text = re.sub(kv_pattern, _replace_kv, text)

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

    encoded = context.encode("utf-8")
    lines = context.splitlines(keepends=True)
    if (
        len(encoded) <= max_bytes
        and len(context.split()) <= max_tokens
        and _find_unclosed_fence(lines) is None
    ):
        return context

    trunc_comment = "\n\n<!-- Context truncated to stay within budget -->"

    def fit_prefix(candidate: str) -> str:
        """Fit a prefix plus truncation marker and any required closing fence."""
        # Respect word budget without flattening whitespace or line endings.
        word_count = 0
        token_end = len(candidate)
        for match in re.finditer(r"\S+", candidate):
            word_count += 1
            if word_count > max_tokens:
                token_end = match.start()
                break
        candidate = candidate[:token_end]

        while candidate:
            open_fence = _find_unclosed_fence(candidate.splitlines(keepends=True))
            close_fence = f"\n{open_fence[0]}{open_fence[1]}" if open_fence else ""
            suffix = close_fence + trunc_comment
            result = candidate.rstrip() + suffix
            if (
                len(result.encode("utf-8")) <= max_bytes
                and len(result.split()) <= max_tokens
            ):
                return result

            # Remove a complete line first to preserve source formatting. If
            # the only line is oversized, trim at a UTF-8 character boundary.
            if "\n" in candidate.rstrip("\n"):
                candidate = candidate.rsplit("\n", 1)[0] + "\n"
            else:
                candidate = candidate[:-1]

        # Very small budgets cannot fit the full marker. Return a bounded
        # prefix of it rather than exceeding the caller's byte limit.
        marker = "<!-- Context truncated -->"
        return marker.encode("utf-8")[: max(0, max_bytes)].decode(
            "utf-8", errors="ignore"
        )

    # Keep a newline-aware prefix first. Oversized individual lines are then
    # shortened by bytes, preserving any newline characters in the prefix.
    reserve = len(trunc_comment.encode("utf-8")) + 8
    max_prefix_bytes = max(0, max_bytes - reserve)
    prefix = encoded[:max_prefix_bytes].decode("utf-8", errors="ignore")
    return fit_prefix(prefix)


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
