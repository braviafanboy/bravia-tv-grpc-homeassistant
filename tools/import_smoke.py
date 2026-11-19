"""Import every module of the integration against the installed Home Assistant.

CI runs this on the newest Home Assistant release, which the test package
does not support yet: an API the integration imports that has been removed
shows up here weeks before the test suite could run there.

    python tools/import_smoke.py
"""

from __future__ import annotations

import importlib
from pathlib import Path
import pkgutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import custom_components.bravia_tv_grpc as integration  # noqa: E402


def main() -> int:
    names = [integration.__name__] + sorted(
        module.name
        for module in pkgutil.walk_packages(
            integration.__path__, integration.__name__ + "."
        )
    )
    failed = 0
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as err:  # noqa: BLE001 - report every module, then fail
            failed += 1
            print(f"FAILED  {name}: {type(err).__name__}: {err}")
    print(f"{len(names) - failed} of {len(names)} modules import")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
