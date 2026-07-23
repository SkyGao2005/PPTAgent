import os
from pathlib import Path

from deeppresenter.utils.constants import (
    DEFAULT_WORKSPACE_BASE,
    PACKAGE_DIR,
    PROJECT_ROOT,
    WORKSPACE_BASE,
)


def test_default_workspace_is_project_userdata() -> None:
    assert PROJECT_ROOT == PACKAGE_DIR.parent
    assert DEFAULT_WORKSPACE_BASE == PROJECT_ROOT / "userdata"
    expected = Path(
        os.getenv("DEEPPRESENTER_WORKSPACE_BASE", str(DEFAULT_WORKSPACE_BASE))
    ).expanduser()
    assert WORKSPACE_BASE == expected
