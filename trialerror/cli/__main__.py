"""Allows ``python -m trialerror.cli ...`` as an alternative to the installed
``trialerror`` console script (handy in tests / environments where the venv's
Scripts/bin dir isn't on PATH)."""

from trialerror.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
