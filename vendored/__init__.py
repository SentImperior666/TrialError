"""Marker package for ``vendored/`` (design Section 3.1: "adopted
third-party code, one dir per item + VENDORED.md manifest"). This file
exists so ``vendored.<item>.<module>`` is a normal, explicit Python import
rather than relying on implicit namespace-package resolution — see
``trialerror/memory/merge.py``'s module docstring for the repo-root sys.path
convention that makes that import reachable from an editable install
(this directory is deliberately NOT part of the installed ``trialerror``
distribution — ``pyproject.toml``'s ``packages.find`` includes only
``trialerror*`` — vendored code is consumed by filesystem-relative import from
within this checkout, exactly like ``tests/_concurrent_writer_worker.py``'s
existing ``sys.path.insert(0, repo_root)`` precedent).

Not every vendored item is Python (e.g. a prompt template or a non-Python
sanitizer script may land here too) — this marker only matters for the
items that ARE imported as Python modules.
"""
