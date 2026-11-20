"""Allow ``python -m warning_response``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
