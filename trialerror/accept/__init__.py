"""``trialerror.accept`` -- M15's acceptance-harness journey runners. Design
Section 12 (M15 row): "end-to-end smoke: init program -> ingest -> search
w/ citations (fence incl.) -> book/spawn-refusal -> citecheck -> gate ->
close-refusal -> close; doctor green ... doubles as the CI definition."

This package holds the SINGLE canonical implementation of each acceptance
journey (:mod:`trialerror.accept.journeys`), reused by two front doors:

- ``tests/acceptance/`` -- pytest scenarios (``pytest -m acceptance``, the
  design's own stated acceptance bar).
- ``trialerror accept`` (``trialerror/cli/accept.py``) -- a doctor-shaped CLI summary
  over the same journeys, for a human or agent to run without pytest.

Kept as its own top-level package (not folded into ``trialerror/util/`` or a
sibling module's directory) because M15 is explicitly build-order 7, LAST,
gated on every other module -- it is allowed to import from all of them,
which no earlier-order module may do.
"""
