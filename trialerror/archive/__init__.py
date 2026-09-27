"""The transcript archive (``trialerror archive``): a content-addressed copy of
session transcripts, kept outside every repository, and its audit."""

from trialerror.archive.store import (  # noqa: F401
    ArchiveError,
    archive_status,
    is_secret,
    kind_of,
    restore,
    run_archive,
)
