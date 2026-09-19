"""DDL for the four TrialError stores, verbatim from design Section 4 (platform,
ops, knowledge) and Section 4.4 (jobs), plus Sections 9.6-9.8 (lens, memory,
room — in M1 scope per the review union: "M1 scope corrected to '§4 +
§9.6-9.8 verbatim'").

Each sibling module (``platform``, ``ops``, ``knowledge``, ``jobs``) exports:

- ``TABLES``: a tuple of every base-table name the module creates (used by
  ``trialerror.stores.TABLE_DB`` for write-API routing and by the
  ``store_schema_version`` doctor check to know what "fully migrated"
  means).
- ``MIGRATIONS``: an ordered tuple of :class:`trialerror.stores.migrate.Migration`
  applied via ``PRAGMA user_version``.

v0.1's entire schema ships as one migration (version 1) per DB — there is
no prior version to migrate *from* yet; the runner and the numbering are
what M1 delivers, not a multi-step history (that starts the day schema v2
is needed).
"""
