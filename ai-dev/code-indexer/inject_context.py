#!/usr/bin/env python3
"""Insert retrieved code into SWE-agent system_template blocks.

Stdlib only so the swe-agent container can run this without the indexer
dependencies. The same file is mounted from the swe-agent ConfigMap at
/config/inject_context.py.
"""

import sys
from pathlib import Path
from typing import List, Optional, Tuple


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


def inject_context_into_system_template(config_text: str, context: str) -> str:
    """Append context inside every ``system_template: |`` literal block.

    Modern SWE-agent reads ``agent.templates.system_template``. This project
    also keeps a flat ``agent.system_template``. Updating every literal block
    feeds the retrieved snippets to whichever key the agent actually loads.
    The rest of the document, including comments, is preserved.
    """
    snippet = (context or "").strip("\n")
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
