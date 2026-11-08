"""Explicit protected-key setup. The secret is never printed or sent anywhere."""

import sys

from .credentials import import_api_key
from .paths import launcher_workspace


def main() -> int:
    if sys.argv[1:]:
        print("Usage: ./scripts/import-key", file=sys.stderr)
        return 2
    try:
        root, _ = launcher_workspace()
        import_api_key(root)
    except Exception:
        print("Key import failed. Put only your Groq key in root api.txt with mode 0600; no key was displayed.", file=sys.stderr)
        return 1
    print("Key imported into protected local storage; input api.txt removed. No network request made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
