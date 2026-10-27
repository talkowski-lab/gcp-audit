"""`gca setup`: the wizard and the script it writes. Nothing here, or in the module, sends a request."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re

import pytest

from gcp_audit import cli, setup_script
from gcp_audit.setup_script import Plan, SetupError

ACCOUNT = "someone@example.org"
SA = "gca-audit@lab-home-1.iam.gserviceaccount.com"
PLAN = Plan(account=ACCOUNT, sa_name="gca-audit", home_project="lab-home-1")


def args(**over) -> argparse.Namespace:
    base = dict(account=None, service_account=None, create=None, home_project=None,
                bq_jobs=False, out=pathlib.Path("gca-setup.sh"), force=False)
    base.update(over)
    return argparse.Namespace(**base)


def answers(*replies):
    """An `input` stand-in that returns each reply in turn and fails if asked once too often."""
    it = iter(replies)

    def ask(prompt):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"unexpected prompt: {prompt!r}") from None
    return ask


# ---------------------------------------------------------------- the script

def test_the_script_carries_the_plan_and_every_step():
    text = setup_script.render(PLAN, today=dt.date(2026, 10, 28))
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "Written by `gca setup` on 2026-10-28" in text
    assert f"ACCOUNT={ACCOUNT}\n" in text
    assert "SA_NAME=gca-audit\nHOME_PROJECT=lab-home-1\n" in text
    for step in ("create)", "key)", "impersonate)", "find)", "grant)"):
        assert step in text
    assert "--account=\"$ACCOUNT\"" in text


def test_the_script_grants_the_documented_roles_and_job_user_only_when_asked():
    plain = setup_script.render(PLAN)
    roles_line = re.search(r"^ROLES=\((.*)\)$", plain, re.M).group(1).split()
    assert tuple(roles_line) == setup_script.ROLES
    assert setup_script.BQ_JOB_ROLE not in plain
    with_jobs = setup_script.render(Plan(**{**PLAN.__dict__, "bq_jobs": True}))
    assert setup_script.BQ_JOB_ROLE in re.search(r"^ROLES=\((.*)\)$", with_jobs, re.M).group(1)


def test_every_granted_role_is_documented_in_setup_md():
    doc = (pathlib.Path(__file__).resolve().parents[1] / "docs/SETUP.md").read_text("utf-8")
    for role in setup_script.ROLES + (setup_script.BQ_JOB_ROLE,):
        assert f"`{role}`" in doc, f"{role} is granted by gca setup but not in docs/SETUP.md"


def test_create_enables_the_apis_gca_bills_to_the_home_project():
    """Resource Manager and Cloud Billing calls are charged to the service account's own project;
    with either API off there, every run fails (seen live, 2026-10-28)."""
    text = setup_script.render(PLAN)
    assert 'gc services enable "${HOME_APIS[@]}" --project="$HOME_PROJECT"' in text
    for api in ("cloudresourcemanager.googleapis.com", "cloudbilling.googleapis.com"):
        assert api in re.search(r"^HOME_APIS=\((.*)\)$", text, re.M).group(1)


def test_the_grant_step_is_a_dry_run_unless_told_to_apply():
    text = setup_script.render(PLAN)
    assert 'if [[ "$apply" == "--apply" ]]' in text
    assert "dry run; add --apply" in text


def test_the_find_step_passes_project_ids_as_arguments_not_by_substitution():
    """macOS `xargs -I` caps the substituted command at 255 bytes, which the curl call exceeds."""
    text = setup_script.render(PLAN)
    assert "xargs -P 8 -n 1 sh -c" in text
    assert "xargs -I" not in text


@pytest.mark.parametrize("bad", [
    Plan(account="me@example.org; rm -rf ~", sa_name="gca-audit", home_project="lab-home-1"),
    Plan(account=ACCOUNT, sa_name="gca-audit$(id)", home_project="lab-home-1"),
    Plan(account=ACCOUNT, sa_name="gca-audit", home_project="Lab_Home"),
    Plan(account=SA, sa_name="gca-audit", home_project="lab-home-1"),
])
def test_a_value_that_is_not_shaped_right_never_reaches_the_script(bad):
    with pytest.raises(SetupError):
        setup_script.render(bad)


def test_a_service_account_email_splits_into_name_and_home_project():
    assert setup_script.parse_sa_email(SA) == ("gca-audit", "lab-home-1")
    with pytest.raises(SetupError):
        setup_script.parse_sa_email(ACCOUNT)


def test_write_refuses_to_replace_a_file_without_force(tmp_path):
    out = tmp_path / "gca-setup.sh"
    setup_script.write(PLAN, out)
    assert out.stat().st_mode & 0o111
    with pytest.raises(SetupError):
        setup_script.write(PLAN, out)
    setup_script.write(PLAN, out, force=True)


# ---------------------------------------------------------------- the wizard

def test_flags_alone_write_the_script_without_prompting(tmp_path, capsys):
    out = tmp_path / "s.sh"
    code = cli.cmd_setup(args(account=ACCOUNT, service_account=SA, out=out),
                         ask=answers(), interactive=False)
    assert code == cli.EXIT_OK
    assert "SA_NAME=gca-audit\nHOME_PROJECT=lab-home-1\n" in out.read_text()
    assert "gca sent nothing" in capsys.readouterr().out


def test_the_next_steps_can_be_pasted_for_a_script_in_the_current_directory(tmp_path, capsys,
                                                                           monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.cmd_setup(args(account=ACCOUNT, service_account=SA, out=pathlib.Path("gca-setup.sh")),
                  ask=answers(), interactive=False)
    assert "  ./gca-setup.sh create" in capsys.readouterr().out


def test_without_a_terminal_a_missing_flag_is_an_error_not_a_prompt(tmp_path, capsys):
    out = tmp_path / "s.sh"
    code = cli.cmd_setup(args(account=ACCOUNT, out=out), ask=answers(), interactive=False)
    assert code == cli.EXIT_ERROR
    assert not out.exists()
    assert "--service-account" in capsys.readouterr().err


def test_the_wizard_asks_for_an_existing_service_account(tmp_path):
    out = tmp_path / "s.sh"
    code = cli.cmd_setup(args(out=out), interactive=True,
                         ask=answers(ACCOUNT, "y", SA, "n"))
    assert code == cli.EXIT_OK
    text = out.read_text()
    assert "HOME_PROJECT=lab-home-1" in text and setup_script.BQ_JOB_ROLE not in text


def test_the_wizard_creates_a_new_service_account_with_the_default_name(tmp_path):
    out = tmp_path / "s.sh"
    code = cli.cmd_setup(args(out=out), interactive=True,
                         ask=answers(ACCOUNT, "n", "lab-home-2", "", "y"))
    assert code == cli.EXIT_OK
    text = out.read_text()
    assert "SA_NAME=gca-audit\nHOME_PROJECT=lab-home-2\n" in text
    assert setup_script.BQ_JOB_ROLE in text


def test_the_wizard_asks_again_after_a_bad_answer(tmp_path, capsys):
    out = tmp_path / "s.sh"
    code = cli.cmd_setup(args(out=out), interactive=True,
                         ask=answers("not-an-email", ACCOUNT, "y", SA, ""))
    assert code == cli.EXIT_OK
    assert "not an email address" in capsys.readouterr().err


def test_an_existing_script_is_replaced_only_on_a_yes(tmp_path):
    out = tmp_path / "s.sh"
    out.write_text("mine\n")
    flags = dict(account=ACCOUNT, service_account=SA, out=out)
    assert cli.cmd_setup(args(**flags), ask=answers("n", "n"), interactive=True) == cli.EXIT_ERROR
    assert out.read_text() == "mine\n"
    assert cli.cmd_setup(args(**flags), ask=answers(), interactive=False) == cli.EXIT_ERROR
    assert cli.cmd_setup(args(**flags, force=True), ask=answers(), interactive=False) == cli.EXIT_OK
    assert out.read_text().startswith("#!/usr/bin/env bash")


def test_setup_is_reachable_from_the_command_line(tmp_path):
    out = tmp_path / "s.sh"
    assert cli.main(["setup", "--account", ACCOUNT, "--create", "gca-audit",
                     "--home-project", "lab-home-1", "--bq-jobs", "-o", str(out)]) == 0
    assert setup_script.BQ_JOB_ROLE in out.read_text()
