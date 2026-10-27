"""The lint that enforces "nothing but the guard reaches a wire" — tested by breaking it on purpose.

A gate nobody has watched fail is a gate that might not be looking. Every case below plants one
violation in a temporary tree and fails if the linter walks past it; the last cases prove the
opposite direction too, so the lint is not simply always-red.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import lint_readonly as lint  # noqa: E402  (tools/ is a script directory, not a package)

VIOLATIONS = [
    ("subprocess", "import subprocess\nsubprocess.run(['gcloud', 'compute', 'instances', 'list'])\n",
     "banned-import subprocess"),
    ("socket", "import socket\n", "banned-import socket"),
    ("urllib", "import urllib.request\nurllib.request.urlopen('https://x')\n",
     "banned-import urllib.request"),
    ("http_client", "import http.client\n", "banned-import http.client"),
    ("requests", "import requests\nrequests.get('https://x')\n", "banned-import requests"),
    ("importlib", "import importlib\nimportlib.import_module('gcp_audit.guard')\n",
     "banned-import importlib"),
    ("admin", "import admin.setup_sa\n", "banned-import"),
    ("os_system", "import os\nos.system('gsutil ls')\n", "banned-call os.system"),
    ("eval", "eval('1+1')\n", "banned-call eval"),
    ("compile", "compile('1', '<s>', 'eval')\n", "banned-call compile"),
    ("dunder_import", "__import__('subprocess')\n", "banned-call __import__"),
    ("spawn", "import os\nos.spawnv(os.P_NOWAIT, '/bin/sh')\n", "banned-call os.spawnv"),
    ("session_post", "session.post('https://compute.googleapis.com/x')\n", "http-mutation"),
    ("session_delete", "http.delete(url)\n", "http-mutation"),
    ("urlopen", "urllib_request.urlopen(url)\n", "http-mutation"),
    ("open_connection", "loop.open_connection(host, 443)\n", "http-mutation"),
    ("redirects", "session.request('GET', url, allow_redirects=True)\n", "allow_redirects"),
    ("dynamic_getattr", "getattr(obj, 's' + 'end')(req)\n", "dynamic-attribute"),
    ("syntax_error", "def broken(:\n", "SyntaxError"),
]


def check(tmp_path: Path, source: str, **kwargs) -> list[str]:
    target = tmp_path / "src/gcp_audit/sample.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    hits, _counts = lint.lint([target], tmp_path)
    return hits


@pytest.mark.parametrize("name,source,needle", VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_the_lint_fails_on_the_thing_it_claims_to_catch(tmp_path, name, source, needle):
    hits = check(tmp_path, source)
    assert any(needle in hit for hit in hits), f"{name} passed the lint: {hits}"


def test_a_clean_file_passes(tmp_path):
    source = ("from gcp_audit import guard\n"
              "def read_it(project):\n"
              "    return guard.session().request('compute.images.list', {'project': project})\n")
    assert check(tmp_path, source) == []


def test_going_through_the_guard_is_allowed_and_shelling_out_is_not(tmp_path):
    """The pair the lint exists to distinguish: same intent, one admissible and one not."""
    ok = "rows = guard.paged('compute.disks.aggregatedList', params={'project': p})\n"
    bad = ("import subprocess\n"
           "rows = subprocess.run(['gcloud', 'compute', 'disks', 'list'])\n")
    assert check(tmp_path, ok) == []
    assert any("banned-import" in h for h in check(tmp_path, bad))


def test_an_allowance_needs_its_reason_on_the_same_line(tmp_path):
    with_reason = check(tmp_path, "import requests  # lint-allow: staged reply in a helper\n")
    without = check(tmp_path, "import requests  # lint-allow\n")
    assert not any("banned-import" in h for h in with_reason)
    assert any("without a reason" in h for h in without)


def test_an_allowance_does_not_spill_to_the_next_line(tmp_path):
    source = ("import requests  # lint-allow: justified in the review\n"
              "import subprocess\n")
    hits = check(tmp_path, source)
    assert any("banned-import subprocess" in h for h in hits)


def test_the_pinned_allowance_count_is_a_gate_not_a_suggestion(tmp_path):
    source = "import requests  # lint-allow: staged reply object\n"
    target = tmp_path / "src/gcp_audit/pinned.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    original = dict(lint.ALLOW_PIN)
    try:
        lint.ALLOW_PIN.clear()
        assert not any("lint-allow-count" in h for h in check(tmp_path, source))
        lint.ALLOW_PIN["src/gcp_audit/pinned.py"] = 2       # claims two, file has one
        assert any("lint-allow-count" in h for h in check(tmp_path, source))
    finally:
        lint.ALLOW_PIN.clear()
        lint.ALLOW_PIN.update(original)


def test_the_two_named_exceptions_are_the_only_ones_and_are_still_needed():
    """Both files import something on the banned list for a stated reason. If either stops needing
    it, the exception has to go — an exception nobody uses is a hole kept warm."""
    assert set(lint.IMPORT_EXCEPTIONS) == {"src/gcp_audit/guard.py", "tests/conftest.py"}
    root = Path(__file__).resolve().parents[1]
    guard_src = (root / "src/gcp_audit/guard.py").read_text(encoding="utf-8")
    conftest = (root / "tests/conftest.py").read_text(encoding="utf-8")
    assert "requests.get" in guard_src, "guard.py no longer uses requests; drop its exception"
    assert "socket.socket" in conftest, "conftest no longer blocks the connect seam"


def test_the_shipped_tree_is_clean():
    """The real tree, not a fixture: the same call AGENTS.md tells a lane to run."""
    root = Path(__file__).resolve().parents[1]
    targets = []
    for sub in ("src/gcp_audit", "tests", "tools"):
        targets.extend(lint.python_files(root / sub))
    hits, counts = lint.lint(targets, root)
    assert hits == [], "\n".join(hits)
    assert sum(counts.values()) == 0, "the shipped tree has no justified allowances; keep it that way"
