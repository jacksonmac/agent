"""File tools, jailed to the run's Workspace."""

import os

from ..workspace import Workspace


def write_file(ws: Workspace, text: str, name: str) -> str:
    """Write text to a workspace file (creating parent dirs)."""
    try:
        path = ws.resolve(name)
    except ValueError:
        return f"[ERROR] refusing to write outside the workspace: {name}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    print("WROTE:", path)
    return f"WROTE {len(text)} chars to {name}"


def read_file(ws: Workspace, name: str, offset: int = 0, max_chars: int = 6_000) -> str:
    """Read a workspace file. A head window + explicit offset (instead of
    middle-truncation) lets the model page through big files deliberately."""
    try:
        path = ws.resolve(name)
    except ValueError:
        return f"[ERROR] refusing to read outside the workspace: {name}"
    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        return f"[ERROR] could not read {name}: {e}"
    offset = max(0, int(offset))
    max_chars = max(1, int(max_chars))
    chunk = content[offset:offset + max_chars]
    end = offset + len(chunk)
    header = f"[{name}, chars {offset}-{end} of {len(content)}]"
    if end < len(content):
        header += f" (call again with offset={end} for more)"
    return f"{header}\n{chunk}"


def list_files(ws: Workspace, path: str = ".") -> str:
    """Recursive listing of the workspace (or a subdirectory), one file per
    line with its size. Junk (__pycache__, hidden files, .pyc) is skipped."""
    try:
        root = ws.resolve(path)
    except ValueError:
        return f"[ERROR] refusing to list outside the workspace: {path}"
    if not os.path.isdir(root):
        return f"[ERROR] not a directory: {path}"
    entries = [f for f in ws.list_all_files()
               if root == ws.root or ws.resolve(f).startswith(root + os.sep)]
    if not entries:
        return "(the workspace is empty)"
    lines = []
    for rel in entries[:200]:
        try:
            size = os.path.getsize(ws.resolve(rel))
        except (OSError, ValueError):
            continue
        lines.append(f"{rel}  ({size} bytes)")
    out = "\n".join(lines)
    if len(entries) > 200:
        out += f"\n[... and {len(entries) - 200} more files]"
    return out[:4_000]


def grep_files(ws: Workspace, pattern: str, path: str = ".",
               max_results: int = 50) -> str:
    """Search workspace files for a regex (or, if the regex is invalid, the
    literal text) and return file:line matches — the targeted alternative to
    reading whole files."""
    import re
    try:
        root = ws.resolve(path)
    except ValueError:
        return f"[ERROR] refusing to search outside the workspace: {path}"
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))  # plain-text fallback
    max_results = max(1, int(max_results))
    entries = [f for f in ws.list_all_files()
               if root == ws.root or ws.resolve(f).startswith(root + os.sep)]
    lines = []
    for rel in entries:
        try:
            with open(ws.resolve(rel)) as f:
                content = f.read()
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if rx.search(line):
                lines.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                if len(lines) >= max_results:
                    out = "\n".join(lines)
                    return (out + f"\n[... stopped at {max_results} matches]")[:4_000]
    if not lines:
        return f"(no matches for {pattern!r})"
    return "\n".join(lines)[:4_000]


def edit_file(ws: Workspace, name: str, old_text: str, new_text: str) -> str:
    """Replace an exact snippet in an existing file — the targeted alternative
    to rewriting the whole file with write_file."""
    import difflib
    try:
        path = ws.resolve(name)
    except ValueError:
        return f"[ERROR] refusing to edit outside the workspace: {name}"
    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        return f"[ERROR] could not read {name}: {e}"
    count = content.count(old_text)
    if count == 0:
        close = difflib.get_close_matches(old_text.strip(), content.splitlines(), n=1)
        hint = f" Closest line in the file: {close[0]!r}" if close else ""
        return f"[ERROR] old_text not found in {name}.{hint}"
    if count > 1:
        return (f"[ERROR] old_text found {count} times in {name} — include more "
                f"surrounding context so it matches exactly once.")
    content = content.replace(old_text, new_text, 1)
    with open(path, "w") as f:
        f.write(content)
    print("EDITED:", path)
    return f"REPLACED 1 occurrence in {name} (file now {len(content)} chars)"
