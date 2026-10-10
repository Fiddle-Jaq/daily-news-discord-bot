from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_STATE = Path(__file__).parent.parent / "state.json"


@pytest.fixture(autouse=True)
def _repo_state_json_is_untouched() -> Iterator[None]:
    """Tests must point --state at a temporary file, never at the committed state.json."""
    before = REPO_STATE.read_bytes() if REPO_STATE.exists() else None
    yield
    after = REPO_STATE.read_bytes() if REPO_STATE.exists() else None
    assert after == before, "a test created or modified the repository's state.json"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"
