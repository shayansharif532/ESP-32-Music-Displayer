"""Turns THIRD_PARTY_NOTICES.md into the plain-text page the installer shows (and installs next to the program).

    python installer\\make_notices.py [output.txt]        default: installer\\build\\THIRD_PARTY_NOTICES.txt

Setup's information page is a plain-text box, so the Markdown is flattened: headings become underlined titles, table rows become
short bullet lists, emphasis marks and link syntax are removed, paragraphs are re-wrapped. The wording is not changed, so the Markdown
file stays the source of truth. Only the parts that apply to the Windows program are kept: the introduction (trademarks) and the
section about the companion (`## 3.`); the firmware and development-tool sections belong to other deliverables of the project.
The output is UTF-8 with a byte-order mark (what Inno Setup needs to show non-ASCII characters) and Windows line endings.
"""
from __future__ import annotations

import os
import re
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SOURCE = os.path.join(ROOT, "THIRD_PARTY_NOTICES.md")
DEFAULT_OUT = os.path.join(ROOT, "installer", "build", "THIRD_PARTY_NOTICES.txt")
WIDTH = 78
KEEP_SECTIONS = ("3.",)          # numbered `##` sections that apply to the Windows companion

HEADER = ("DeskMedia Companion - third-party notices\n"
          "=========================================\n\n"
          "This program contains open-source software. The licence texts that must accompany it are in the LICENSES\n"
          "folder next to the program. Nothing here changes the licence of any component. The complete notices of the\n"
          "whole project are in THIRD_PARTY_NOTICES.md in its repository.\n")


def _inline(text: str) -> str:
    def link(m: re.Match) -> str:
        target = m.group(2)
        return m.group(1) if target.startswith(("docs/", "LICENSES/", "../", "#")) else f"{m.group(1)} ({target})"
    text = re.sub(r"\[([^\]]+)\]\(<?([^)>\s]+)>?\)", link, text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.replace("**", "").replace("`", "").replace("\\|", "|").strip()


def _wrap(text: str, first: str = "", rest: str = "") -> list[str]:
    return textwrap.wrap(text, WIDTH, initial_indent=first, subsequent_indent=rest, break_long_words=False,
                         break_on_hyphens=False) or [first.rstrip()]


def convert(md: str) -> str:
    out: list[str] = HEADER.splitlines() + [""]
    para: list[str] = []                 # lines of the paragraph being collected
    item: list[str] = []                 # lines of the bullet being collected
    header_cells: list[str] = []
    in_table = False
    include = False                      # true inside the trademark paragraph and the kept `##` sections

    def flush() -> None:
        nonlocal para, item
        if para:
            out.extend(_wrap(_inline(" ".join(para))))
            out.append("")
        if item:
            out.extend(_wrap(_inline(" ".join(item)), "  *  ", "     "))
        para, item = [], []

    for raw in md.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            in_table = False
            level, title = len(heading.group(1)), _inline(heading.group(2))
            if level == 2:
                include = title.startswith(KEEP_SECTIONS)
            if level == 1 or not include:
                continue                                           # the document title is replaced by HEADER
            out += ["", title, ("-" if level == 2 else "~") * min(len(title), WIDTH), ""]
            continue
        if line.startswith("|"):
            if not include:
                continue
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):            # the |---|---| separator row
                continue
            if not in_table:
                in_table, header_cells = True, [_inline(c) for c in cells]
                continue
            cells = [_inline(c) for c in cells]
            out += _wrap(cells[0], " *  ", "    ")
            for label, value in zip(header_cells[1:], cells[1:]):
                if value:
                    out += _wrap(f"{label}: {value}", "      - ", "        ")
            continue
        in_table = False
        if line.startswith("Project name and trademarks"):
            include = True                                          # the one paragraph of the preamble that applies here
        if not include:
            continue
        if not line.strip():
            flush()
            if out and out[-1]:
                out.append("")
            continue
        if re.match(r"^\s*[*-]\s+", line):
            flush()
            item = [re.sub(r"^\s*[*-]\s+", "", line)]
        elif item:
            item.append(line.strip())
        else:
            para.append(line.strip())
    flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def main(argv: list[str]) -> int:
    dest = argv[1] if len(argv) > 1 else DEFAULT_OUT
    with open(SOURCE, encoding="utf-8") as f:
        text = convert(f.read())
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(dest, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text.replace("\n", "\r\n"))
    print(f"{os.path.relpath(dest, ROOT)}: {len(text.splitlines())} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
