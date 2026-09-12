"""
Shared pytest configuration.

Makes the agent test fixtures importable from other test packages
(e.g. tests/security) so security tests can reuse them instead of
duplicating fakes.
"""

from __future__ import annotations

import sys
from pathlib import Path


AGENT_TESTS = (
    Path(__file__).resolve().parent / "agent"
)

if str(AGENT_TESTS) not in sys.path:
    sys.path.insert(0, str(AGENT_TESTS))
