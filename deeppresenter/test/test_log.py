import logging
import uuid
from pathlib import Path

from deeppresenter.utils.log import create_logger


def test_create_logger_reconfigures_an_existing_task_logger(tmp_path: Path) -> None:
    """Research, Design, and retries may create the same task logger in sequence."""

    name = f"deeppresenter-loop-test-{uuid.uuid4().hex}"
    first_path = tmp_path / "research.log"
    second_path = tmp_path / "design.log"

    try:
        first = create_logger(name, first_path)
        first.info("research")

        second = create_logger(name, second_path)
        second.info("design")

        assert second is first
        assert "research" in first_path.read_text(encoding="utf-8")
        assert "design" in second_path.read_text(encoding="utf-8")
        assert "design" not in first_path.read_text(encoding="utf-8")
    finally:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()
        logging.Logger.manager.loggerDict.pop(name, None)
