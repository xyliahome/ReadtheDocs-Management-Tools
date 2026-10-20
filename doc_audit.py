#!/usr/bin/env python3
"""Audit documentation repositories for common rich-text and link issues.

This script is designed to run locally against a zh/en documentation pair.
It scans markdown and reStructuredText files, reports potential rendering
issues such as missing local images and link/anchor problems. It also
compares headings between an English repo and a Chinese repo and ensures
anchor IDs are consistent via explicit RST ``.. _label:`` directives placed
before each non-H1 heading.

Anchor strategy (one heading, one id):
  - Every heading except the page title carries exactly one explicit
    ``.. _label:`` directive, on the line immediately above it, separated by a
    single blank line. The page title needs none: the page is addressed by its
    path.
  - No ``.. _label:`` may appear anywhere else. Labels that sit above body
    text, inside a list, above the page title, or stacked two-deep above one
    heading are removed with ``--fix``.
  - An id may only contain lowercase ASCII letters, digits and hyphens, and is
    unique within its file (collisions get a ``-2``, ``-3``, ... suffix).
  - The English repo owns the ids. A Chinese heading takes the id of the
    English heading at the same position, so the two languages always agree.
    That only happens when both files have the same heading structure (same
    count, same levels, same order); otherwise positional pairing would attach
    ids to the wrong headings, so the file is reported for manual alignment
    and its Chinese ids are left alone.
  - An already-present valid id is preserved wherever possible so existing
    links keep working; a new id is derived from the heading text only when
    there is nothing to preserve.
  - The whole label layout of a file is computed first and written in one
    rewrite, never patched in place, so the pass is idempotent and no
    line-number drift can move a label away from its heading.

Table rendering problems (malformed list-table/csv-table, column-count
mismatches, etc.) are intentionally NOT checked by any static/pre-build text
scan -- that produced too many false positives on content that actually
renders fine. They are only ever detected with --build-html, by scanning the
real Sphinx build output for ERROR/WARNING lines naming a table directive,
and are reported for manual review (kind: "table-build-error").

Fix policy (important): this script only ever rewrites a link, image path, or
anchor label when it has verified the result is actually correct (a single
matching file found on disk, or a label id that exactly matches the English
reference). It never invents a target, never shortens an already more
specific link, and never drops an anchor fragment. Anything it cannot verify
this way -- cross-repo links to a different product, ambiguous filename
matches, third-party URLs -- is left untouched and reported for manual
review instead.

Link classification:
  - same-repo absolute link: "/docs/{this repo's name}/{lang}/{version}/..."
    -- treated exactly like an internal link (resolved and, if unambiguous,
    fixed -- keeping the absolute-URL style, never shortened to a relative
    link). {version} is whatever segment is actually there (dev, 3.0.0, ...)
    -- it is not assumed to be "dev".
  - cross-repo link: "/docs/{another repo}/...", "/monitor/...",
    "/app-portal/..." -- cannot be resolved locally. With --check-external,
    verified by requesting https://support.enos-iot.com<path>.
  - third-party link: any other http(s) URL. Ignored unless --check-external
    is passed, in which case it's requested directly and only reported if it
    does not respond.

Links to the same reponame that use a different {version} segment than the
majority of links to that reponame are flagged (not auto-fixed) as
inconsistent-doc-version, since the script has no way to know which version
was actually intended.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Dict, Set, Tuple
from urllib.parse import urlparse


MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".mp4", ".webm", ".pdf",
}

IGNORED_DIRS = {".git", ".venv", "venv", "_build", "node_modules", "__pycache__", ".tox", "dist", "build"}

# Product roots that are never resolvable locally even when they don't use
# the "/docs/{repo}/..." shape.
KNOWN_PRODUCT_PREFIXES = ("monitor", "app-portal")

# /docs/{repo}/{lang}/{version}/{remainder} -- version is whatever segment is
# actually there (dev, 3.0.0, latest, ...), never assumed to be "dev".
DOCS_PATH_RE = re.compile(r"^/docs/([^/]+)/([^/]+)/([^/]+)/(.*)$")
# /{repo}/{lang}/{version}/{remainder} for known non-"/docs/" product roots.
PRODUCT_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/([^/]+)/(.*)$")

# Sphinx's auto-numbered fallback anchor id for headings without a stable
# slug (e.g. duplicate titles). Never something this script can compute --
# only ever discoverable from an actual HTML build.
ID_PATTERN_RE = re.compile(r"^id\d+$")

DEFAULT_DOC_HOST = "https://support.enos-iot.com"
URL_CHECK_TIMEOUT = 10.0
# @AI_GENERATED: Kiro v1.0
# Transient network hiccups (a connection reset mid-handshake, a proxy
# briefly dropping the socket, ...) are not evidence the link is actually
# broken -- a single retry after a short pause is enough to tell a real
# dead link apart from a one-off blip, without slowing down the common case
# where the first attempt already succeeds.
URL_CHECK_RETRIES = 2
URL_CHECK_RETRY_DELAY = 1.0
# @AI_GENERATED: end


def _is_existing_file(path: Path) -> bool:
    """Like Path.exists() but never true for a directory. Doc repos commonly
    have both "name.rst" (a page) and "name/" (that page's sub-toctree)
    side by side; treating the directory as a match crashes downstream
    read_text() calls and silently resolves links to the wrong target."""
    try:
        return path.is_file()
    except OSError:
        return False


@dataclass
class Heading:
    line_no: int
    level: int
    text: str
    source: str
    anchor: Optional[str] = None


@dataclass
class Issue:
    kind: str
    file: str
    line: Optional[int]
    message: str
    severity: str = "warning"
    details: Optional[Dict[str, object]] = None


class AuditRunner:
    def __init__(
        self,
        en_root: Optional[Path] = None,
        zh_root: Optional[Path] = None,
        fix: bool = False,
        build_html: bool = False,
        build_dir: Optional[Path] = None,
        repo_name: Optional[str] = None,
        check_external: bool = False,
    ) -> None:
        self.en_root = en_root.resolve() if en_root else None
        self.zh_root = zh_root.resolve() if zh_root else None
        self.fix = fix
        self.build_html = build_html
        self.build_dir = build_dir.resolve() if build_dir else None
        self.repo_name = repo_name or self._infer_repo_name(self.en_root, self.zh_root)
        self.check_external = check_external
        self.issues: List[Issue] = []
        self._url_check_cache: Dict[str, Tuple[bool, str]] = {}
        # (rel_path, line, reponame, version_seg, target, lang) for every
        # "/docs/{repo}/{lang}/{version}/..." (or known product-prefix) link
        # seen, used to flag version segments that disagree with the
        # majority used for that reponame elsewhere in the corpus.
        # @AI_GENERATED: Kiro v1.0
        self._doc_version_observations: List[Tuple[str, int, str, str, str, str]] = []
        # @AI_GENERATED: end

    # @AI_GENERATED: Kiro v1.0
    @staticmethod
    def _lang_tag(lang: str) -> str:
        """Render a language marker ("[en]"/"[zh]"/"[en/zh]") to prefix an
        issue's file field, so a report covering both repos always shows
        which repo a given finding actually came from. "en/zh" is used for
        findings that inherently compare both languages against the same
        relative path (e.g. heading-count-mismatch, anchor-alignment)."""
        lang_lower = (lang or "").lower()
        if lang_lower in ("en/zh", "zh/en"):
            return "[en/zh]"
        if lang_lower.startswith("en"):
            return "[en]"
        if lang_lower.startswith("zh"):
            return "[zh]"
        return f"[{lang}]" if lang else ""

    @classmethod
    def _tag_rel_path(cls, rel_path: str, lang: str) -> str:
        tag = cls._lang_tag(lang)
        return f"{tag} {rel_path}" if tag else rel_path
    # @AI_GENERATED: end

    @staticmethod
    def _infer_repo_name(en_root: Optional[Path], zh_root: Optional[Path]) -> Optional[str]:
        for root in (en_root, zh_root):
            if not root:
                continue
            stripped = re.sub(r"[_-](en|zh)([_-][a-zA-Z]{2})?$", "", root.name, flags=re.I)
            if stripped:
                return stripped
        return None

    def run(self) -> Dict[str, object]:
        if self.build_html:
            self._build_sphinx_html()

        en_files = self._collect_files(self.en_root) if self.en_root else []
        zh_files = self._collect_files(self.zh_root) if self.zh_root else []

        # Load HTML anchor maps when --build-html is active (enables #idN resolution)
        self._html_anchor_map: Dict[str, Dict[str, Set[str]]] = {}
        if self.build_html:
            if self.en_root:
                self._html_anchor_map["en"] = self._load_html_anchor_map(self.en_root)
            if self.zh_root:
                self._html_anchor_map["zh"] = self._load_html_anchor_map(self.zh_root)

        # @AI_GENERATED: Kiro v1.0
        # Anchor handling is a single normalization pass per repo instead of
        # the previous three separate passes (insert / dedupe / align).  Those
        # passes each rewrote the same files while holding line numbers parsed
        # *before* the other pass had inserted or deleted lines, so every
        # insertion silently shifted the positions the next one relied on --
        # that is what produced stacked labels above one heading, labels
        # stranded in the middle of bullet lists, and ids attached to the
        # wrong heading.  The normalization pass computes the complete desired
        # label layout for a file first and then rewrites the file exactly
        # once, so no stale index can ever be used.
        anchor_result = self._normalize_anchors(en_files, zh_files)
        self.issues.extend(anchor_result["issues"])

        if self.en_root:
            self._audit_repo(self.en_root, en_files, "en")
        if self.zh_root:
            self._audit_repo(self.zh_root, zh_files, "zh")

        if self.zh_root and anchor_result["zh_changed_files"]:
            self.issues.extend(self._apply_anchor_reference_renames(
                self.zh_root, zh_files, anchor_result["zh_renames"],
                anchor_result["zh_changed_files"], anchor_result["zh_new_anchors"], "zh"))
        if self.en_root and anchor_result["en_changed_files"]:
            self.issues.extend(self._apply_anchor_reference_renames(
                self.en_root, en_files, anchor_result["en_renames"],
                anchor_result["en_changed_files"], anchor_result["en_new_anchors"], "en"))
        # @AI_GENERATED: end

        self.issues.extend(self._check_version_consistency())

        actionable = self._filter_actionable_issues(self.issues)
        summary = {
            "en_repo": str(self.en_root) if self.en_root else None,
            "zh_repo": str(self.zh_root) if self.zh_root else None,
            "repo_name": self.repo_name,
            "files_scanned": {
                "en": len(en_files),
                "zh": len(zh_files),
            },
            "issues": [asdict(item) for item in actionable],
            "issue_count": len(actionable),
        }
        return summary

    def _build_sphinx_html(self) -> None:
        repos = []
        if self.en_root:
            repos.append(self.en_root)
        if self.zh_root:
            repos.append(self.zh_root)
        for repo in repos:
            if not (repo / "conf.py").exists():
                continue
            build_dir = self.build_dir / repo.name if self.build_dir else repo / "_build" / "html"
            python_exe = self._ensure_build_prereqs(repo)
            # Sanitize conf.py to fix known incompatibilities with newer
            # Sphinx versions (e.g. invalid intersphinx_mapping format).
            # @AI_GENERATED: Kiro v1.0
            # Keep a backup even when the initial sanitization does not edit
            # conf.py: a later config or theme fallback may still patch it.
            conf_backup = (repo / "conf.py").read_text(encoding="utf-8", errors="ignore")
            self._sanitize_conf_py(repo)
            theme_retry_attempted = False
            # @AI_GENERATED: end
            try:
                # -E: ignore any saved/pickled environment and re-read every
                # source file. Without this, Sphinx's incremental build can
                # silently keep serving HTML generated by a *previous*, broken
                # config/extension state even after conf.py or requirements.txt
                # has been fixed, because its change-detection doesn't reliably
                # cover every kind of edit (e.g. requirements.txt changes are
                # invisible to it entirely). That produces exactly the
                # confusing "I fixed it but nothing changed" symptom.
                cmd = [python_exe, "-m", "sphinx", "-b", "html", "-E", str(repo), str(build_dir)]
                print(f"Building HTML for {repo.name}: {' '.join(cmd)}")
                completed = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self._subprocess_env())

                # Sphinx can exit 0 while still logging WARNINGs that mean a
                # page came out empty or broken (e.g. an unparseable source
                # file, a bad toctree entry) -- those are exactly the kind of
                # thing worth seeing, so the output is always saved, not just
                # on a hard failure.
                combined = (completed.stdout or "") + (completed.stderr or "")

                # If Sphinx failed due to intersphinx_mapping or other config
                # errors, attempt a more aggressive fix and retry once.
                if completed.returncode != 0 and ("intersphinx_mapping" in combined or "ConfigError" in combined or "Configuration error" in combined):
                    print(f"  [doc_audit] Sphinx config error detected for {repo.name}, applying aggressive fix and retrying...", file=sys.stderr)
                    self._sanitize_conf_py_aggressive(repo)
                    completed = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self._subprocess_env())
                    combined = (completed.stdout or "") + (completed.stderr or "")

                log_path = repo / "sphinx_build_output.log"
                try:
                    log_path.write_text(combined, encoding="utf-8", errors="ignore")
                except OSError:
                    log_path = None

                # @AI_GENERATED: Kiro v1.0
                if completed.returncode != 0:
                    # Retry each repository once: one failed theme must not
                    # suppress the fallback for the next repository.
                    if "ThemeError" in combined and not theme_retry_attempted:
                        theme_retry_attempted = True
                        print(f"  [doc_audit] ThemeError detected for {repo.name}, retrying with built-in 'alabaster' theme...", file=sys.stderr)
                        self._patch_theme_to_alabaster(repo)
                        completed = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self._subprocess_env())
                        combined = (completed.stdout or "") + (completed.stderr or "")
                # @AI_GENERATED: end

                # @AI_GENERATED: Kiro v1.0
                if completed.returncode != 0:
                    log_path = repo / "sphinx_build_output.log"
                    try:
                        log_path.write_text(combined, encoding="utf-8", errors="ignore")
                    except OSError:
                        log_path = None
                    tail = "\n".join(combined.strip().splitlines()[-10:])
                    print(f"  [doc_audit] ERROR: Sphinx build failed for {repo.name}.", file=sys.stderr)
                    print(f"  Last lines: {tail}", file=sys.stderr)
                    log_note = f" Full build output: {log_path}" if log_path else ""
                    raise RuntimeError(f"Sphinx build failed for {repo.name}.\n{tail}{log_note}")
                # @AI_GENERATED: end

                warning_lines = [line for line in combined.splitlines() if "WARNING" in line]
                if warning_lines:
                    note = f" See {log_path} for the full build output." if log_path else ""
                    print(f"Sphinx build for {repo.name} succeeded but logged {len(warning_lines)} warning(s) -- a warned-about page may be empty or broken.{note}", file=sys.stderr)
                    for line in warning_lines[:10]:
                        print(f"  {line}", file=sys.stderr)

                # @AI_GENERATED: Kiro v1.0
                # Table rendering problems (malformed list-table/csv-table,
                # mismatched columns, etc.) are only ever detected here, from
                # an actual Sphinx HTML build -- never from a static
                # pre-build scan of the source text. Docutils reports these
                # as ERROR/WARNING lines in the build output without
                # necessarily failing the build (Sphinx keeps going and
                # simply renders a broken/partial table), so the return code
                # alone can't be trusted to surface them; the output has to
                # be scanned directly.
                table_build_issues = self._check_table_build_errors(repo, combined)
                if table_build_issues:
                    self.issues.extend(table_build_issues)
                    print(
                        f"  [doc_audit] Sphinx build for {repo.name} reported "
                        f"{len(table_build_issues)} table rendering problem(s) -- flagged for manual review.",
                        file=sys.stderr,
                    )
                # @AI_GENERATED: end


            finally:
                # Restore original conf.py if it was modified
                self._restore_conf_py(repo, conf_backup)

    # @AI_GENERATED: Kiro v1.0
    _ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
    # Docutils/Sphinx table-related build messages, e.g.:
    #   "...view_asset_list.md:73: ERROR: Error parsing content block for
    #    the "list-table" directive: uniform two-level bullet list expected,
    #    but row 2 does not contain the same number of items as row 1 (1 vs 3)."
    #   "...config_charting_tool.md:85: ERROR: Error with CSV data in
    #    "csv-table" directive: ',' expected after '"'"
    # @AI_GENERATED: Kiro v1.0
    # Only match Sphinx/docutils messages that reference a table *directive*
    # (list-table, csv-table, or the generic "table" directive).  The
    # previous pattern used IGNORECASE and matched any line containing the
    # substring "table" anywhere in the message, producing false positives
    # on unrelated warnings that happen to contain that word.
    _TABLE_BUILD_MSG_RE = re.compile(
        r'^(?P<file>.+?):(?:(?P<line>\d+):)?\s*(?P<level>WARNING|ERROR):\s*(?P<msg>.*(?:"list-table"|"csv-table"|"table")\s+directive.*)$',
        re.IGNORECASE,
    )
    # @AI_GENERATED: end

    def _check_table_build_errors(self, repo: Path, build_output: str) -> List[Issue]:
        """Scan raw Sphinx build output for table-rendering problems.

        This is the only place table issues are ever reported: real
        rendering failures (broken list-table/csv-table structure, column
        mismatches Sphinx itself rejects, etc.) can only be known once
        Sphinx has actually tried to build the HTML. A static pre-build
        text scan produces too many false positives on content that
        happens to look table-like but renders fine, so that check is
        intentionally not run; only genuine build-time errors/warnings
        naming a table directive are surfaced here, for manual review.
        """
        issues: List[Issue] = []
        clean_output = self._ANSI_ESCAPE_RE.sub("", build_output)
        # @AI_GENERATED: Kiro v1.0
        # Determine language label from repo path for disambiguation in reports
        repo_name_lower = repo.name.lower()
        if repo_name_lower.endswith("_zh") or "zh" in repo_name_lower.split("_"):
            lang_label = "[zh]"
        elif repo_name_lower.endswith("_en") or "en" in repo_name_lower.split("_"):
            lang_label = "[en]"
        else:
            lang_label = f"[{repo.name}]"
        # @AI_GENERATED: end
        for raw_line in clean_output.splitlines():
            line = raw_line.strip()
            m = self._TABLE_BUILD_MSG_RE.match(line)
            if not m:
                continue
            file_part = m.group("file").strip()
            try:
                rel_file = Path(file_part).resolve().relative_to(repo).as_posix()
            except (OSError, ValueError):
                rel_file = file_part
            line_no = int(m.group("line")) if m.group("line") else None
            level = m.group("level").upper()
            # @AI_GENERATED: Kiro v1.0
            # Prefix file path with language label so the report clearly
            # shows which repo the error came from
            display_file = f"{lang_label} {rel_file}"
            # @AI_GENERATED: end
            issues.append(Issue(
                kind="table-build-error",
                file=display_file,
                line=line_no,
                message=f"Sphinx {level.lower()} while rendering a table: {m.group('msg').strip()}",
                severity="error" if level == "ERROR" else "warning",
                details={
                    "status": "needs_manual_review",
                    "source": "sphinx-build",
                    "repo": repo.name,
                    "note": "Line number is from Sphinx/myst-parser internal representation and may not match the source file exactly (can exceed actual file line count due to internal expansion of include directives or myst conversion offsets).",
                },
            ))
        return issues
    # @AI_GENERATED: end

    def _sanitize_conf_py(self, repo: Path) -> Optional[str]:
        """Fix known conf.py incompatibilities with newer Sphinx versions.
        Returns the original content (for restoration) if changes were made,
        or None if no changes were needed.

        Known fixes:
        1. intersphinx_mapping: Sphinx >= 7.x requires values to be
           two-element tuples (url, inventory). Old format like
           {'https://docs.python.org/': None} is invalid -- it must be
           {'python': ('https://docs.python.org/', None)}.
        2. html_static_path: entries that don't exist cause a WARNING that
           can obscure real issues; remove them.
        3. exclude_patterns: ensure .venv, _build, and other build artifacts
           are excluded so Sphinx doesn't try to render them as doc pages.
        """
        conf_path = repo / "conf.py"
        if not conf_path.exists():
            return None

        original = conf_path.read_text(encoding="utf-8", errors="ignore")
        modified = original
        changed = False

        # Fix 1: intersphinx_mapping with invalid format.
        intersphinx_re = re.compile(
            r"^(\s*intersphinx_mapping\s*=\s*)\{([^}]*)\}",
            re.MULTILINE,
        )
        m = intersphinx_re.search(modified)
        if m:
            mapping_body = m.group(2).strip()
            old_entry_re = re.compile(
                r"""(['"])(https?://[^'"]+)\1\s*:\s*None"""
            )
            old_entries = old_entry_re.findall(mapping_body)
            if old_entries:
                new_entries = []
                for i, (_, url) in enumerate(old_entries):
                    domain = urlparse(url).netloc.replace(".", "_")
                    label = domain.replace("docs_python_org", "python").replace("www_", "")
                    if not label:
                        label = f"ext{i}"
                    new_entries.append(f"'{label}': ('{url}', None)")
                new_mapping = f"{m.group(1)}{{{', '.join(new_entries)}}}"
                modified = modified[:m.start()] + new_mapping + modified[m.end():]
                changed = True

        # Fix 2: html_static_path entries that don't exist on disk.
        static_re = re.compile(
            r"^(\s*html_static_path\s*=\s*\[)([^\]]*)\]",
            re.MULTILINE,
        )
        sm = static_re.search(modified)
        if sm:
            entries_str = sm.group(2)
            entry_re = re.compile(r"""['"]([^'"]+)['"]""")
            entries = entry_re.findall(entries_str)
            valid_entries = []
            for entry in entries:
                entry_path = repo / entry.lstrip("./")
                if entry_path.exists():
                    valid_entries.append(entry)
            if len(valid_entries) < len(entries):
                if valid_entries:
                    new_list = ", ".join(f"'{e}'" for e in valid_entries)
                    replacement = f"{sm.group(1)}{new_list}]"
                else:
                    replacement = f"{sm.group(1)}]"
                modified = modified[:sm.start()] + replacement + modified[sm.end():]
                changed = True

        # Fix 3: Ensure .venv and other non-doc directories are excluded.
        # Without this, Sphinx scans .venv/Lib/site-packages and tries to
        # render random package files as documentation pages, crashing on
        # theme errors or producing garbage output.
        must_exclude = ['.venv', '_build', 'node_modules', '__pycache__', '.git', '**/HIDDEN-*']
        exclude_re = re.compile(
            r"^(\s*exclude_patterns\s*=\s*\[)([^\]]*)\]",
            re.MULTILINE,
        )
        em = exclude_re.search(modified)
        if em:
            existing_str = em.group(2)
            existing_entries = re.findall(r"""['"]([^'"]+)['"]""", existing_str)
            additions = [d for d in must_exclude if d not in existing_entries]
            if additions:
                if existing_entries:
                    new_list = ", ".join(f"'{e}'" for e in existing_entries + additions)
                else:
                    new_list = ", ".join(f"'{e}'" for e in additions)
                replacement = f"{em.group(1)}{new_list}]"
                modified = modified[:em.start()] + replacement + modified[em.end():]
                changed = True
        else:
            # No exclude_patterns found at all -- append one
            exclude_line = f"\nexclude_patterns = [{', '.join(repr(d) for d in must_exclude)}]\n"
            modified += exclude_line
            changed = True

        if changed:
            conf_path.write_text(modified, encoding="utf-8")
            print(f"  [doc_audit] Temporarily patched conf.py for {repo.name} (intersphinx_mapping / html_static_path / exclude_patterns)")
            return original
        return None

    def _restore_conf_py(self, repo: Path, backup: Optional[str]) -> None:
        """Restore original conf.py content after building."""
        if backup is None:
            return
        conf_path = repo / "conf.py"
        conf_path.write_text(backup, encoding="utf-8")

    def _sanitize_conf_py_aggressive(self, repo: Path) -> None:
        """More aggressive conf.py fix: completely disable intersphinx and
        remove any html_static_path that doesn't exist. Called as a retry
        when the first sanitize pass didn't resolve the build failure.

        This does NOT return a backup -- the caller already holds the original
        from _sanitize_conf_py and will restore from that."""
        conf_path = repo / "conf.py"
        if not conf_path.exists():
            return

        text = conf_path.read_text(encoding="utf-8", errors="ignore")

        # Remove sphinx.ext.intersphinx from extensions list
        text = re.sub(
            r"""['"]sphinx\.ext\.intersphinx['"]\s*,?\s*""",
            "",
            text,
        )

        # Comment out the entire intersphinx_mapping line
        text = re.sub(
            r"^(\s*intersphinx_mapping\s*=\s*.+)$",
            r"# \1  # disabled by doc_audit",
            text,
            flags=re.MULTILINE,
        )

        # Also nuke html_static_path if the directory doesn't exist
        text = re.sub(
            r"^(\s*html_static_path\s*=\s*)\[[^\]]*\]",
            r"\1[]",
            text,
            flags=re.MULTILINE,
        )

        conf_path.write_text(text, encoding="utf-8")
        print(f"  [doc_audit] Aggressively patched conf.py for {repo.name} (disabled intersphinx entirely)")

    def _patch_theme_to_alabaster(self, repo: Path) -> None:
        """Replace a third-party Sphinx theme with the built-in 'alabaster'
        theme when a ThemeError indicates incompatibility with the current
        Sphinx version. This is a last-resort fallback so that the HTML
        build can at least produce output for anchor/link validation."""
        conf_path = repo / "conf.py"
        if not conf_path.exists():
            return

        text = conf_path.read_text(encoding="utf-8", errors="ignore")

        # Replace html_theme = '...' with 'alabaster'
        text = re.sub(
            r"""^(\s*html_theme\s*=\s*)(['"])([^'"]+)\2""",
            r"\1'alabaster'",
            text,
            flags=re.MULTILINE,
        )

        # Comment out html_theme_path (not needed for built-in themes)
        text = re.sub(
            r"^(\s*html_theme_path\s*=.+)$",
            r"# \1  # disabled by doc_audit (using alabaster)",
            text,
            flags=re.MULTILINE,
        )

        # Comment out any import of the third-party theme module
        text = re.sub(
            r"^(\s*import\s+sphinx_\w+_theme.*)$",
            r"# \1  # disabled by doc_audit",
            text,
            flags=re.MULTILINE,
        )

        # @AI_GENERATED: Kiro v1.0
        # Third-party option keys are not meaningful to alabaster and produce
        # warnings; keep the fallback build focused on document validation.
        text = re.sub(
            r"^(\s*html_theme_options\s*=\s*)\{.*?^\s*\}",
            r"\1{}",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        # @AI_GENERATED: end

        conf_path.write_text(text, encoding="utf-8")
        print(f"  [doc_audit] Switched {repo.name} to 'alabaster' theme for build compatibility")

    def _ensure_build_prereqs(self, repo: Path) -> str:
        """Provision a venv with the repo's requirements (e.g. its Sphinx
        theme package) and return the python executable the build should
        actually use. Installing into a venv but then building with the
        ambient interpreter -- as this used to do -- means the installed
        packages are never seen by Sphinx.

        If a previous run left an incomplete .venv (interrupted `python -m
        venv`, disk full, etc.), silently falling back to the ambient
        Python here would make Sphinx fail with a confusing
        ModuleNotFoundError for a package that actually *is* declared in
        requirements.txt -- indistinguishable from the dependency really
        being missing. Rebuilding a broken venv is cheap and safe (it's
        disposable build tooling, not user content), so that's done
        automatically instead; if even a fresh venv doesn't produce a
        working python executable, this raises loudly rather than silently
        using the wrong interpreter."""
        req_file = repo / "requirements.txt"
        if not req_file.exists():
            return sys.executable

        venv_dir = repo / ".venv"
        python_exe = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

        if venv_dir.exists() and not python_exe.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)

        env = self._subprocess_env()

        if not venv_dir.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True, text=True, env=env)

        if not python_exe.exists():
            raise RuntimeError(
                f"`python -m venv` did not produce a usable interpreter at {python_exe}. "
                f"Delete {venv_dir} and re-run, or check that {sys.executable} can create venvs on this machine."
            )

        install_cmd = [str(python_exe), "-m", "pip", "install", "--upgrade", "pip"]
        subprocess.run(install_cmd, check=True, capture_output=True, text=True, env=env)

        install_req_cmd = [str(python_exe), "-m", "pip", "install", "-r", str(req_file)]
        subprocess.run(install_req_cmd, check=True, capture_output=True, text=True, env=env)
        return str(python_exe)

    def _subprocess_env(self) -> Dict[str, str]:
        """Force UTF-8 mode for every build-related subprocess. Without it,
        on a non-UTF-8 Windows locale (e.g. Simplified Chinese, cp936), any
        library in the chain (Sphinx, docutils, Jinja2, m2r, myst_parser,
        the theme, ...) that does `open(path, 'w')` without an explicit
        encoding gets bitten by Python defaulting text-mode file I/O to
        locale.getpreferredencoding(). The HTML still declares
        <meta charset="utf-8"> but the bytes on disk are actually cp936 --
        the page isn't empty, it's mojibake, and only non-ASCII (Chinese)
        content is affected since ASCII is byte-identical in both encodings.
        PYTHONUTF8=1 overrides the locale default for every open() call
        inside the subprocess, regardless of which dependency forgot to
        pin an encoding."""
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _collect_files(self, root: Path) -> List[Path]:
        if not root.exists():
            return []
        files = []
        for path in root.rglob("*"):
            if path.is_dir() and path.name in IGNORED_DIRS:
                continue
            if path.is_file() and path.suffix.lower() in {".md", ".rst"} and not any(part in IGNORED_DIRS for part in path.parts):
                # @AI_GENERATED: Kiro v1.0
                # Skip files whose name starts with "HIDDEN-" — these are
                # deprecated/unused files kept in the repo for reference but
                # no longer published or maintained.
                if path.name.startswith("HIDDEN-"):
                    continue
                # @AI_GENERATED: end
                files.append(path)
        return sorted(files)

    def _collect_media_files(self, root: Path) -> List[Path]:
        if not root.exists():
            return []
        files = []
        for path in root.rglob("*"):
            if path.is_dir() and path.name in IGNORED_DIRS:
                continue
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and not any(part in IGNORED_DIRS for part in path.parts):
                files.append(path)
        return sorted(files)

    def _audit_repo(self, root: Path, files: List[Path], lang: str) -> List[Dict[str, object]]:
        reports = []
        media_files = self._collect_media_files(root)
        for path in files:
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            headings = self._parse_headings(text, rel, lang)
            links = self._parse_local_links(text, rel)
            findings = self._check_rich_text_patterns(text, rel, lang)
            self.issues.extend(findings)

            if lang == "en":
                pass  # English heading tag checks removed (tags are no longer used)

            fix_spans: List[Tuple[int, int, str]] = []

            media_issues, media_fix_spans = self._check_media_targets(path, root, rel, text, media_files, lang)
            self.issues.extend(media_issues)
            fix_spans.extend(media_fix_spans)

            # @AI_GENERATED: Kiro v1.0
            directive_content_spans = self._rst_directive_content_spans(text)
            protected_spans = self._protected_content_spans(text)
            for link in links:
                link_issues, fix_span = self._check_link_target(path, root, link, rel, lang, files)
                self.issues.extend(link_issues)

                is_directive_markdown_link = (
                    link["kind"] in {"markdown", "markdown-anchor"}
                    and self._offset_is_in_spans(int(link["match_start"]), directive_content_spans)
                    and not self._offset_is_in_spans(int(link["match_start"]), protected_spans)
                )
                if self.fix and is_directive_markdown_link:
                    replacement_target = str(fix_span[2]) if fix_span else str(link["target"])
                    fix_spans.append((
                        int(link["match_start"]),
                        int(link["match_end"]),
                        f"`{link['text']} <{replacement_target}>`_",
                    ))
                elif fix_span:
                    fix_spans.append(fix_span)
            # @AI_GENERATED: end

            if self.fix and fix_spans:
                self._apply_text_fixes(path, text, fix_spans)

            if headings:
                reports.append({"file": rel, "headings": headings})
        return reports

    # @AI_GENERATED: Kiro v1.0
    # Pattern for an explicit RST anchor label that immediately precedes a
    # heading.  The label is the anchor id for that heading; blank lines
    # between the label and the heading are allowed (Sphinx/docutils accept
    # them) but this scanner requires zero or one blank line to avoid
    # spuriously attributing a label to a distant heading.
    _RST_LABEL_RE = re.compile(r"^\.\.\s+_([^:]+):\s*$")
    # @AI_GENERATED: end

    # @AI_GENERATED: Kiro v1.0
    # Matches CJK Unified Ideographs and common CJK punctuation/fullwidth
    # ranges.  Any anchor id containing these must not be used.
    _CJK_RE = re.compile(
        r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
    )
    # @AI_GENERATED: end

    def _parse_headings(self, text: str, rel_path: str, lang: str) -> List[Heading]:
        # @AI_GENERATED: Kiro v1.0
        """Parse headings from a Markdown or RST file.

        Anchor strategy (new):
        - If a ``.. _label:`` line appears immediately before a heading
          (with at most one blank line between them), that label is the
          heading's anchor id.
        - For headings without a preceding label the anchor falls back to
          the Sphinx slug of the heading text (same formula as before),
          so link-checking still works for files that haven't been
          migrated yet.
        - Any ``<tag>`` embedded in the heading *text* is stripped before
          reporting (it is no longer the anchor source).
        """
        lines = text.splitlines()
        headings: List[Heading] = []
        in_code_block = False

        # Pre-scan: build a map of line_index -> rst label for lines that
        # carry a ``.. _label:`` directive.  This lets the heading parser
        # look back cheaply without rescanning.  Labels shown inside a fenced
        # code block are sample text, not markup, so _label_lines skips them.
        label_at: Dict[int, str] = self._label_lines(lines)

        def _preceding_label(heading_line_idx: int) -> Optional[str]:
            # @AI_GENERATED: Kiro v1.0
            """Return the RST label closest above the heading, skipping any
            number of blank lines and <br /> directives between the label and
            the heading.  Returns None if a non-blank, non-br, non-label line
            is encountered first (meaning the label belongs to something else).
            """
            prev = heading_line_idx - 1
            while prev >= 0:
                stripped = lines[prev].strip()
                if not stripped or stripped in self._BR_LINES:
                    prev -= 1
                    continue
                if prev in label_at:
                    return label_at[prev]
                break  # hit a real content line that is not a label — stop
            return None
            # @AI_GENERATED: end

        i = 0
        while i < len(lines):
            line = lines[i]
            # Skip fenced code block content.
            if re.match(r"^\s*(`{3,}|~{3,})", line):
                in_code_block = not in_code_block
                i += 1
                continue
            if in_code_block:
                i += 1
                continue

            md_match = re.match(r"^(#{1,6})\s+(.*)$", line)
            if md_match:
                level = len(md_match.group(1))
                # Strip any residual <tag> from heading text -- it is no
                # longer the anchor source, just visual noise to remove.
                raw_title = md_match.group(2).strip()
                title = re.sub(r"\s*<[^>]+>", "", self._strip_inline_markers(raw_title)).strip()
                anchor = _preceding_label(i) or self._build_anchor_id(title, lang)
                headings.append(Heading(i + 1, level, title, rel_path, anchor))
                i += 1
                continue

            # RST underline-style headings must start at column 0.
            if line and line[0] in (" ", "\t"):
                i += 1
                continue
            if i + 1 < len(lines) and lines[i + 1].strip() and re.fullmatch(r"[=\-`~^\"'#+*:.]+", lines[i + 1].strip()):
                title = line.strip()
                if title:
                    underline = lines[i + 1].strip()
                    level = 1 if underline[0] in "=" else 2
                    clean_title = re.sub(r"\s*<[^>]+>", "", self._strip_inline_markers(title)).strip()
                    anchor = _preceding_label(i) or self._build_anchor_id(clean_title, lang)
                    headings.append(Heading(i + 1, level, clean_title, rel_path, anchor))
                    i += 2
                else:
                    i += 1
                continue

            i += 1
        return headings
    # @AI_GENERATED: end

    # @AI_GENERATED: Kiro v1.0
    def _collect_rst_anchors(self, text: str) -> Set[str]:
        """Return every explicit RST anchor label declared in *text*.

        Matches lines of the form ``.. _label:`` (with optional surrounding
        whitespace) and returns the label values as a set.  These ids are
        explicit and always win over Sphinx's auto-generated slugs for the
        purpose of link validation.  Labels inside fenced code blocks are
        sample text, not real targets, and are skipped.
        """
        return set(self._label_lines(text.splitlines()).values())
    # @AI_GENERATED: end

    # ------------------------------------------------------------------
    # Anchor normalization
    # ------------------------------------------------------------------
    # @AI_GENERATED: Kiro v1.0
    # Lines that may sit between a ``.. _label:`` directive and the heading it
    # belongs to without breaking the association.
    _BR_LINES = ("<br />", "<br/>", "<br>")
    _FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
    # A usable anchor id: ASCII lowercase letters, digits and single hyphens.
    # A leading digit is accepted because the corpus already contains such ids
    # ("3d-model", "1-define-parameter-information") and rewriting them would
    # break every published link that points at them.
    _VALID_ANCHOR_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    @classmethod
    def _is_valid_anchor_id(cls, anchor_id: Optional[str]) -> bool:
        return bool(anchor_id) and bool(cls._VALID_ANCHOR_ID_RE.match(anchor_id or ""))

    def _label_lines(self, lines: List[str]) -> Dict[int, str]:
        """Map 0-based line index -> anchor id for every ``.. _id:`` line that
        is real markup, i.e. outside fenced code blocks. Fenced blocks are
        skipped so a label shown as sample text in a code example is never
        moved, renamed or counted as a duplicate."""
        found: Dict[int, str] = {}
        in_fence = False
        for idx, raw in enumerate(lines):
            if self._FENCE_RE.match(raw):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            match = self._RST_LABEL_RE.match(raw)
            if match:
                found[idx] = match.group(1).strip()
        return found

    def _attached_labels(
        self, lines: List[str], headings: List[Heading], label_lines: Dict[int, str]
    ) -> Dict[int, List[int]]:
        """heading index -> the label line indices that belong to it, nearest
        first. A label belongs to a heading when only blank lines, ``<br>``
        markers and other labels separate the two."""
        attached: Dict[int, List[int]] = {}
        for h_idx, heading in enumerate(headings):
            found: List[int] = []
            prev = heading.line_no - 2  # the line directly above the heading
            while prev >= 0:
                stripped = lines[prev].strip()
                if not stripped or stripped in self._BR_LINES:
                    prev -= 1
                    continue
                if prev in label_lines:
                    found.append(prev)
                    prev -= 1
                    continue
                break  # real content that is not a label: stop
            attached[h_idx] = found
        return attached

    def _desired_anchor_ids(
        self,
        headings: List[Heading],
        attached: Dict[int, List[int]],
        label_lines: Dict[int, str],
        lang: str,
        reference: Optional[List[Optional[str]]] = None,
    ) -> Tuple[List[Optional[str]], Dict[int, str]]:
        """Decide the single anchor id every heading should carry.

        Rules, in priority order:
        1. The first heading is the page title and never carries a label; the
           page itself is already addressable by its path.
        2. ``reference`` -- the index-aligned English ids for the same file --
           wins when present. This is what keeps zh and en ids identical.
        3. Otherwise an already-attached, syntactically valid id is kept so
           links that point at it stay valid.
        4. Only when there is none is a new id derived from the heading text.
        5. Ids are made unique inside the file by appending -2, -3, ...

        Returns ``(desired, collisions)`` where ``desired[i]`` is the id for
        ``headings[i]`` (``None`` = no label) and ``collisions`` maps a heading
        index to the base id it collided with, for reporting.
        """
        desired: List[Optional[str]] = [None] * len(headings)
        collisions: Dict[int, str] = {}
        used: Set[str] = set()
        for h_idx, heading in enumerate(headings):
            if h_idx == 0:
                continue
            candidate: Optional[str] = reference[h_idx] if reference else None
            if not candidate:
                for label_idx in attached.get(h_idx, []):
                    existing = label_lines.get(label_idx, "")
                    if self._is_valid_anchor_id(existing):
                        candidate = existing
                        break
            if not candidate:
                candidate = self._sanitize_anchor_id(self._build_anchor_id(heading.text, lang))
            if not self._is_valid_anchor_id(candidate):
                # Nothing usable -- e.g. a pure-CJK heading in a file that has
                # no English counterpart. Leaving the heading unlabeled is
                # better than inventing an id that cannot be kept in sync with
                # the other language.
                continue
            unique = candidate
            suffix = 2
            while unique in used:
                unique = f"{candidate}-{suffix}"
                suffix += 1
            if unique != candidate:
                collisions[h_idx] = candidate
            used.add(unique)
            desired[h_idx] = unique
        return desired, collisions

    def _rewrite_anchor_labels(
        self,
        lines: List[str],
        headings: List[Heading],
        label_lines: Dict[int, str],
        desired: List[Optional[str]],
    ) -> List[str]:
        """Rebuild the file's label layout in a single pass.

        Every existing ``.. _id:`` line is dropped -- together with the blank
        line that follows it, so removing a label never leaves a stray blank
        line splitting a list or paragraph -- and exactly one label is emitted
        immediately above each heading that has a desired id.

        Rebuilding instead of patching in place is what makes this idempotent
        and is the fix for the earlier corruption: the previous code inserted
        and deleted lines while still using line numbers parsed before those
        edits, so labels drifted upward into body text and stacked up above
        the wrong heading. Here the output depends only on the heading
        positions in the input and the desired ids, so nothing can drift.
        """
        label_for_heading_line: Dict[int, str] = {}
        for h_idx, heading in enumerate(headings):
            if desired[h_idx]:
                label_for_heading_line[heading.line_no - 1] = str(desired[h_idx])

        drop: Set[int] = set()
        for idx in label_lines:
            drop.add(idx)
            if idx + 1 < len(lines) and not lines[idx + 1].strip():
                drop.add(idx + 1)

        out: List[str] = []
        for idx, raw in enumerate(lines):
            if idx in drop:
                continue
            if idx in label_for_heading_line:
                if out and out[-1].strip():
                    out.append("")
                out.append(f".. _{label_for_heading_line[idx]}:")
                out.append("")
            out.append(raw)
        return out

    def _file_anchor_model(self, path: Path, rel_path: str, lang: str):
        """Read a file and return everything the anchor passes need:
        ``(text, lines, headings, label_lines, attached)``."""
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        headings = self._parse_headings(text, rel_path, lang)
        label_lines = self._label_lines(lines)
        attached = self._attached_labels(lines, headings, label_lines)
        return text, lines, headings, label_lines, attached

    def _normalize_file_anchors(
        self,
        path: Path,
        rel_path: str,
        lang: str,
        reference: Optional[List[Optional[str]]] = None,
    ) -> Dict[str, object]:
        """Bring one file to the target state: exactly one ``.. _id:`` label
        directly above every heading except the page title, no labels anywhere
        else, ids unique within the file, and -- when ``reference`` is given --
        identical to the English ids.

        Returns a dict with the resulting ``desired`` ids, the issues to
        report, the ``old id -> new id`` renames links elsewhere need, the set
        of final ids, and whether the file was rewritten.
        """
        text, lines, headings, label_lines, attached = self._file_anchor_model(path, rel_path, lang)
        result: Dict[str, object] = {
            "headings": headings,
            "desired": [],
            "issues": [],
            "renames": {},
            "final_ids": set(),
            "changed": False,
        }
        issues: List[Issue] = result["issues"]  # type: ignore[assignment]
        renames: Dict[str, str] = result["renames"]  # type: ignore[assignment]
        can_fix = self.fix and path.suffix.lower() in (".md", ".rst")
        fixed_status = "auto_fixed" if can_fix else "auto_fixable"

        if not headings:
            for idx in sorted(label_lines):
                issues.append(Issue(
                    kind="orphan-rst-anchor",
                    file=self._tag_rel_path(rel_path, lang),
                    line=idx + 1,
                    message=(
                        f"Anchor '{label_lines[idx]}' sits in a file with no heading; "
                        "removed because anchor ids are only ever attached to headings."
                    ),
                    severity="warning",
                    details={"anchor_id": label_lines[idx], "reason": "no-heading-in-file",
                             "status": fixed_status},
                ))
            if label_lines and can_fix:
                new_lines = self._rewrite_anchor_labels(lines, headings, label_lines, [])
                if new_lines != lines:
                    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                    result["changed"] = True
            return result

        desired, collisions = self._desired_anchor_ids(headings, attached, label_lines, lang, reference)
        result["desired"] = desired
        final_ids = {str(anchor) for anchor in desired if anchor}
        result["final_ids"] = final_ids

        attached_label_indices: Set[int] = set()
        for h_idx, heading in enumerate(headings):
            label_indices = attached.get(h_idx, [])
            attached_label_indices.update(label_indices)
            attached_ids = [label_lines[i] for i in label_indices]
            want = desired[h_idx]

            if h_idx == 0:
                # Page title: its labels are surplus and get removed.
                for label_idx in label_indices:
                    issues.append(Issue(
                        kind="orphan-rst-anchor",
                        file=self._tag_rel_path(rel_path, lang),
                        line=label_idx + 1,
                        message=(
                            f"Anchor '{label_lines[label_idx]}' precedes the page title; "
                            "removed because the page title is addressed by the page path, "
                            "not by an anchor id."
                        ),
                        severity="warning",
                        details={"anchor_id": label_lines[label_idx], "reason": "page-title",
                                 "status": fixed_status},
                    ))
                continue

            if want is None:
                if attached_ids:
                    for label_idx in label_indices:
                        issues.append(Issue(
                            kind="orphan-rst-anchor",
                            file=self._tag_rel_path(rel_path, lang),
                            line=label_idx + 1,
                            message=(
                                f"Anchor '{label_lines[label_idx]}' is not a valid id "
                                "(ids may only contain lowercase ASCII letters, digits and "
                                "hyphens) and no replacement could be derived from the "
                                f"heading '{heading.text}'; removed."
                            ),
                            severity="warning",
                            details={"anchor_id": label_lines[label_idx], "reason": "unusable-id",
                                     "heading": heading.text, "status": fixed_status},
                        ))
                else:
                    issues.append(Issue(
                        kind="missing-rst-anchor",
                        file=self._tag_rel_path(rel_path, lang),
                        line=heading.line_no,
                        message=(
                            f"Heading '{heading.text}' has no anchor id and none could be "
                            "derived from its text; add an English counterpart heading or "
                            "an explicit .. _label: by hand."
                        ),
                        severity="warning",
                        details={"heading": heading.text, "status": "needs_manual_fix"},
                    ))
                continue

            surplus = label_indices[1:]
            for label_idx in surplus:
                issues.append(Issue(
                    kind="duplicate-anchor",
                    file=self._tag_rel_path(rel_path, lang),
                    line=label_idx + 1,
                    message=(
                        f"Heading '{heading.text}' had {len(label_indices)} anchor ids stacked "
                        f"above it; '{label_lines[label_idx]}' removed, '{want}' kept."
                    ),
                    severity="warning",
                    details={"removed_anchor": label_lines[label_idx], "kept_anchor": want,
                             "heading": heading.text, "status": fixed_status},
                ))

            current = attached_ids[0] if attached_ids else None
            if current is None:
                issues.append(Issue(
                    kind="missing-rst-anchor",
                    file=self._tag_rel_path(rel_path, lang),
                    line=heading.line_no,
                    message=f"Heading '{heading.text}' has no explicit anchor id; inserted '{want}'.",
                    severity="warning",
                    details={"heading": heading.text, "label_id": want, "status": fixed_status},
                ))
            elif current != want:
                if h_idx in collisions:
                    reason = "duplicate-id"
                elif reference:
                    reason = "en-reference"
                else:
                    reason = "invalid-or-stale-id"
                kind = "duplicate-anchor" if reason == "duplicate-id" else "anchor-alignment"
                issues.append(Issue(
                    kind=kind,
                    file=self._tag_rel_path(rel_path, lang),
                    line=heading.line_no,
                    message=(
                        f"Anchor id of heading '{heading.text}' changed from "
                        f"'{current}' to '{want}' ({reason})."
                    ),
                    severity="warning",
                    details={"heading": heading.text, "old_anchor": current,
                             "new_anchor": want, "reason": reason, "status": fixed_status},
                ))

            # A link elsewhere may point at an id this heading is giving up.
            # Only record the rename when the old id disappears from the file
            # entirely: after a corrupted run an id is often merely attached to
            # the wrong heading and still exists on its correct one, and
            # rewriting links in that case would break working links.
            for old_id in attached_ids:
                if old_id != want and old_id not in final_ids:
                    renames[old_id] = want

        for idx in sorted(set(label_lines) - attached_label_indices):
            anchor_id = label_lines[idx]
            still_exists = anchor_id in final_ids
            issues.append(Issue(
                kind="orphan-rst-anchor",
                file=self._tag_rel_path(rel_path, lang),
                line=idx + 1,
                message=(
                    f"Anchor '{anchor_id}' is not attached to any heading; removed."
                    + ("" if still_exists else " No heading in this file carries that id anymore,"
                                               " so links pointing at it need a manual check.")
                ),
                severity="warning",
                details={
                    "anchor_id": anchor_id,
                    "reason": "unattached",
                    "status": fixed_status if still_exists else "needs_manual_review",
                },
            ))

        if can_fix:
            new_lines = self._rewrite_anchor_labels(lines, headings, label_lines, desired)
            if new_lines != lines:
                path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                result["changed"] = True
        return result

    def _heading_structure_diff(self, en_headings: List[Heading], zh_headings: List[Heading]):
        """Return the headings that have no counterpart on the other side,
        computed as an LCS over (level, normalized text) so an added or
        removed section shows up as a local gap instead of shifting
        everything after it."""
        def _key(heading: Heading):
            return (heading.level, heading.text.strip().lower())

        a_keys = [_key(h) for h in en_headings]
        b_keys = [_key(h) for h in zh_headings]
        m, n = len(a_keys), len(b_keys)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        for i in range(m):
            for j in range(n):
                if a_keys[i] == b_keys[j]:
                    dp[i + 1][j + 1] = dp[i][j] + 1
                else:
                    dp[i + 1][j + 1] = max(dp[i][j + 1], dp[i + 1][j])
        pairs: Set[Tuple[int, int]] = set()
        i2, j2 = m, n
        while i2 > 0 and j2 > 0:
            if a_keys[i2 - 1] == b_keys[j2 - 1]:
                pairs.add((i2 - 1, j2 - 1))
                i2 -= 1
                j2 -= 1
            elif dp[i2 - 1][j2] >= dp[i2][j2 - 1]:
                i2 -= 1
            else:
                j2 -= 1
        only_en = [en_headings[i] for i in range(m) if not any(p[0] == i for p in pairs)]
        only_zh = [zh_headings[j] for j in range(n) if not any(p[1] == j for p in pairs)]
        return only_en, only_zh

    def _normalize_anchors(self, en_files: List[Path], zh_files: List[Path]) -> Dict[str, object]:
        """Single entry point for all anchor work.

        The English repo is normalized first and its ids become the reference
        for the Chinese repo, so a heading pair ends up with one identical id
        on both sides. A Chinese file only adopts the English ids when both
        files have the same heading structure (same count, same levels in the
        same order); positional pairing is not trustworthy otherwise, and
        guessing there is exactly what previously attached ids to the wrong
        headings. Such files are reported for manual alignment instead.
        """
        issues: List[Issue] = []
        en_desired: Dict[str, List[Optional[str]]] = {}
        en_headings: Dict[str, List[Heading]] = {}
        en_renames: Dict[Tuple[str, str], str] = {}
        zh_renames: Dict[Tuple[str, str], str] = {}
        en_changed: Set[str] = set()
        zh_changed: Set[str] = set()
        en_new: Dict[str, Set[str]] = {}
        zh_new: Dict[str, Set[str]] = {}

        en_by_rel: Dict[str, Path] = {}
        zh_by_rel: Dict[str, Path] = {}
        if self.en_root:
            en_by_rel = {path.relative_to(self.en_root).as_posix(): path for path in en_files}
        if self.zh_root:
            zh_by_rel = {path.relative_to(self.zh_root).as_posix(): path for path in zh_files}

        for rel_path in sorted(en_by_rel):
            outcome = self._normalize_file_anchors(en_by_rel[rel_path], rel_path, "en")
            issues.extend(outcome["issues"])  # type: ignore[arg-type]
            en_desired[rel_path] = outcome["desired"]  # type: ignore[assignment]
            en_headings[rel_path] = outcome["headings"]  # type: ignore[assignment]
            en_new[rel_path] = outcome["final_ids"]  # type: ignore[assignment]
            for old_id, new_id in outcome["renames"].items():  # type: ignore[union-attr]
                en_renames[(rel_path, old_id)] = new_id
            if outcome["changed"] or outcome["renames"]:
                en_changed.add(rel_path)

        if self.en_root and self.zh_root:
            for rel_path in sorted(set(en_by_rel) - set(zh_by_rel)):
                issues.append(Issue(
                    kind="missing-translation",
                    file=self._tag_rel_path(rel_path, "en"),
                    line=None,
                    message=f"File exists in the English repo but has no Chinese counterpart at the same path: {rel_path}",
                    severity="info",
                    details={"missing_in": "zh", "status": "needs_manual_review"},
                ))
            for rel_path in sorted(set(zh_by_rel) - set(en_by_rel)):
                issues.append(Issue(
                    kind="missing-translation",
                    file=self._tag_rel_path(rel_path, "zh"),
                    line=None,
                    message=f"File exists in the Chinese repo but has no English counterpart at the same path: {rel_path}",
                    severity="info",
                    details={"missing_in": "en", "status": "needs_manual_review"},
                ))

        for rel_path in sorted(zh_by_rel):
            zh_path = zh_by_rel[rel_path]
            reference: Optional[List[Optional[str]]] = None
            if rel_path in en_desired:
                zh_heads = self._parse_headings(
                    zh_path.read_text(encoding="utf-8", errors="ignore"), rel_path, "zh")
                en_heads = en_headings[rel_path]
                if [h.level for h in en_heads] == [h.level for h in zh_heads]:
                    reference = en_desired[rel_path]
                else:
                    only_en, only_zh = self._heading_structure_diff(en_heads, zh_heads)
                    issues.append(Issue(
                        kind="heading-count-mismatch",
                        file=self._tag_rel_path(rel_path, "en/zh"),
                        line=None,
                        message=(
                            f"English has {len(en_heads)} headings, Chinese has {len(zh_heads)}, "
                            "or their levels differ, so Chinese anchor ids were left as they are "
                            "instead of being aligned to the English ones. "
                            f"Only in EN: {[h.text for h in only_en]}. "
                            f"Only in ZH: {[h.text for h in only_zh]}."
                        ),
                        severity="warning",
                        details={
                            "en_count": len(en_heads),
                            "zh_count": len(zh_heads),
                            "only_in_en": [f"L{h.line_no} H{h.level}: {h.text}" for h in only_en],
                            "only_in_zh": [f"L{h.line_no} H{h.level}: {h.text}" for h in only_zh],
                            "status": "needs_manual_review",
                        },
                    ))
            outcome = self._normalize_file_anchors(zh_path, rel_path, "zh", reference)
            issues.extend(outcome["issues"])  # type: ignore[arg-type]
            zh_new[rel_path] = outcome["final_ids"]  # type: ignore[assignment]
            for old_id, new_id in outcome["renames"].items():  # type: ignore[union-attr]
                zh_renames[(rel_path, old_id)] = new_id
            if outcome["changed"] or outcome["renames"]:
                zh_changed.add(rel_path)

        return {
            "issues": issues,
            "en_renames": en_renames,
            "zh_renames": zh_renames,
            "en_changed_files": en_changed,
            "zh_changed_files": zh_changed,
            "en_new_anchors": en_new,
            "zh_new_anchors": zh_new,
        }
    # @AI_GENERATED: end

    def _parse_local_links(self, text: str, rel_path: str) -> List[Dict[str, object]]:
        links = []
        # @AI_GENERATED: Kiro v1.0
        # Exclude image syntax so local assets are handled only by
        # _check_media_targets rather than reported as generic broken links.
        markdown_link_re = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
        for match in markdown_link_re.finditer(text):
            link_text = match.group(1).strip()
            raw_target = match.group(2)
            target = raw_target.strip()
            if target.startswith(("mailto:", "tel:")):
                continue
            offset = len(raw_target) - len(raw_target.lstrip())
            start = match.start(2) + offset
            end = start + len(target)
            kind = "markdown-anchor" if target.startswith("#") else "markdown"
            links.append({"target": target, "line": text[:match.start()].count("\n") + 1, "kind": kind, "start": start, "end": end, "text": link_text, "match_start": match.start(), "match_end": match.end()})
        # @AI_GENERATED: end

        rst_link_re = re.compile(r"`([^`]+)\s+<([^>]+)>`_", re.M)
        for match in rst_link_re.finditer(text):
            link_text = match.group(1).strip()
            raw_target = match.group(2)
            target = raw_target.strip()
            if target.startswith(("mailto:", "tel:")):
                continue
            offset = len(raw_target) - len(raw_target.lstrip())
            start = match.start(2) + offset
            end = start + len(target)
            links.append({"target": target, "line": text[:match.start()].count("\n") + 1, "kind": "rst", "start": start, "end": end, "text": link_text})

        return links

    # @AI_GENERATED: Kiro v1.0
    def _rst_directive_content_spans(self, text: str) -> List[Tuple[int, int]]:
        """Return ranges occupied by ``note`` and ``list-table`` directives."""
        lines = text.splitlines(keepends=True)
        starts: List[int] = []
        offset = 0
        for line in lines:
            starts.append(offset)
            offset += len(line)

        directive_re = re.compile(
            r"^(?P<indent>[ \t]*)\.\.\s+(?:note|list-table)::", re.I
        )
        spans: List[Tuple[int, int]] = []
        index = 0
        while index < len(lines):
            match = directive_re.match(lines[index])
            if not match:
                index += 1
                continue

            directive_indent = len(match.group("indent"))
            end_index = index + 1
            while end_index < len(lines):
                line = lines[end_index]
                if line.strip() and len(line) - len(line.lstrip(" \t")) <= directive_indent:
                    break
                end_index += 1

            end_offset = starts[end_index] if end_index < len(lines) else len(text)
            spans.append((starts[index], end_offset))
            index = end_index
        return spans

    def _protected_content_spans(self, text: str) -> List[Tuple[int, int]]:
        """Return HTML-comment and fenced-code ranges that must not be rewritten."""
        spans = [(match.start(), match.end()) for match in re.finditer(r"<!--.*?(?:-->|$)", text, re.S)]
        lines = text.splitlines(keepends=True)
        offset = 0
        fence_start: Optional[int] = None
        fence_marker: Optional[str] = None
        for line in lines:
            match = re.match(r"^\s*(`{3,}|~{3,})", line)
            if fence_start is None:
                if match:
                    fence_start = offset
                    fence_marker = match.group(1)[0]
            elif match and match.group(1)[0] == fence_marker:
                spans.append((fence_start, offset + len(line)))
                fence_start = None
                fence_marker = None
            offset += len(line)
        if fence_start is not None:
            spans.append((fence_start, len(text)))
        return spans

    @staticmethod
    def _offset_is_in_spans(offset: int, spans: List[Tuple[int, int]]) -> bool:
        return any(start <= offset < end for start, end in spans)
    # @AI_GENERATED: end

    def _apply_text_fixes(self, path: Path, original_text: str, fixes: List[Tuple[int, int, str]]) -> None:
        new_text = original_text
        for start, end, replacement in sorted(fixes, key=lambda item: item[0], reverse=True):
            new_text = new_text[:start] + replacement + new_text[end:]
        if new_text != original_text:
            path.write_text(new_text, encoding="utf-8")

    def _split_target_parts(self, target_path: str) -> List[str]:
        normalized = target_path.replace("\\", "/")
        for suffix in (".html", ".md", ".rst"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return [part for part in normalized.split("/") if part not in ("", ".", "..")]

    def _find_fix_candidates(self, root: Path, files: List[Path], target_path: str) -> List[Path]:
        parts = self._split_target_parts(target_path)
        if not parts:
            return []
        stem = parts[-1].lower()
        by_stem = [f for f in files if f.stem.lower() == stem]
        if len(by_stem) == 1:
            return by_stem
        # When stem has no direct match, also try it as a directory name with index.md inside
        # e.g. link to "overview.html" -> look for "overview/index.md"
        if len(by_stem) == 0:
            by_idx = [f for f in files if f.stem.lower() == "index"]
            for depth in range(len(parts) + 1, 0, -1):
                want = tuple(p.lower() for p in parts[-(depth - 1):]) + ("index",) if depth > 1 else ("index",)
                narrowed2 = []
                for c in by_idx:
                    rel_parts = tuple(p.lower() for p in c.with_suffix("").parts)
                    if len(rel_parts) >= depth and rel_parts[-depth:] == want:
                        narrowed2.append(c)
                if len(narrowed2) == 1:
                    return narrowed2
            return []

        for depth in range(len(parts), 0, -1):
            wanted_tail = tuple(p.lower() for p in parts[-depth:])
            narrowed = []
            for candidate in by_stem:
                rel_no_suffix = candidate.relative_to(root).with_suffix("").as_posix().lower()
                rel_parts = tuple(rel_no_suffix.split("/")[-depth:])
                if rel_parts == wanted_tail:
                    narrowed.append(candidate)
            if len(narrowed) == 1:
                return narrowed
        # #3: index-segment fallback: link to dir/ or dir.html -> dir/index.md
        if parts:
            by_idx = [f for f in files if f.stem.lower() == "index"]
            for depth in range(len(parts) + 1, 0, -1):
                if depth > 1:
                    want_tail = tuple(p.lower() for p in parts[-(depth - 1):]) + ("index",)
                else:
                    want_tail = ("index",)
                narrowed_idx = []
                for candidate in by_idx:
                    rel_ns = candidate.relative_to(root).with_suffix("").as_posix().lower()
                    rel_p = tuple(rel_ns.split("/")[-depth:])
                    if rel_p == want_tail:
                        narrowed_idx.append(candidate)
                if len(narrowed_idx) == 1:
                    return narrowed_idx
        return by_stem

    def _shared_dir_depth(self, source_path: Path, root: Path, candidate: Path) -> int:
        # Shared leading directory segment count between source_path and candidate.
        a_parts = source_path.relative_to(root).parent.parts
        b_parts = candidate.relative_to(root).parent.parts
        count = 0
        for x, y in zip(a_parts, b_parts):
            if x.lower() == y.lower():
                count += 1
            else:
                break
        return count

    def _doc_rel_html(self, root: Path, candidate: Path) -> str:
        rel = candidate.relative_to(root).as_posix()
        if rel.endswith(".md"):
            rel = rel[:-3] + ".html"
        elif rel.endswith(".rst"):
            rel = rel[:-4] + ".html"
        return rel

    def _compute_relative_link(self, source_path: Path, candidate: Path, anchor: Optional[str]) -> str:
        rel = Path(os.path.relpath(candidate, start=source_path.parent)).as_posix()
        if rel.endswith(".md"):
            rel = rel[:-3] + ".html"
        elif rel.endswith(".rst"):
            rel = rel[:-4] + ".html"
        if anchor:
            rel = f"{rel}#{anchor}"
        return rel

    def _find_media_fix_candidates(self, media_files: List[Path], target_filename: str) -> List[Path]:
        return [m for m in media_files if m.name.lower() == target_filename.lower()]

    def _check_media_targets(self, source_path: Path, root: Path, rel_path: str, text: str, media_files: List[Path], lang: str = "en") -> Tuple[List[Issue], List[Tuple[int, int, str]]]:
        issues: List[Issue] = []
        fix_spans: List[Tuple[int, int, str]] = []

        candidates: List[Tuple[int, str, Optional[Tuple[int, int]]]] = []

        # @AI_GENERATED: Kiro v1.0
        def _is_in_html_comment(offset: int) -> bool:
            """Ignore inactive Markdown/RST syntax inside ``<!-- ... -->``."""
            return text.rfind("<!--", 0, offset) > text.rfind("-->", 0, offset)
        # @AI_GENERATED: end

        for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
            line = text[:match.start()].count("\n") + 1
            raw_target = match.group(1)
            target = raw_target.strip()
            offset = len(raw_target) - len(raw_target.lstrip())
            start = match.start(1) + offset
            end = start + len(target)
            candidates.append((line, target, (start, end)))

        for match in re.finditer(r"^[ \t]*\.\.\s+(?:image|figure)::\s+(\S+)", text, re.M):
            line = text[:match.start()].count("\n") + 1
            target = match.group(1).strip()
            candidates.append((line, target, (match.start(1), match.end(1))))

        for match in re.finditer(r"^[ \t]*\.\.\s+\|[^|]+\|\s+image::\s+(\S+)", text, re.M):
            line = text[:match.start()].count("\n") + 1
            target = match.group(1).strip()
            candidates.append((line, target, (match.start(1), match.end(1))))

        for line, target, span in candidates:
            # @AI_GENERATED: Kiro v1.0
            # A commented-out image is intentionally absent from Sphinx's
            # HTML output and must not be reported as a missing build asset.
            if span and _is_in_html_comment(span[0]):
                continue
            # @AI_GENERATED: end
            if target.startswith(("http://", "https://", "mailto:", "tel:")):
                continue

            if target.startswith("/"):
                resolved = (root / target.lstrip("/")).resolve()
            else:
                resolved = (source_path.parent / target).resolve()

            if not _is_existing_file(resolved):
                details: Dict[str, object] = {"target": target}
                fix_span = None
                if not target.startswith("/"):
                    media_candidates = self._find_media_fix_candidates(media_files, Path(target).name)
                    if len(media_candidates) == 1:
                        suggested = Path(os.path.relpath(media_candidates[0], start=source_path.parent)).as_posix()
                        details["suggested_fix"] = suggested
                        if self.fix and span:
                            fix_span = (span[0], span[1], suggested)
                            details["status"] = "auto_fixed"
                        else:
                            details["status"] = "auto_fixable"
                    elif len(media_candidates) > 1:
                        # #6: case-insensitive name match (Windows dev -> Linux deploy)
                        ci_only = [m for m in media_candidates
                                   if m.name.lower() == Path(target).name.lower()]
                        if len(ci_only) == 1:
                            suggested = Path(os.path.relpath(ci_only[0], start=source_path.parent)).as_posix()
                            details["suggested_fix"] = suggested
                            details["note"] = "Case-insensitive filename match; corrected to actual on-disk filename."
                            if self.fix and span:
                                fix_span = (span[0], span[1], suggested)
                                details["status"] = "auto_fixed"
                            else:
                                details["status"] = "auto_fixable"
                        else:
                            details["candidates"] = [Path(os.path.relpath(m, start=root)).as_posix() for m in media_candidates]
                            details["status"] = "needs_manual_fix"
                            details["note"] = "Multiple media files share this filename; pick the correct one manually."
                    else:
                        details["status"] = "needs_manual_fix"
                        details["note"] = "File not found anywhere in the repo; the image/media asset is missing."
                else:
                    details["status"] = "needs_manual_review"

                issues.append(Issue(
                    kind="media",
                    file=self._tag_rel_path(rel_path, lang),
                    line=line,
                    message=f"Missing local media file: {target}",
                    severity="error",
                    details=details,
                ))
                if fix_span:
                    fix_spans.append(fix_span)

            if self.build_html:
                html_path = self._resolve_build_html_path(source_path, root, target, rel_path)
                if html_path and not _is_existing_file(html_path):
                    issues.append(Issue(
                        kind="media-build",
                        file=self._tag_rel_path(rel_path, lang),
                        line=line,
                        message=f"Image target is not present in built HTML output: {target}",
                        severity="warning",
                        details={"target": target, "expected_build_path": str(html_path)},
                    ))

        return issues, fix_spans

    def _resolve_build_html_path(self, source_path: Path, root: Path, target: str, rel_path: str) -> Optional[Path]:
        # @AI_GENERATED: Kiro v1.0
        """Return the Sphinx-managed copy of a local image, when available.

        Sphinx copies image assets to ``_images`` rather than preserving the
        source document's media directory. Checking ``_build/html/<target>``
        therefore reports every correctly rendered image as missing.
        """
        if not self.build_html:
            return None

        build_root = self.build_dir / root.name if self.build_dir else root / "_build" / "html"
        images_root = build_root / "_images"
        if not images_root.exists():
            return None

        if target.startswith("/"):
            source_target = (root / target.lstrip("/")).resolve()
        else:
            source_target = (source_path.parent / target).resolve()
        if not _is_existing_file(source_target):
            # The source-media check reports this condition with the actual
            # document-relative target; do not duplicate it as a build issue.
            return None

        expected = images_root / source_target.name
        if _is_existing_file(expected):
            return expected

        # Sphinx appends a numeric suffix when files with the same name collide.
        # Presence of a matching emitted name confirms the source image made it
        # into the HTML build even when it could not keep its original name.
        if list(images_root.glob(f"{source_target.stem}*{source_target.suffix}")):
            return images_root
        return expected
        # @AI_GENERATED: end

    # -- link classification & checks ------------------------------------

    def _classify_absolute_link(self, target_path: str) -> Dict[str, object]:
        parsed = urlparse(target_path)
        has_scheme = bool(parsed.scheme)
        path = parsed.path if has_scheme else target_path

        m = DOCS_PATH_RE.match(path)
        if m:
            reponame, lang_seg, version_seg, remainder = m.groups()
            prefix = path[: len(path) - len(remainder)]
            same_repo = bool(self.repo_name) and reponame.lower() == self.repo_name.lower()
            return {
                "type": "same-repo" if same_repo else "cross-repo",
                "reponame": reponame,
                "lang_seg": lang_seg,
                "version_seg": version_seg,
                "remainder": remainder,
                "prefix": prefix,
                "has_scheme": has_scheme,
            }

        for known_prefix in KNOWN_PRODUCT_PREFIXES:
            if path.startswith(f"/{known_prefix}/"):
                m2 = PRODUCT_PATH_RE.match(path)
                lang_seg = m2.group(2) if m2 else None
                version_seg = m2.group(3) if m2 else None
                remainder = m2.group(4) if m2 else ""
                prefix = path[: len(path) - len(remainder)] if m2 else path
                return {
                    "type": "cross-repo",
                    "reponame": known_prefix,
                    "lang_seg": lang_seg,
                    "version_seg": version_seg,
                    "remainder": remainder,
                    "prefix": prefix,
                    "has_scheme": has_scheme,
                }

        if has_scheme:
            return {"type": "third-party", "reponame": None, "lang_seg": None, "version_seg": None, "remainder": None, "prefix": None, "has_scheme": True}

        # Leading "/" that matches no known doc-link shape and has no
        # scheme -- not confidently local, not confidently third-party.
        return {"type": "unknown-root-relative", "reponame": None, "lang_seg": None, "version_seg": None, "remainder": None, "prefix": None, "has_scheme": False}

    def _check_url_reachable(self, url: str) -> Tuple[bool, str]:
        if url in self._url_check_cache:
            return self._url_check_cache[url]
        result = self._check_url_reachable_uncached(url)
        self._url_check_cache[url] = result
        return result

    # @AI_GENERATED: Kiro v1.0
    # A dropped/reset connection during the TLS handshake or the request
    # itself (e.g. Windows "[WinError 10054] An existing connection was
    # forcibly closed by the remote host") is a one-off network blip, not
    # proof the target is actually down -- a real browser's own retry/
    # reconnect logic is exactly why the same link opens fine there. Only
    # these transient-looking error shapes get retried; a definitive
    # response (HTTP 4xx/5xx, a real redirect loop, DNS failure, ...) is
    # trusted the first time.
    _TRANSIENT_ERROR_MARKERS = (
        "10054", "connection reset", "connection aborted", "connection refused",
        "forcibly closed", "timed out", "temporarily unavailable",
    )

    @classmethod
    def _is_transient_network_error(cls, error_text: str) -> bool:
        lowered = error_text.lower()
        return any(marker in lowered for marker in cls._TRANSIENT_ERROR_MARKERS)
    # @AI_GENERATED: end

    def _check_url_reachable_uncached(self, url: str) -> Tuple[bool, str]:
        # @AI_GENERATED: Kiro v1.0
        """Navigate to *url* like a browser and report the final response.

        A browser opens a documentation link with GET and automatically
        follows redirects; it does not preflight the page with HEAD. Several
        documentation endpoints reset HEAD connections while successfully
        redirecting GET requests to the current versioned page. Fragments are
        client-side only, so remove them before making the request.
        """
        parsed = urlparse(url)
        request_url = parsed._replace(fragment="").geturl()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        def request_with_context(context: Optional[ssl.SSLContext]) -> Tuple[bool, str]:
            # HTTPRedirectHandler is installed by default and follows 301,
            # 302, 303, 307, and 308 redirects, matching browser navigation.
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
            request = urllib.request.Request(request_url, method="GET", headers=headers)
            try:
                with opener.open(request, timeout=URL_CHECK_TIMEOUT) as response:
                    status = getattr(response, "status", 200)
                    final_url = response.geturl()
                    detail = str(status)
                    if final_url != request_url:
                        detail = f"{status} (redirected to {final_url})"
                    return status < 400, detail
            except urllib.error.HTTPError as exc:
                # A redirect only counts as reachable after the redirect
                # handler has completed it. An HTTPError here therefore
                # represents a failed browser-style navigation, including a
                # redirect loop or an invalid redirect destination.
                return False, f"HTTP {exc.code}"

        def attempt_once() -> Tuple[bool, str]:
            try:
                return request_with_context(None)
            except (ssl.SSLError, urllib.error.URLError) as exc:
                error_text = str(exc)
                if "SSL" not in error_text.upper() and "CERTIFICATE" not in error_text.upper():
                    return False, error_text

                # Keep the prior compatibility fallback for internal servers
                # with incomplete certificate chains, while still using
                # browser-style GET navigation and redirect handling.
                insecure_context = ssl.create_default_context()
                insecure_context.check_hostname = False
                insecure_context.verify_mode = ssl.CERT_NONE
                try:
                    ok, detail = request_with_context(insecure_context)
                    return ok, f"{detail} (SSL verification skipped)"
                except (ssl.SSLError, urllib.error.URLError, TimeoutError) as retry_exc:
                    # The retry uses an alternate SSL context, but it must
                    # retain the same non-fatal behavior for slow or
                    # unreachable endpoints as the primary request.
                    return False, f"SSL retry error: {retry_exc}"
            except Exception as exc:  # network errors, timeouts, bad URLs, etc.
                return False, str(exc)

        # @AI_GENERATED: Kiro v1.0
        # Retry only on transient-looking failures; a clean success or a
        # definitive failure (HTTP 404, a genuine unresolvable host, ...) is
        # returned immediately on the first attempt.
        last_ok, last_detail = attempt_once()
        attempt = 1
        while not last_ok and attempt < URL_CHECK_RETRIES and self._is_transient_network_error(last_detail):
            time.sleep(URL_CHECK_RETRY_DELAY)
            last_ok, last_detail = attempt_once()
            attempt += 1
        return last_ok, last_detail
        # @AI_GENERATED: end

    def _check_absolute_or_external_link(
        self,
        root: Path,
        link: Dict[str, object],
        target: str,
        target_path: str,
        anchor: Optional[str],
        line: int,
        rel_path: str,
        lang: str,
        files: List[Path],
    ) -> Tuple[List[Issue], Optional[Tuple[int, int, str]]]:
        issues: List[Issue] = []
        classification = self._classify_absolute_link(target_path)
        kind = classification["type"]

        if classification.get("version_seg"):
            self._doc_version_observations.append((rel_path, line, str(classification["reponame"]), str(classification["version_seg"]), target, lang))

        if kind == "third-party":
            if self.check_external:
                ok, detail = self._check_url_reachable(target_path)
                if not ok:
                    issues.append(Issue(
                        kind="external-link",
                        file=self._tag_rel_path(rel_path, lang),
                        line=line,
                        message=f"Third-party link appears unreachable: {target} ({detail})",
                        severity="error",
                        details={"target": target, "link_type": "third-party", "status": "needs_manual_fix", "check_result": detail},
                    ))
            return issues, None

        if kind == "unknown-root-relative":
            resolved = (root / target_path.lstrip("/")).resolve()
            if not _is_existing_file(resolved):
                issues.append(Issue(
                    kind="link",
                    file=self._tag_rel_path(rel_path, lang),
                    line=line,
                    message=f"Broken local link target: {target}",
                    severity="error",
                    details={
                        "target": target,
                        "status": "needs_manual_review",
                        "note": "Root-relative path that doesn't match a known doc-link pattern (/docs/{repo}/... etc); verify manually.",
                    },
                ))
            return issues, None

        reponame = classification["reponame"]
        remainder = str(classification["remainder"])
        prefix = str(classification["prefix"])

        if kind == "cross-repo":
            # #5: detect if reponame looks like a typo of a known repo (edit-distance <= 1)
            _known_repos = list(KNOWN_PRODUCT_PREFIXES)
            if self.repo_name:
                _known_repos.append(self.repo_name)
            _known_repos += list({r for _, _, r, _, _, _ in self._doc_version_observations})
            def _ed1(a: str, b: str) -> bool:
                if abs(len(a) - len(b)) > 1:
                    return False
                if len(a) == len(b):
                    diffs = sum(x != y for x, y in zip(a, b))
                    if diffs == 1:
                        return True
                    # adjacent transposition: "montior" <-> "monitor"
                    if diffs == 2:
                        idxs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
                        if len(idxs) == 2 and idxs[1] == idxs[0] + 1:
                            return a[idxs[0]] == b[idxs[1]] and a[idxs[1]] == b[idxs[0]]
                    return False
                short, long_ = (a, b) if len(a) < len(b) else (b, a)
                return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))
            _typos = [k for k in _known_repos if k != reponame and _ed1(reponame.lower(), k.lower())]
            _tnote = f" Possible typo of '{_typos[0]}'?" if len(_typos) == 1 else ""
            details: Dict[str, object] = {
                "target": target,
                "link_type": "cross-repo",
                "repo": reponame,
                "status": "needs_manual_review",
                "note": "Cross-repo link to a different product's docs; confirm manually. Never auto-fix or shorten this." + _tnote,
            }
            if _typos:
                details["possible_typo_of"] = _typos[0] if len(_typos) == 1 else _typos
            message = f"Cross-repo documentation link detected (repo: {reponame}){_tnote}: {target}"
            severity = "info"
            if self.check_external:
                full_url = target if classification["has_scheme"] else f"{DEFAULT_DOC_HOST}{target_path}"
                ok, detail = self._check_url_reachable(full_url)
                details["checked_url"] = full_url
                details["check_result"] = detail
                if ok:
                    return issues, None
                details["status"] = "needs_manual_fix"
                details["note"] = "Cross-repo link failed a reachability check; confirm and fix manually."
                message = f"Cross-repo documentation link appears broken (repo: {reponame}): {target} -> {detail}"
                severity = "error"
            issues.append(Issue(kind="external-link", file=self._tag_rel_path(rel_path, lang), line=line, message=message, severity=severity, details=details))
            return issues, None

        # kind == "same-repo": treat like an internal link, but keep the
        # absolute-URL style in any suggested fix -- never shorten it to a
        # relative link.
        normalized_remainder = remainder
        if normalized_remainder.endswith(".md"):
            normalized_remainder = normalized_remainder[:-3] + ".html"
        elif normalized_remainder.endswith(".rst"):
            normalized_remainder = normalized_remainder[:-4] + ".html"

        resolved = (root / normalized_remainder).resolve()
        if not _is_existing_file(resolved) and normalized_remainder.endswith(".html"):
            stem_no_ext = normalized_remainder[:-5]
            for candidate in (root / stem_no_ext, root / (stem_no_ext + ".md"), root / (stem_no_ext + ".rst")):
                candidate = candidate.resolve()
                if _is_existing_file(candidate):
                    resolved = candidate
                    break

        fix_span: Optional[Tuple[int, int, str]] = None

        if not _is_existing_file(resolved):
            details = {"target": target, "link_type": "same-repo-absolute"}
            fix_candidates = self._find_fix_candidates(root, files, remainder)
            if len(fix_candidates) == 1:
                rel_html = self._doc_rel_html(root, fix_candidates[0])
                suggested = f"{prefix}{rel_html}"
                if anchor:
                    suggested = f"{suggested}#{anchor}"
                details["suggested_fix"] = suggested
                if self.fix:
                    fix_span = (int(link["start"]), int(link["end"]), suggested)
                    details["status"] = "auto_fixed"
                else:
                    details["status"] = "auto_fixable"
            elif len(fix_candidates) > 1:
                # #1: rank by shared directory depth; auto-pick if top score is unique
                ranked = sorted(fix_candidates,
                                key=lambda c: self._shared_dir_depth(source_path, root, c),
                                reverse=True)
                if len(ranked) >= 2 and (
                        self._shared_dir_depth(source_path, root, ranked[0]) >
                        self._shared_dir_depth(source_path, root, ranked[1])):
                    suggested = self._compute_relative_link(source_path, ranked[0], anchor)
                    details["suggested_fix"] = suggested
                    details["note"] = "Auto-picked: deepest shared directory with source file."
                    if self.fix:
                        fix_span = (int(link["start"]), int(link["end"]), suggested)
                        details["status"] = "auto_fixed"
                    else:
                        details["status"] = "auto_fixable"
                else:
                    details["candidates"] = [c.relative_to(root).as_posix() for c in fix_candidates]
                    details["status"] = "needs_manual_fix"
                    details["note"] = "Multiple files share this name; pick the correct target manually -- refusing to guess."
            else:
                details["status"] = "needs_manual_fix"
                details["note"] = "No matching file found anywhere in the repo; do not invent a path -- needs manual replacement."

            issues.append(Issue(
                kind="link",
                file=self._tag_rel_path(rel_path, lang),
                line=line,
                message=f"Broken same-repo absolute link target: {target}",
                severity="error",
                details=details,
            ))
            return issues, fix_span

        if anchor:
            target_text = resolved.read_text(encoding="utf-8", errors="ignore")
            headings = self._parse_headings(target_text, resolved.relative_to(root).as_posix(), lang)
            existing_ids = {h.anchor for h in headings if h.anchor}
            if anchor not in existing_ids:
                fuzzy = self._fuzzy_match_anchor(anchor, existing_ids)
                if fuzzy:
                    new_target = target.split("#")[0] + "#" + fuzzy
                    if self.fix:
                        fix_span = (int(link["start"]), int(link["end"]), new_target)
                        issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                            message=f"Anchor '#{anchor}' fuzzy-matched to '#{fuzzy}' -- auto-fixed.",
                            severity="warning",
                            details={"target": target, "suggested_fix": new_target,
                                     "expected_anchor": anchor, "matched_anchor": fuzzy,
                                     "status": "auto_fixed"}))
                    else:
                        issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                            message=f"Anchor '#{anchor}' not found; fuzzy-matched to '#{fuzzy}'.",
                            severity="warning",
                            details={"target": target, "suggested_fix": new_target,
                                     "expected_anchor": anchor, "matched_anchor": fuzzy,
                                     "status": "auto_fixable"}))
                else:
                    issues.append(Issue(
                        kind="anchor",
                        file=self._tag_rel_path(rel_path, lang),
                        line=line,
                        message=f"Missing anchor '#{anchor}' in {resolved.relative_to(root).as_posix()}",
                        severity="warning",
                        details={
                            "target": target,
                            "expected_anchor": anchor,
                            "status": "needs_manual_review",
                            "note": "Anchor left untouched -- may be a Sphinx auto-numbered id (e.g. #id2). Verify manually, ideally against --build-html output.",
                        },
                    ))

        return issues, fix_span

    def _load_html_anchor_map(self, root: Path) -> "Dict[str, Set[str]]":
        # Parse Sphinx HTML build output; return rel_path_no_ext -> set of anchor ids.
        build_root = self.build_dir / root.name if self.build_dir else root / "_build" / "html"
        if not build_root.exists():
            return {}
        try:
            from html.parser import HTMLParser
        except ImportError:
            return {}

        class _AP(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids: Set[str] = set()
            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if "id" in d:
                    self.ids.add(d["id"])

        result: Dict[str, Set[str]] = {}
        for html_file in build_root.rglob("*.html"):
            rel = html_file.relative_to(build_root).with_suffix("").as_posix()
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            parser = _AP()
            parser.feed(content)
            result[rel] = parser.ids
        return result

    # @AI_GENERATED: Kiro v1.0
    def _check_version_consistency(self) -> List[Issue]:
        """Same-repo and cross-repo doc links encode a version segment
        (/docs/{repo}/{lang}/{version}/...). Different repos can legitimately
        use different version schemes, so the majority is computed per
        reponame, not globally. Mismatches are only ever listed -- picking
        the "right" version isn't something this script can know."""
        issues: List[Issue] = []
        by_repo: Dict[str, Dict[str, int]] = {}
        for _, _, reponame, version_seg, _, _ in self._doc_version_observations:
            counts = by_repo.setdefault(reponame, {})
            counts[version_seg] = counts.get(version_seg, 0) + 1

        majority_by_repo = {
            reponame: max(counts, key=lambda v: counts[v])
            for reponame, counts in by_repo.items()
            if len(counts) > 1
        }
        if not majority_by_repo:
            return issues

        for rel_path, line, reponame, version_seg, target, obs_lang in self._doc_version_observations:
            majority = majority_by_repo.get(reponame)
            if majority is None or version_seg == majority:
                continue
            issues.append(Issue(
                kind="inconsistent-doc-version",
                file=self._tag_rel_path(rel_path, obs_lang),
                line=line,
                message=f"Link to repo '{reponame}' uses version segment '{version_seg}', but most links to that repo in this run use '{majority}': {target}",
                severity="warning",
                details={
                    "target": target,
                    "repo": reponame,
                    "used_version": version_seg,
                    "majority_version": majority,
                    "status": "needs_manual_review",
                    "note": "Version segment mismatch is never auto-fixed; confirm which version this link is actually meant to point to.",
                },
            ))
        return issues

    def _resolve_link_target_file(self, source_path: Path, root: Path, target_path: str) -> Optional[Path]:
        """Resolve a link's file part (no anchor) to an existing file inside
        `root`, the same way _check_link_target / _check_absolute_or_external_link
        do, but without generating issues -- used to find what a link
        currently points at when cascading an anchor rename."""
        if not target_path:
            return source_path
        if target_path.startswith(("http://", "https://", "mailto:", "tel:")):
            return None

        if target_path.startswith("/"):
            classification = self._classify_absolute_link(target_path)
            if classification["type"] != "same-repo":
                return None
            normalized = str(classification["remainder"])
            base = root
        else:
            normalized = target_path
            base = source_path.parent

        if normalized.endswith(".md"):
            normalized = normalized[:-3] + ".html"
        elif normalized.endswith(".rst"):
            normalized = normalized[:-4] + ".html"

        candidate = (base / normalized).resolve()
        if _is_existing_file(candidate):
            return candidate

        if normalized.endswith(".html"):
            stem_no_ext = normalized[:-5]
            for alt in (base / stem_no_ext, base / (stem_no_ext + ".md"), base / (stem_no_ext + ".rst")):
                alt = alt.resolve()
                if _is_existing_file(alt):
                    return alt

        return None

    def _apply_anchor_reference_renames(
        self,
        root: Path,
        files: List[Path],
        renames: Dict[Tuple[str, str], str],
        changed_files: Set[str],
        dedup_new_anchors: Optional[Dict[str, Set[str]]] = None,
        lang: str = "zh",
    ) -> List[Issue]:
        """After a heading's RST anchor label is renamed (by alignment or
        deduplication), any link elsewhere in the repo pointing at the old
        anchor id needs updating too, or it silently breaks. Rewrites are
        only ever applied for the exact (file, old-anchor) pairs this run
        computed and (with --fix) wrote; anything shaped like a Sphinx
        auto-numbered id (#id2) pointing at a file whose anchors just changed
        is flagged instead, since this script has no way to know what that id
        actually resolves to without an HTML build."""
        issues: List[Issue] = []
        if not renames and not changed_files:
            return issues

        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            rel_path = path.relative_to(root).as_posix()
            links = self._parse_local_links(text, rel_path)
            fix_spans: List[Tuple[int, int, str]] = []

            for link in links:
                target = str(link["target"])
                if "#" not in target:
                    continue
                target_path, anchor = target.split("#", 1)
                resolved = self._resolve_link_target_file(path, root, target_path)
                if resolved is None:
                    continue
                target_rel = resolved.relative_to(root).as_posix()

                key = (target_rel, anchor)
                if key in renames:
                    new_anchor = renames[key]
                    new_target = f"{target_path}#{new_anchor}"
                    details: Dict[str, object] = {"old_anchor": anchor, "new_anchor": new_anchor, "target_file": target_rel}
                    if self.fix:
                        fix_spans.append((int(link["start"]), int(link["end"]), new_target))
                        details["status"] = "auto_fixed"
                    else:
                        details["status"] = "auto_fixable"
                    issues.append(Issue(
                        kind="anchor-reference-update",
                        file=self._tag_rel_path(rel_path, lang),
                        line=int(link["line"]),
                        message=f"Link anchor '#{anchor}' points to a Chinese heading whose id changed to '#{new_anchor}'.",
                        severity="warning",
                        details=details,
                    ))
                elif ID_PATTERN_RE.match(anchor) and target_rel in changed_files:
                    # #4: resolve via HTML anchor map if --build-html was used
                    html_map = getattr(self, "_html_anchor_map", {}).get(lang, {})
                    target_stem = target_rel.rsplit(".", 1)[0] if "." in target_rel else target_rel
                    html_ids = html_map.get(target_stem, set())
                    new_anchors = {v for (f, a), v in renames.items() if f == target_rel}
                    new_anchors |= (dedup_new_anchors or {}).get(target_rel, set())
                    resolved_html = new_anchors & html_ids
                    if len(resolved_html) == 1:
                        real_anchor = next(iter(resolved_html))
                        new_target = target_path + "#" + real_anchor
                        _det: Dict[str, object] = {
                            "target_file": target_rel, "old_anchor": anchor,
                            "new_anchor": real_anchor, "suggested_fix": new_target,
                        }
                        if self.fix:
                            fix_spans.append((int(link["start"]), int(link["end"]), new_target))
                            _det["status"] = "auto_fixed"
                        else:
                            _det["status"] = "auto_fixable"
                        issues.append(Issue(
                            kind="stale-auto-id-reference", file=self._tag_rel_path(rel_path, lang),
                            line=int(link["line"]),
                            message=f"Auto-id '#{anchor}' in {target_rel} resolved via HTML build to '#{real_anchor}'.",
                            severity="warning", details=_det))
                    else:
                        issues.append(Issue(
                            kind="stale-auto-id-reference",
                            file=self._tag_rel_path(rel_path, lang),
                            line=int(link["line"]),
                            message=f"Link anchor '#{anchor}' looks like a Sphinx auto-numbered id and points at {target_rel}, where a heading anchor is being realigned in this run.",
                            severity="warning",
                            details={
                                "target_file": target_rel,
                                "status": "needs_manual_review",
                                "note": "Auto-numbered ids (#id2, #id3, ...) can shift and cannot be resolved from source text alone; rebuild with --build-html, check the real id in the rendered page, and update this link by hand.",
                            },
                        ))

            if self.fix and fix_spans:
                self._apply_text_fixes(path, text, fix_spans)

        return issues

    def _check_link_target(self, source_path: Path, root: Path, link: Dict[str, object], rel_path: str, lang: str, files: List[Path]) -> Tuple[List[Issue], Optional[Tuple[int, int, str]]]:
        target = str(link["target"])
        line = int(link["line"])
        target_path = target.split("#", 1)[0]
        anchor = target.split("#", 1)[1] if "#" in target else None

        if not target_path:
            target_path = source_path.name

        if target_path.startswith("/") or urlparse(target_path).scheme in ("http", "https"):
            return self._check_absolute_or_external_link(root, link, target, target_path, anchor, line, rel_path, lang, files)

        issues: List[Issue] = []
        fix_span: Optional[Tuple[int, int, str]] = None

        normalized_path = target_path
        if normalized_path.endswith(".md"):
            normalized_path = normalized_path[:-3] + ".html"
        elif normalized_path.endswith(".rst"):
            normalized_path = normalized_path[:-4] + ".html"

        base_path = source_path.parent
        resolved = (base_path / normalized_path).resolve()

        if not _is_existing_file(resolved) and normalized_path.endswith(".html"):
            stem_no_ext = normalized_path[:-5]
            candidate_sources = [
                (base_path / stem_no_ext).resolve(),
                (base_path / (stem_no_ext + ".md")).resolve(),
                (base_path / (stem_no_ext + ".rst")).resolve(),
            ]
            for candidate in candidate_sources:
                if _is_existing_file(candidate):
                    resolved = candidate
                    break

        if not _is_existing_file(resolved):
            details: Dict[str, object] = {"target": target}
            fix_candidates = self._find_fix_candidates(root, files, target_path)
            if len(fix_candidates) == 1:
                suggested = self._compute_relative_link(source_path, fix_candidates[0], anchor)
                details["suggested_fix"] = suggested
                if self.fix:
                    fix_span = (int(link["start"]), int(link["end"]), suggested)
                    details["status"] = "auto_fixed"
                else:
                    details["status"] = "auto_fixable"
            elif len(fix_candidates) > 1:
                # #1: rank by shared directory depth; auto-pick if top score is unique
                ranked = sorted(fix_candidates,
                                key=lambda c: self._shared_dir_depth(source_path, root, c),
                                reverse=True)
                if len(ranked) >= 2 and (
                        self._shared_dir_depth(source_path, root, ranked[0]) >
                        self._shared_dir_depth(source_path, root, ranked[1])):
                    suggested = self._compute_relative_link(source_path, ranked[0], anchor)
                    details["suggested_fix"] = suggested
                    details["note"] = "Auto-picked: deepest shared directory with source file."
                    if self.fix:
                        fix_span = (int(link["start"]), int(link["end"]), suggested)
                        details["status"] = "auto_fixed"
                    else:
                        details["status"] = "auto_fixable"
                else:
                    details["candidates"] = [c.relative_to(root).as_posix() for c in fix_candidates]
                    details["status"] = "needs_manual_fix"
                    details["note"] = "Multiple files share this name; pick the correct target manually -- refusing to guess."
            else:
                details["status"] = "needs_manual_fix"
                details["note"] = "No matching file found anywhere in the repo; do not invent a path -- needs manual replacement."

            issues.append(Issue(
                kind="link",
                file=self._tag_rel_path(rel_path, lang),
                line=line,
                message=f"Broken local link target: {target}",
                severity="error",
                details=details,
            ))
            return issues, fix_span

        if anchor:
            target_text = resolved.read_text(encoding="utf-8", errors="ignore")
            headings = self._parse_headings(target_text, resolved.relative_to(root).as_posix(), lang)
            existing_ids = {heading.anchor for heading in headings if heading.anchor}

            # @AI_GENERATED: Kiro v1.0
            # Collect all explicit RST anchor labels from the target file.
            # These are the canonical ids set by .. _label: directives and
            # always take precedence over Sphinx-generated slugs.
            existing_ids |= self._collect_rst_anchors(target_text)

            # When --build-html was used, also consult the HTML anchor map
            # which contains ALL ids present in the rendered output (including
            # Sphinx auto-numbered ids like #id2, RST labels, etc.).
            html_anchor_map = getattr(self, "_html_anchor_map", {}).get(lang, {})
            if html_anchor_map:
                target_rel = resolved.relative_to(root).as_posix()
                # The map keys are rel_path without extension (e.g. "widgets/overview")
                target_stem = target_rel.rsplit(".", 1)[0] if "." in target_rel else target_rel
                html_ids = html_anchor_map.get(target_stem, set())
                existing_ids = existing_ids | html_ids
            # @AI_GENERATED: end

            if anchor not in existing_ids:
                # @AI_GENERATED: Kiro v1.0
                # For #idN anchors (Sphinx auto-numbered), do NOT fuzzy-match
                # because id2 -> id1 is edit-distance 1 but completely wrong.
                # Instead, try to resolve via the link text: find a heading in
                # the target file whose text matches the link text, and use
                # that heading's anchor as the correct replacement.
                link_text = str(link.get("text", "")).strip()
                if ID_PATTERN_RE.match(anchor):
                    resolved_anchor = self._resolve_anchor_from_link_text(link_text, headings, lang)
                    if resolved_anchor:
                        new_target = target.split("#")[0] + "#" + resolved_anchor
                        if self.fix:
                            fix_span = (int(link["start"]), int(link["end"]), new_target)
                            issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                                message=f"Auto-id '#{anchor}' resolved to '#{resolved_anchor}' via link text '{link_text}' -- auto-fixed.",
                                severity="warning",
                                details={"target": target, "suggested_fix": new_target,
                                         "expected_anchor": anchor, "resolved_anchor": resolved_anchor,
                                         "link_text": link_text, "status": "auto_fixed"}))
                        else:
                            issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                                message=f"Auto-id '#{anchor}' resolved to '#{resolved_anchor}' via link text '{link_text}'.",
                                severity="warning",
                                details={"target": target, "suggested_fix": new_target,
                                         "expected_anchor": anchor, "resolved_anchor": resolved_anchor,
                                         "link_text": link_text, "status": "auto_fixable"}))
                    else:
                        issues.append(Issue(
                            kind="anchor",
                            file=self._tag_rel_path(rel_path, lang),
                            line=line,
                            message=f"Missing anchor '#{anchor}' in {resolved.relative_to(root).as_posix()}",
                            severity="warning",
                            details={
                                "target": target,
                                "expected_anchor": anchor,
                                "link_text": link_text,
                                "status": "needs_manual_review",
                                "note": "Sphinx auto-numbered id cannot be resolved from source. Link text did not match any heading in the target file.",
                            },
                        ))
                else:
                    # Non-idN anchors: also try link-text resolution before fuzzy
                    resolved_anchor = self._resolve_anchor_from_link_text(link_text, headings, lang) if link_text else None
                    if resolved_anchor:
                        new_target = target.split("#")[0] + "#" + resolved_anchor
                        if self.fix:
                            fix_span = (int(link["start"]), int(link["end"]), new_target)
                            issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                                message=f"Anchor '#{anchor}' resolved to '#{resolved_anchor}' via link text '{link_text}' -- auto-fixed.",
                                severity="warning",
                                details={"target": target, "suggested_fix": new_target,
                                         "expected_anchor": anchor, "resolved_anchor": resolved_anchor,
                                         "link_text": link_text, "status": "auto_fixed"}))
                        else:
                            issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                                message=f"Anchor '#{anchor}' resolved to '#{resolved_anchor}' via link text '{link_text}'.",
                                severity="warning",
                                details={"target": target, "suggested_fix": new_target,
                                         "expected_anchor": anchor, "resolved_anchor": resolved_anchor,
                                         "link_text": link_text, "status": "auto_fixable"}))
                    else:
                        fuzzy = self._fuzzy_match_anchor(anchor, existing_ids)
                        if fuzzy:
                            new_target = target.split("#")[0] + "#" + fuzzy
                            if self.fix:
                                fix_span = (int(link["start"]), int(link["end"]), new_target)
                                issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                                    message=f"Anchor '#{anchor}' fuzzy-matched to '#{fuzzy}' -- auto-fixed.",
                                    severity="warning",
                                    details={"target": target, "suggested_fix": new_target,
                                             "expected_anchor": anchor, "matched_anchor": fuzzy,
                                             "status": "auto_fixed"}))
                            else:
                                issues.append(Issue(kind="anchor", file=self._tag_rel_path(rel_path, lang), line=line,
                                    message=f"Anchor '#{anchor}' not found; fuzzy-matched to '#{fuzzy}'.",
                                    severity="warning",
                                    details={"target": target, "suggested_fix": new_target,
                                             "expected_anchor": anchor, "matched_anchor": fuzzy,
                                             "status": "auto_fixable"}))
                        else:
                            issues.append(Issue(
                                kind="anchor",
                                file=self._tag_rel_path(rel_path, lang),
                                line=line,
                                message=f"Missing anchor '#{anchor}' in {resolved.relative_to(root).as_posix()}",
                                severity="warning",
                                details={
                                    "target": target,
                                    "expected_anchor": anchor,
                                    "status": "needs_manual_review",
                                    "note": "Anchor left untouched -- it may be a Sphinx auto-numbered id (e.g. #id2) that this heading-based check cannot reconstruct. Verify manually, ideally against --build-html output.",
                                },
                            ))
                # @AI_GENERATED: end

        return issues, fix_span

    def _check_rich_text_patterns(self, text: str, rel_path: str, lang: str) -> List[Issue]:
        issues: List[Issue] = []
        lines = text.splitlines()

        image_hits = []
        for idx, line in enumerate(lines, 1):
            if re.search(r"!\[[^\]]*\]\(([^)]+)\)", line):
                image_hits.append((idx, line.strip()))
            if re.match(r"^\s*\.\.\s+(image|figure)::", line, re.I):
                image_hits.append((idx, line.strip()))

        if image_hits:
            for idx, line in image_hits:
                issues.append(Issue(
                    kind="image",
                    file=self._tag_rel_path(rel_path, lang),
                    line=idx,
                    message="Image syntax detected; verify the local file exists and renders correctly.",
                    severity="warning",
                    details={"snippet": line},
                ))

        # Table rendering problems are intentionally NOT checked here (or
        # anywhere in this static pre-build scan). They are only ever
        # detected from an actual Sphinx HTML build, in
        # _check_table_build_errors, to avoid flagging table-like content
        # that would actually render fine.

        list_hits = []
        for idx, line in enumerate(lines, 1):
            if re.match(r"^\s*([-*+]\s|\d+\.\s)", line):
                list_hits.append((idx, line.strip()))

        if list_hits:
            for idx, line in list_hits:
                issues.append(Issue(
                    kind="list",
                    file=self._tag_rel_path(rel_path, lang),
                    line=idx,
                    message="List or bullet syntax detected; verify indentation and nesting render correctly.",
                    severity="warning",
                    details={"snippet": line},
                ))

        heading_hits = []
        for idx, line in enumerate(lines, 1):
            if re.match(r"^#{1,6}\s+", line):
                heading_hits.append((idx, line.strip()))

        if heading_hits:
            for idx, line in heading_hits:
                issues.append(Issue(
                    kind="heading",
                    file=self._tag_rel_path(rel_path, lang),
                    line=idx,
                    message="Heading syntax detected; verify the title level and anchor output are expected.",
                    severity="info",
                    details={"snippet": line},
                ))

        admonition_hits = []
        for idx, line in enumerate(lines, 1):
            if re.match(r"^\s*\.\.\s+(note|warning|tip|important|attention|caution|danger|error|hint|seealso|admonition)::", line, re.I):
                admonition_hits.append((idx, line.strip()))

        if admonition_hits:
            for idx, line in admonition_hits:
                issues.append(Issue(
                    kind="admonition",
                    file=self._tag_rel_path(rel_path, lang),
                    line=idx,
                    message="Admonition syntax detected; verify the rendered note/warning block is correct.",
                    severity="warning",
                    details={"snippet": line},
                ))

        return issues

    def _filter_actionable_issues(self, issues: List[Issue]) -> List[Issue]:
        actionable_kinds = {
            "media", "media-build", "link", "anchor", "anchor-alignment",
            "external-link", "missing-translation", "heading-count-mismatch",
            "inconsistent-doc-version",
            "anchor-reference-update", "stale-auto-id-reference",
            "table-build-error", "duplicate-anchor",
            "tag-at-start-of-heading",
            "missing-rst-anchor", "orphan-rst-anchor",
        }
        filtered = []
        for issue in issues:
            if issue.kind in actionable_kinds:
                # @AI_GENERATED: Kiro v1.0
                # Skip issues from HIDDEN- files (deprecated/unused files).
                # The file field may have a "[zh] " or "[en] " prefix for
                # table-build-error issues; check for HIDDEN- anywhere in path.
                file_path = issue.file or ""
                if "HIDDEN-" in file_path:
                    continue
                # @AI_GENERATED: end
                filtered.append(issue)
        return filtered

    def _build_anchor_id(self, title: str, lang: str) -> str:
        # @AI_GENERATED: Kiro v1.0
        # Model Sphinx/docutils heading-slug generation as seen in the real
        # HTML build: lowercase the whole heading, turn every run of
        # non-[a-z0-9] characters (spaces, CJK, punctuation, and the < > of a
        # <tag>) into a single hyphen, collapse hyphens, trim. There is NO
        # de-duplication -- a <tag>'s content AND any latin tokens already in
        # the heading text BOTH land in the id, in document order. So
        #   "<step-3-copying-the-page-url> 步骤 3：获取页面 URL"
        # -> "step-3-copying-the-page-url-3-url". Reproducing an English
        # anchor therefore needs interleaved tags (now replaced by explicit
        # .. _label: directives). (lang is accepted for call-site
        # compatibility; en and zh now slugify identically.)
        title = self._strip_inline_markers(title)
        value = title.lower().replace("<", " ").replace(">", " ")
        ascii_value = re.sub(r"[^a-z0-9\s-]", " ", value)
        ascii_value = re.sub(r"[\s-]+", "-", ascii_value.strip()).strip("-")
        if ascii_value or lang != "zh":
            return ascii_value
        # Bug fix: pure-Chinese headings (no ASCII/alnum token survives at
        # all, e.g. "标签管理") used to fall through to "" here, which is
        # not a usable anchor id and doesn't match GUIDE.md's documented
        # convention ("纯中文标题则回退为中文字符 slug" -- a pure-Chinese
        # heading falls back to a Chinese-character slug). An empty string
        # also can never equal a real link's literal Chinese anchor
        # fragment (e.g. "#标签管理"), so anchor-alignment renames and
        # anchor-reference-update lookups silently missed every such
        # heading. Fall back to a slug built from the heading's own
        # Unicode word characters (CJK counts as \w) -- zh-only, so English
        # headings that still contain leftover untranslated Chinese text
        # keep resolving to "" and are skipped as documented above.
        # Pure-CJK heading: no ASCII tokens survive.  Fall back to a slug
        # built from the heading's Unicode word characters (CJK counts as \w)
        # so that every zh heading gets an anchor id even without an English
        # counterpart.  _sanitize_anchor_id will strip CJK back out, so we
        # keep the raw CJK characters here and let the caller decide — but
        # we return something non-empty so the heading is not silently skipped.
        cjk_slug = re.sub(r"\s+", "-", title.strip())
        cjk_slug = re.sub(r"[^\w-]", "", cjk_slug, flags=re.UNICODE)
        cjk_slug = re.sub(r"-+", "-", cjk_slug).strip("-")
        return cjk_slug
        # @AI_GENERATED: end

    # @AI_GENERATED: Kiro v1.0
    # HTML element names that are unsafe as anchor ids: browsers and Sphinx
    # themes can treat elements with these ids specially (scroll offsets,
    # CSS rules, JS queries), causing navigation to land at the wrong place.
    _HTML_RESERVED_ANCHOR_IDS: frozenset = frozenset({
        "header", "footer", "nav", "main", "section", "article",
        "aside", "form", "table", "input",
    })

    def _sanitize_anchor_id(self, slug: str) -> str:
        """Apply safety rules to a generated slug before using it as a label id.

        Rules (applied in order):
        1. Strip any CJK characters (replace with hyphen).
        2. Lowercase, and replace anything that is not a-z / 0-9 with a hyphen,
           so an id can only ever contain ASCII letters, digits and hyphens.
        3. Collapse runs of hyphens; strip leading/trailing hyphens.
        4. If the result exactly matches an HTML reserved element name,
           append ``-section``.

        A leading digit is deliberately left alone: ids such as ``3d-model``
        already exist in the corpus and are linked to, and HTML5 permits them.

        Returns the sanitized slug, or an empty string if nothing usable
        remains after the above transformations.
        """
        if not slug:
            return ""
        # Rule 1: remove CJK that may have leaked through the fallback branch
        slug = self._CJK_RE.sub("-", slug)
        # Rule 2: restrict the character set to a-z, 0-9 and hyphen
        slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())
        # Rule 3: collapse and strip
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        if not slug:
            return ""
        # Rule 4: must not shadow HTML element names
        if slug in self._HTML_RESERVED_ANCHOR_IDS:
            slug = f"{slug}-section"
        return slug
    # @AI_GENERATED: end

    def _apply_heading_update(self, file_path: Path, line_no: int, new_heading: str) -> None:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line_no <= 0 or line_no > len(lines):
            return
        current = lines[line_no - 1]
        md_match = re.match(r"^(#{1,6})\s+(.*)$", current)
        if md_match:
            level = md_match.group(1)
            lines[line_no - 1] = f"{level} {new_heading}"
            file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # @AI_GENERATED: Kiro v1.0
    _LINKTEXT_MIN_RATIO = 0.6
    _LINKTEXT_MIN_MARGIN = 0.1

    def _clean_heading_text(self, text: str) -> str:
        """Normalise a heading / link label for comparison: drop <tag>
        markers and inline-code backticks, collapse whitespace."""
        cleaned = re.sub(r"<[^>]+>", "", text)
        cleaned = self._strip_inline_markers(cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _match_heading_by_link_text(self, link_text: str, headings: List[Heading], lang: str) -> Optional[Heading]:
        """Rank all headings by similarity to the link label and return the
        single closest one only when it clears the ratio floor AND leads the
        runner-up by the margin. A unique case-insensitive exact match always
        wins; multiple exact matches are ambiguous -> give up."""
        if not link_text or not headings:
            return None
        target = self._clean_heading_text(link_text)
        if not target:
            return None
        target_lc = target.lower()
        exact = [h for h in headings if self._clean_heading_text(h.text).lower() == target_lc]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
        import difflib
        scored: List[Tuple[float, Heading]] = []
        for heading in headings:
            ht = self._clean_heading_text(heading.text).lower()
            if not ht:
                continue
            ratio = difflib.SequenceMatcher(None, target_lc, ht).ratio()
            if target_lc in ht or ht in target_lc:
                ratio = max(ratio, 0.85)
            scored.append((ratio, heading))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        best_ratio, best_heading = scored[0]
        second_ratio = scored[1][0] if len(scored) > 1 else 0.0
        if best_ratio >= self._LINKTEXT_MIN_RATIO and (best_ratio - second_ratio) >= self._LINKTEXT_MIN_MARGIN:
            return best_heading
        return None

    def _resolve_anchor_from_link_text(self, link_text: str, headings: List[Heading], lang: str) -> Optional[str]:
        heading = self._match_heading_by_link_text(link_text, headings, lang)
        return heading.anchor if heading and heading.anchor else None
    # @AI_GENERATED: end

    def _fuzzy_match_anchor(self, anchor: str, existing_ids: set) -> "str | None":
        # Return single fuzzy-matched anchor: case-insensitive, norm, or edit-dist-1. None if ambiguous.
        def _norm(s: str) -> str:
            return re.sub(r"[-_]+", "-", s.lower().strip("-"))
        def _lev1(a: str, b: str) -> bool:
            if abs(len(a) - len(b)) > 1:
                return False
            if len(a) == len(b):
                return sum(x != y for x, y in zip(a, b)) == 1
            short, long_ = (a, b) if len(a) < len(b) else (b, a)
            return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))
        ci = [e for e in existing_ids if e.lower() == anchor.lower()]
        if len(ci) == 1:
            return ci[0]
        if len(ci) > 1:
            return None
        norm_a = _norm(anchor)
        normed = [e for e in existing_ids if _norm(e) == norm_a]
        if len(normed) == 1:
            return normed[0]
        if len(normed) > 1:
            return None
        lev = [e for e in existing_ids if _lev1(anchor, e)]
        if len(lev) == 1:
            return lev[0]
        return None

    def _normalize_tag(self, tag: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")
        return value

    def _strip_inline_markers(self, text: str) -> str:
        return re.sub(r"`([^`]+)`", r"\1", text).strip()


def _format_issue_location(issue: Dict[str, object]) -> str:
    if issue.get("line") is not None:
        return f"{issue['file']}:{issue['line']}"
    return str(issue["file"])


def _build_checklist(summary: Dict[str, object]) -> List[str]:
    """Group issues into a human checklist: what was auto-fixed vs. what
    still needs the user's attention, split by category."""
    issues = summary["issues"]

    def status_of(issue: Dict[str, object]) -> str:
        details = issue.get("details") or {}
        return str(details.get("status", "needs_manual_review"))

    auto_fixed = [i for i in issues if status_of(i) == "auto_fixed"]
    auto_fixable = [i for i in issues if status_of(i) == "auto_fixable"]
    broken_links = [i for i in issues if i["kind"] == "link" and status_of(i) not in ("auto_fixed", "auto_fixable")]
    missing_media = [i for i in issues if i["kind"] in ("media", "media-build") and status_of(i) not in ("auto_fixed", "auto_fixable")]
    anchors = [i for i in issues if i["kind"] == "anchor"]
    external = [i for i in issues if i["kind"] == "external-link"]
    anchor_alignment = [i for i in issues if i["kind"] == "anchor-alignment" and status_of(i) != "auto_fixed"]
    heading_mismatch = [i for i in issues if i["kind"] == "heading-count-mismatch"]
    missing_translation = [i for i in issues if i["kind"] == "missing-translation"]
    missing_rst_anchor = [i for i in issues if i["kind"] == "missing-rst-anchor" and status_of(i) != "auto_fixed"]
    stale_auto_id = [i for i in issues if i["kind"] == "stale-auto-id-reference"]
    inconsistent_version = [i for i in issues if i["kind"] == "inconsistent-doc-version"]
    # @AI_GENERATED: Kiro v1.0
    table_build_errors = [i for i in issues if i["kind"] == "table-build-error"]
    # @AI_GENERATED: end

    lines: List[str] = []
    lines.append(f"Scanned {summary['files_scanned']['en']} English files and {summary['files_scanned']['zh']} Chinese files. Repo name for link classification: {summary.get('repo_name')}")
    lines.append(f"Found {summary['issue_count']} issues.")
    lines.append("")

    def section(title: str, items: List[Dict[str, object]], hide_fixed: bool = True) -> None:
        if hide_fixed:
            items = [i for i in items if status_of(i) != "auto_fixed"]
        lines.append(f"## {title} ({len(items)})")
        if not items:
            lines.append("(none)")
        for issue in items:
            details = issue.get("details") or {}
            lines.append(f"- [{issue['kind']}] {_format_issue_location(issue)} {issue['message']}")
            if details.get("suggested_fix"):
                lines.append(f"    suggested_fix: {details['suggested_fix']}")
            if details.get("suggested_heading"):
                lines.append(f"    suggested_heading: {details['suggested_heading']}")
            if details.get("candidates"):
                lines.append(f"    candidates: {', '.join(details['candidates'])}")
            if details.get("note"):
                lines.append(f"    note: {details['note']}")
        lines.append("")

    section("已自动修复 / Auto-fixed", auto_fixed, hide_fixed=False)
    section("可自动修复（加 --fix 后会应用）/ Auto-fixable with --fix", auto_fixable)
    section("失效的内部链接（需人工处理）/ Broken internal links needing manual fix", broken_links)
    section("缺失图片/媒体文件 / Missing media", missing_media)
    section("锚点缺失或不确定 / Missing or uncertain anchors", anchors)
    section("外部/跨仓库链接（需人工确认）/ External links needing manual confirmation", external)
    section("中英文锚点未对齐 / Chinese/English anchor mismatches", anchor_alignment)
    section("缺失 RST 锚点声明 / Headings missing explicit .. _label: directive", missing_rst_anchor)
    # @AI_GENERATED: Kiro v1.0
    orphan_anchor = [i for i in issues if i["kind"] == "orphan-rst-anchor"]
    section("不在标题上的锚点 / Anchors that are not attached to a heading", orphan_anchor)
    # @AI_GENERATED: end
    section("中英文标题数量不一致 / Heading count mismatches", heading_mismatch)
    section("缺少对应语言版本的文件 / Missing translation counterpart", missing_translation)
    duplicate_anchor = [i for i in issues if i["kind"] == "duplicate-anchor" and status_of(i) != "auto_fixed"]
    section("同文档内重复锚点 / Duplicate anchors within a file", duplicate_anchor)
    section("疑似失效的 #idN 引用（需人工核对）/ Possibly-stale #idN references", stale_auto_id)
    section("链接版本号不一致 / Inconsistent doc-link version segments", inconsistent_version)
    # @AI_GENERATED: Kiro v1.0
    section("HTML 构建时表格渲染失败（需人工处理）/ Tables that failed to render during the HTML build", table_build_errors)
    # @AI_GENERATED: end

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit docs repositories for rendering and anchor issues")
    parser.add_argument("--en-root", default=None, help="Path to the English documentation repo")
    parser.add_argument("--zh-root", default=None, help="Path to the Chinese documentation repo")
    parser.add_argument("--repo-name", default=None, help="This product's repo name as used in /docs/{repo-name}/... links (default: inferred from the root folder name, e.g. dtv_en -> dtv)")
    parser.add_argument("--output", default="audit_report.json", help="Path to the JSON report file")
    parser.add_argument("--text-output", default=None, help="Optional path to write a plain-text checklist report")
    parser.add_argument("--fix", action="store_true", help="Apply safe, verifiable fixes: internal/same-repo link and media paths (only when exactly one matching file exists in the repo) and Chinese anchor-tag updates (only when the suggested tag actually resolves to the English anchor). Never touches cross-repo or third-party links and never invents a target.")
    parser.add_argument("--check-external", action="store_true", help="Verify cross-repo and third-party links over the network (HEAD/GET request) and only report the ones that fail. Off by default since it requires internet access and is slower.")
    parser.add_argument("--build-html", action="store_true", help="Build HTML with Sphinx before auditing")
    parser.add_argument("--build-dir", default=None, help="Optional output directory for Sphinx HTML builds")
    args = parser.parse_args()

    en_root = Path(args.en_root).resolve() if args.en_root else None
    zh_root = Path(args.zh_root).resolve() if args.zh_root else None
    if en_root and not en_root.exists():
        print(f"The provided English repo root does not exist: {en_root}", file=sys.stderr)
        return 2
    if zh_root and not zh_root.exists():
        print(f"The provided Chinese repo root does not exist: {zh_root}", file=sys.stderr)
        return 2
    if not en_root and not zh_root:
        print("Please provide at least one repo root via --en-root or --zh-root.", file=sys.stderr)
        return 2

    build_dir = Path(args.build_dir).resolve() if args.build_dir else None
    runner = AuditRunner(
        en_root, zh_root,
        fix=args.fix,
        build_html=args.build_html,
        build_dir=build_dir,
        repo_name=args.repo_name,
        check_external=args.check_external,
    )
    summary = runner.run()

    output_path = Path(args.output)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    checklist_lines = _build_checklist(summary)

    if args.text_output:
        text_output_path = Path(args.text_output)
        text_output_path.write_text("\n".join(checklist_lines) + "\n", encoding="utf-8")

    print("\n".join(checklist_lines))
    print(f"JSON report written to {output_path}")
    if args.text_output:
        print(f"Text checklist written to {args.text_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
