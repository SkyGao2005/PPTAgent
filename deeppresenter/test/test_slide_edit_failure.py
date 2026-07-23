"""Failure handling for the single-slide conversational editor.

The editor lives in ``pptagent`` but is reached from the DeepPresenter UI, and
``pptagent``'s own suite cannot be collected without model credentials, so its
runnable tests live here.
"""

from __future__ import annotations

from pptagent.apis import CodeExecutor
from pptagent.editor.service import SlideEditService
from pptagent.presentation import SlidePage


def _slide() -> SlidePage:
    return SlidePage(
        shapes=[],
        backgrounds=[],
        slide_idx=1,
        real_idx=1,
        slide_notes=None,
        slide_layout_name="Blank",
        slide_title=None,
        slide_width=12192000,
        slide_height=6858000,
    )


def test_the_failure_report_leads_with_the_exception() -> None:
    """Every consumer truncates this string, and a traceback ends with the cause.

    Reported head-first, the message was three frames of boilerplate plus a
    `<string>` line that linecache resolves to the process's spawn command --
    so a real error read as `from multiprocessing.spawn import spawn_main`.
    """

    result = CodeExecutor(retry_times=1).execute_actions(
        "del_image(99)", _slide(), None, found_code=True
    )

    assert result is not None
    _, trace = result
    first = trace.splitlines()[0]
    assert first.startswith("SlideEditError:")
    assert "Cannot find element 99" in first
    assert "del_image(99)" in trace.splitlines()[1]
    # Truncation must not be able to hide the cause again.
    assert "Cannot find element 99" in trace[:500]


def test_a_failed_batch_leaves_the_slide_untouched() -> None:
    """Edit calls mutate the slide one at a time.

    A batch that fails halfway used to leave a half-edited page on screen, and
    the next retry compounded it. Successful batches are adopted; failed ones
    are dropped with the live slide never having been handed to the executor.
    """

    service = SlideEditService.__new__(SlideEditService)
    service.slide = _slide()
    service.doc = None
    original_shapes = service.slide.shapes
    seen: list[object] = []

    real = CodeExecutor.execute_actions

    def _spy(self, actions, edit_slide, doc, found_code=False):
        seen.append(edit_slide)
        return real(self, actions, edit_slide, doc, found_code)

    CodeExecutor.execute_actions = _spy
    try:
        ok, error = service._run_api_actions(["del_image(99)"])  # noqa: SLF001
    finally:
        CodeExecutor.execute_actions = real

    assert not ok
    assert "Cannot find element 99" in error
    # The executor never touched the live slide, and nothing was adopted.
    assert seen and seen[0] is not service.slide
    assert service.slide.shapes is original_shapes


def test_bare_api_calls_reach_the_slide() -> None:
    """A command is opened by a `#` comment, which only the generator emits.

    The conversational editor sends bare calls, so `del_paragraph` and
    `clone_paragraph` crashed on an empty command history before ever
    touching the slide -- which is every "make this shorter" instruction.
    """

    executor = CodeExecutor(retry_times=1)
    result = executor.execute_actions(
        "del_paragraph(0, 0)", _slide(), None, found_code=True
    )

    assert result is not None
    _, trace = result
    # It failed on the slide's contents, not on the executor's bookkeeping.
    assert trace.startswith("SlideEditError:")
    assert "IndexError" not in trace


def test_mixing_clone_and_delete_is_still_rejected() -> None:
    """The rule the command history exists to enforce must still hold.

    Seeding the history is what a `#` comment plus a successful clone would
    leave behind; the point is that a del_ in the same command is refused.
    """

    from pptagent.apis import HistoryMark

    executor = CodeExecutor(retry_times=1)
    executor.command_history.append([HistoryMark.COMMENT_CORRECT, "# shorten", "clone"])
    result = executor.execute_actions(
        "del_paragraph(0, 0)", _slide(), None, found_code=True
    )

    assert result is not None
    assert "within a single command" in result[1]
