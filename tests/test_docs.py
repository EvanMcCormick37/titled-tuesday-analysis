"""Keeps the CLAUDE.md files honest.

1. Every repo path named in backticks in a CLAUDE.md or a decision record exists.
   Paths resolve against the repo root, then against the doc's own directory.
   Proposals (docs/proposals/) are skipped: plans name files that don't exist yet.
   data/ is skipped: it holds local, gitignored artifacts.
2. Every subsystem CLAUDE.md is listed in the root CLAUDE.md, so none goes unnoticed.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build'}
PATH_RE = re.compile(r'^[\w./-]+(\.(py|md|ipynb|tsx|ts|toml|txt|sh|json|css)|/)$')
FENCE_RE = re.compile(r'```.*?```', re.S)
SPAN_RE = re.compile(r'`([^`\n]+)`')


def _walk(pattern: str) -> list[Path]:
    return [p for p in ROOT.rglob(pattern) if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)]


def doc_files() -> list[Path]:
    return sorted(_walk('CLAUDE.md') + list((ROOT / 'docs' / 'decisions').glob('*.md')))


def referenced_paths(doc: Path) -> list[str]:
    text = FENCE_RE.sub('', doc.read_text(encoding='utf-8'))
    out = []
    for span in SPAN_RE.findall(text):
        span = span.strip()
        if ' ' in span or any(c in span for c in '{}<>*…~$') or span.startswith(('/', '../', 'http', 'data/')):
            continue
        if PATH_RE.match(span) and ('/' in span or '.' in span):
            out.append(span)
    return out


class TestDocs(unittest.TestCase):
    def test_backticked_paths_exist(self):
        missing = []
        for doc in doc_files():
            for ref in referenced_paths(doc):
                if not ((ROOT / ref).exists() or (doc.parent / ref).exists()):
                    missing.append(f'{doc.relative_to(ROOT)}: `{ref}`')
        self.assertEqual(missing, [], 'Docs name paths that do not exist:\n' + '\n'.join(missing))

    def test_subsystem_docs_are_listed_in_root(self):
        root_text = (ROOT / 'CLAUDE.md').read_text(encoding='utf-8')
        unlisted = [str(p.relative_to(ROOT).as_posix()) for p in _walk('CLAUDE.md')
                    if p.parent != ROOT and p.relative_to(ROOT).as_posix() not in root_text]
        self.assertEqual(unlisted, [], 'Add these to the docs table in the root CLAUDE.md: ' + ', '.join(unlisted))


if __name__ == '__main__':
    unittest.main()
