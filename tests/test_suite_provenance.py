"""The suite must test the source tree it was run from.

With an editable install, the package's `.pth` points at the main checkout. Without `src` first
on the path, a run in another worktree would test the main checkout's code instead.

The `pyproject.toml` check catches a removed setting in the main checkout, where the import check
cannot tell the trees apart. The import check catches the problem in a worktree.
"""
import pathlib
import sys

import gcp_audit

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_pyproject_still_puts_the_source_tree_on_the_path():
    """The `pythonpath` setting is checked as text.

    In the main checkout `import gcp_audit` resolves to this `src/` with or without the setting,
    so no other test would notice it was removed.
    """
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pythonpath = ["src"]' in text, (
        "pytest must prepend this tree's src/; without it an editable install's .pth wins and "
        "a worktree run reports 2200 passed against the main checkout's source")


def test_the_package_under_test_is_this_trees_source():
    """The imported package comes from this tree's `src/`."""
    resolved = pathlib.Path(gcp_audit.__file__).resolve()
    assert resolved.is_relative_to(REPO / "src"), (
        f"gcp_audit resolved to {resolved}, outside {REPO / 'src'}: every assertion in this run "
        "is about a different checkout")


def test_the_source_tree_precedes_the_installed_copy_on_sys_path():
    """This tree's `src/` is on the live `sys.path`."""
    src = str((REPO / "src").resolve())
    assert src in [str(pathlib.Path(p).resolve()) for p in sys.path if p], (
        "this tree's src/ is not on sys.path, so the import above came from somewhere else")
