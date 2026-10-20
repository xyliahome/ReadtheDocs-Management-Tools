# @AI_GENERATED: Kiro v1.0
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TOCTREE_RE = re.compile(r"^\s*\.\.\s+toctree::\s*$")
EXPLICIT_TITLE_RE = re.compile(r"^.+?\s*<([^>]+)>$")
DEFAULT_IGNORED_DIRS = {".git", ".svn", "_build", "build", "__pycache__"}

@dataclass(frozen=True)
class TocReference:
    source: Path
    target: str

def parse_toctree_references(document_file: Path) -> list[str]:
    """读取一个 Markdown 或 RST 文件中所有 toctree 的文档目标。"""
    lines = document_file.read_text(encoding="utf-8").splitlines()
    references: list[str] = []
    line_index = 0

    while line_index < len(lines):
        if not TOCTREE_RE.match(lines[line_index]):
            line_index += 1
            continue

        line_index += 1

        # 跳过指令后的空行和 :maxdepth: 等选项。
        while line_index < len(lines):
            line = lines[line_index]

            if not line.strip():
                line_index += 1
                continue

            # toctree 内容必须缩进；遇到非缩进行表示该指令结束。
            if not line[0].isspace():
                break

            item = line.strip()
            line_index += 1

            if not item or item.startswith(":"):
                continue

            # 支持 ``显示标题 <实际路径>``。
            title_match = EXPLICIT_TITLE_RE.match(item)
            target = title_match.group(1) if title_match else item

            # 外部链接和 self 不代表本地文档。
            if target == "self" or "://" in target:
                continue

            references.append(target)

    return references

def resolve_document(root: Path, source: Path, target: str) -> list[Path]:
    """将 toctree 目标解析为实际存在的 .rst / .md 文档路径。"""
    target = target.split("#", maxsplit=1)[0].strip()
    if not target:
        return []

    base_path = root / target.lstrip("/") if target.startswith("/") else source.parent / target

    # 支持 ``:glob:`` 目录匹配。
    if any(character in target for character in "*?["):
        pattern = str(base_path).replace("\\", "/")
        matches = [
            path.resolve()
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".rst", ".md"}
            and fnmatch.fnmatch(str(path).replace("\\", "/"), pattern)
        ]
        return matches

    candidates: list[Path]
    if base_path.suffix.lower() in {".rst", ".md"}:
        candidates = [base_path]
    else:
        # 与常见 Sphinx 配置保持一致：未带扩展名时优先找 RST。
        candidates = [base_path.with_suffix(".rst"), base_path.with_suffix(".md")]

    return [candidate.resolve() for candidate in candidates if candidate.is_file()]

def collect_documents(root: Path) -> set[Path]:
    """收集文档根目录下所有 Markdown 与 RST 文件。"""
    return {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".rst", ".md"}
        and not any(part in DEFAULT_IGNORED_DIRS for part in path.relative_to(root).parts)
    }

def relative_display_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()

def find_orphan_documents(root: Path, entry_file: Path) -> tuple[set[Path], list[TocReference]]:
    """返回未出现在 TOC 中的文件，以及无法解析的 TOC 引用。"""
    root = root.resolve()
    entry_file = entry_file.resolve()

    if not entry_file.is_file():
        raise FileNotFoundError(f"找不到入口文件：{entry_file}")

    included_documents: set[Path] = {entry_file}
    documents_to_parse: list[Path] = [entry_file]
    parsed_documents: set[Path] = set()
    unresolved_references: list[TocReference] = []

    while documents_to_parse:
        document_file = documents_to_parse.pop()

        if document_file in parsed_documents:
            continue
        parsed_documents.add(document_file)

        for target in parse_toctree_references(document_file):
            resolved_documents = resolve_document(root, document_file, target)

            if not resolved_documents:
                unresolved_references.append(TocReference(document_file, target))
                continue

            for document in resolved_documents:
                included_documents.add(document)

                # Markdown 与 RST 均可能继续定义 toctree，均需递归扫描。
                documents_to_parse.append(document)

    all_documents = collect_documents(root)
    orphan_documents = all_documents - included_documents
    return orphan_documents, unresolved_references

def main() -> int:
    parser = argparse.ArgumentParser(
        description="列出未被 index.rst 及其递归 toctree 引用到的 Markdown / RST 文档。"
    )
    parser.add_argument("docs_root", type=Path, help="文档根目录，例如 .")
    parser.add_argument(
        "--entry",
        default="index.rst",
        help="TOC 入口文件，相对于 docs_root；默认：index.rst",
    )
    parser.add_argument(
        "--fail-on-orphans",
        action="store_true",
        help="发现未纳入 TOC 的文档时以退出码 1 结束，适用于 CI。",
    )
    args = parser.parse_args()

    root = args.docs_root.resolve()
    entry_file = root / args.entry

    try:
        orphan_documents, unresolved_references = find_orphan_documents(root, entry_file)
    except (FileNotFoundError, UnicodeDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    print(f"文档根目录：{root}")
    print(f"TOC 入口：{relative_display_path(root, entry_file.resolve())}")
    print()

    if orphan_documents:
        print(f"未纳入 TOC 的文档（{len(orphan_documents)}）：")
        for document in sorted(orphan_documents, key=lambda path: path.as_posix().lower()):
            print(f"  - {relative_display_path(root, document)}")
    else:
        print("未发现未纳入 TOC 的 Markdown 或 RST 文档。")

    if unresolved_references:
        print(f"\n无法解析的 TOC 引用（{len(unresolved_references)}）：")
        for reference in unresolved_references:
            source = relative_display_path(root, reference.source)
            print(f"  - {source}: {reference.target}")

    return 1 if args.fail_on_orphans and orphan_documents else 0

if __name__ == "__main__":
    raise SystemExit(main())
# @AI_GENERATED: end
