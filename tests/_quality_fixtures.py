"""Not a test module (pytest only collects ``test_*.py``) -- the extraction
quality fixtures: four document shapes whose text is deliberately
pathological in one way each, plus a seeder that writes a document+elements
straight into a store.

The four shapes are the ones the measures exist to tell apart:

* ``CLEAN_TEXT`` -- ordinary prose: short tokens, sentence terminators, no
  characters that are not text.
* ``GLUED_TEXT`` -- a word-spacing failure: a whole line arrives as one
  70-character "token", the small end of what a real one does.
* ``UNUSABLE_TEXT`` -- a text layer decoded under the wrong encoding:
  replacement characters and C0 control bytes where letters should be.
* ``NO_TERMINATOR_TEXT`` -- a layout pass that lost sentence structure:
  fragments, no terminators anywhere.

Every shape is deliberately longer than ``[ingest.quality] min_tokens``
(:data:`trialerror.ingest.quality.DEFAULT_MIN_TOKENS`, 200 tokens). Below
that floor a document is measured but never counted suspect -- its rates and
densities have no denominator worth judging -- so a note-sized fixture would
be testing the floor rather than the pathology it is named for. Each shape
reaches document length by repeating itself, which leaves every measure's
value unchanged (``tests/test_ingest_quality.py`` asserts both halves of
that: the fixtures clear the floor, and a small copy of the same text is
measured identically and simply not judged).

The seeder writes rows directly rather than running the pipeline: these
fixtures are about MEASURING text, and a normalizer that produced exactly
the text a test wants is not something the pipeline can be asked for.
"""

from __future__ import annotations

from typing import Any, Sequence

from trialerror.ingest import pipeline
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id

_CLEAN_PARAGRAPH = (
    "The apparatus was assembled from parts already on the bench. Each run was timed "
    "twice, by two clocks, and the slower reading was kept. Three trials disagreed; the "
    "disagreement is reported rather than averaged away. A fourth trial, run a week "
    "later, matched the first two."
)

#: Five paragraphs, not one -- see the module docstring's note on the size
#: floor. Repetition leaves every measure identical (each one is a rate or a
#: density over the text's own length) and puts the fixture on the judged side
#: of ``[ingest.quality] min_tokens``.
CLEAN_TEXT = " ".join([_CLEAN_PARAGRAPH] * 5)

_GLUED_LINE = " ".join(
    [
        "Theapparatuswasassembledfrompartsalreadyonthebenchandeachrunwastimedtwice",
        "byTwoClocksAndTheSlowerReadingWasKeptThreeTrialsDisagreedTheDisagreement",
        "isreportedratherthanaveragedawayafourthtrialrunaweeklatermatchedthefirsttwo",
    ]
)

#: The glued line repeated to document length: the rate stays 1.0 (every
#: token is still a glued one), and the document is no longer a three-token
#: note the size floor would -- correctly -- refuse to judge.
GLUED_TEXT = " ".join([_GLUED_LINE] * 70)

UNUSABLE_TEXT = (
    "Th� apparatu� wa� assemble� fro� part�\x00 alread� o� "
    "th� benc�\x01 Eac� ru� wa� time� twic�\x02 b� tw� "
    "clock�\x03 an� th� slowe� readin� wa� kep�\x04"
) * 10

NO_TERMINATOR_TEXT = (
    "apparatus assembled parts bench\nrun timed twice two clocks\nslower reading kept\n"
    "three trials disagreed\ndisagreement reported not averaged\nfourth trial week later\n"
) * 9


def register_fixture_source(store: Store, launch_id: str, *, title: str = "Fixture source") -> str:
    row = pipeline.register_source(
        store,
        kind="book",
        title=title,
        license_tier="open",
        acquisition_route="web",
        registered_by_launch=launch_id,
    )
    return row["source_id"]


def seed_document(
    store: Store,
    launch_id: str,
    text: str | Sequence[str] | None = None,
    *,
    page_count: int | None = None,
    source_id: str | None = None,
    status: str = "indexed",
    media_type: str = "pdf-text",
    doc_id: str | None = None,
) -> str:
    """One ``document`` row plus one ``element`` row per page of ``text``.

    ``text`` may be one string (a single element, page 1) or a sequence of
    per-page strings. ``page_count`` is the document column -- left
    ``None`` unless a test is about the per-page measure, because that is
    how a non-paginated format actually arrives.
    """
    if source_id is None:
        source_id = register_fixture_source(store, launch_id)
    doc_id = doc_id or new_id("DOC")
    pages: list[str] = [text] if isinstance(text, str) else list(text or [])

    insert(
        store,
        "document",
        {
            "doc_id": doc_id,
            "source_id": source_id,
            "rel_path": f"archive/{doc_id}.txt",
            "raw_path": f"raw/{doc_id}.pdf",
            "media_type": media_type,
            "page_count": page_count,
            "normalizer_id": "trialerror-fixture",
            "normalizer_version": "1",
            "sha256": f"{abs(hash(doc_id)):064x}"[:64],
            "status": status,
        },
    )
    for seq, page_text in enumerate(pages):
        insert(
            store,
            "element",
            {
                "element_id": new_id("ELM"),
                "doc_id": doc_id,
                "seq": seq,
                "type": "NarrativeText",
                "text": page_text,
                "text_as_html": None,
                "page_number": seq + 1,
                "bbox": None,
                "parent_element": None,
                "category_depth": None,
                "detection_origin": "fixture",
            },
        )
    return doc_id


def seed_quality_corpus(store: Store, launch_id: str) -> dict[str, str]:
    """The four shapes as four documents; returns ``{name: doc_id}``."""
    source_id = register_fixture_source(store, launch_id, title="Quality fixture source")
    return {
        "clean": seed_document(store, launch_id, [CLEAN_TEXT, CLEAN_TEXT], page_count=2, source_id=source_id),
        "glued": seed_document(store, launch_id, GLUED_TEXT, source_id=source_id),
        "unusable": seed_document(store, launch_id, UNUSABLE_TEXT, source_id=source_id),
        "fragments": seed_document(store, launch_id, NO_TERMINATOR_TEXT, source_id=source_id),
    }


def seed_many_documents(store: Store, launch_id: str, count: int, *, text: str | None = None) -> list[str]:
    """``count`` clean documents -- for sampling/cost work, where what
    matters is how many rows the measurement has to walk."""
    source_id = register_fixture_source(store, launch_id, title="Bulk fixture source")
    body: Any = text if text is not None else CLEAN_TEXT
    return [seed_document(store, launch_id, body, source_id=source_id) for _ in range(count)]
