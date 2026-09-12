"""
Shared pytest configuration.

Makes the agent test fixtures importable from other test packages
(e.g. tests/security) so security tests can reuse them instead of
duplicating fakes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = (
    Path(__file__).resolve().parents[1]
)

# Host-security invariant: pytest temp never leaves the project.
# Set here (not in pytest.ini) so the same config also works inside
# the Docker sandbox, where .sandbox/ is not part of the snapshot.
_PROJECT_TEMP = PROJECT_ROOT / "data" / "tmp"

_PROJECT_TEMP.mkdir(
    parents=True,
    exist_ok=True,
)

for _name in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_name] = str(_PROJECT_TEMP)

# tempfile caches its choice on first use, so set the cache too.
import tempfile  # noqa: E402

tempfile.tempdir = str(_PROJECT_TEMP)


AGENT_TESTS = (
    Path(__file__).resolve().parent / "agent"
)

if str(AGENT_TESTS) not in sys.path:
    sys.path.insert(0, str(AGENT_TESTS))
