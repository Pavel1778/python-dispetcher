"""Read-only inventory of the OpenHands API NEXUS talks to.

Thin wrapper over :mod:`orchestration.openhands_inventory`, so the inventory can
be run the way the other operator tools are::

    python3 tools/openhands_inventory.py
    python3 tools/openhands_inventory.py --json
    python3 tools/openhands_inventory.py --agent-server-url http://127.0.0.1:60000

It reads the live API and writes nothing. The API key is read from
``OPENHANDS_API_KEY`` and never printed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.openhands_inventory import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
