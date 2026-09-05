"""Not a test module (pytest only collects ``test_*.py``) — shared fixture
builders for the build-v2-summary (``trialerror.summarize`` / the
``trialerror.retrieve.engine`` summary tier) test suite.

Reuses M8's own corpus builder (``tests._retrieve_fixtures.build_small_corpus``)
rather than re-deriving a second copy of the same chunker/anchor-building
plumbing -- M8 is fully landed by this build's order, so there is no
concurrent-edit risk in importing its test helper module (the same
precedent ``tests/_verify_fixtures.py`` already established for M9)."""

from __future__ import annotations

from tests._retrieve_fixtures import bootstrap_launch, build_small_corpus

__all__ = ["bootstrap_launch", "build_small_corpus", "OVER_LENGTH_QUOTE", "restricted_source_id"]

#: A double-quoted span longer than the D-COC-1 20-word cap
#: (``trialerror.summarize.api.MAX_EMBEDDED_QUOTE_WORDS``) -- used by the
#: fence-violation tests to build a summary body that must be refused for
#: a subject citing ``build_small_corpus``'s restricted source.
OVER_LENGTH_QUOTE = (
    '"this exact quorum reconfiguration paragraph is being quoted verbatim word for word for '
    'far more than twenty words on purpose so the D-COC-1 fence has something real to catch"'
)

assert len(OVER_LENGTH_QUOTE.strip('"').split()) > 20  # keep the fixture honest if edited later


def restricted_source_id(corpus: dict) -> str:
    return corpus["restricted_source_id"]
