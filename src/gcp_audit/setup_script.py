"""setup_script.py — `gca setup`: write a shell script that sets up the auditor service account.

gca never changes a cloud resource, so this module sends nothing and runs nothing. It asks for
the user's own Google account and the service account, checks their shape, and writes a bash
script of `gcloud` commands for the user to review and run under their own login.

Every value that reaches the script is validated against a strict pattern and then shell-quoted,
so a typo cannot become a different command.
"""
from __future__ import annotations

import datetime as _dt
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from gcp_audit import model

#: The roles `gca table` needs in each audited project (`docs/SETUP.md` §2).
ROLES = ("roles/compute.viewer", "roles/storage.viewer", "roles/monitoring.viewer",
         "roles/bigquery.metadataViewer")
#: Needed only for `--read-bq-storage`, because the TABLE_STORAGE query runs as a job.
BQ_JOB_ROLE = "roles/bigquery.jobUser"
#: APIs that must be enabled in the service account's home project. Calls to these are charged to
#: the caller's own project, not the audited one, so a disabled API there fails every run.
HOME_APIS = ("cloudresourcemanager.googleapis.com", "cloudbilling.googleapis.com",
             "serviceusage.googleapis.com")

DEFAULT_NAME = "gca-audit"
DEFAULT_SCRIPT = Path("gca-setup.sh")

ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
SA_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
SA_EMAIL_RE = re.compile(r"^([a-z][a-z0-9-]{4,28}[a-z0-9])@([a-z][a-z0-9-]{4,28}[a-z0-9])"
                         r"\.iam\.gserviceaccount\.com$")


class SetupError(ValueError):
    """An input that would not produce a correct script."""


@dataclass(frozen=True)
class Plan:
    account: str            # the user's own login, which runs every gcloud command
    sa_name: str            # service account id, the part before `@`
    home_project: str       # the project the service account lives in
    bq_jobs: bool = False

    @property
    def sa_email(self) -> str:
        return f"{self.sa_name}@{self.home_project}.iam.gserviceaccount.com"

    @property
    def roles(self) -> tuple[str, ...]:
        return ROLES + ((BQ_JOB_ROLE,) if self.bq_jobs else ())


def check_account(value: str) -> str:
    value = value.strip()
    if not ACCOUNT_RE.match(value):
        raise SetupError(f"{value!r} is not an email address")
    if value.endswith(".iam.gserviceaccount.com"):
        raise SetupError(f"{value!r} is a service account; give your own Google account here")
    return value


def check_project(value: str) -> str:
    value = value.strip()
    if not model.PROJECT_ID_RE.match(value):
        raise SetupError(f"{value!r} is not a project id (6-30 characters: a-z, 0-9 and -)")
    return value


def check_sa_name(value: str) -> str:
    value = value.strip()
    if not SA_NAME_RE.match(value):
        raise SetupError(f"{value!r} is not a service account name (6-30 characters: a-z, 0-9 "
                         "and -, starting with a letter)")
    return value


def parse_sa_email(value: str) -> tuple[str, str]:
    """`name@project.iam.gserviceaccount.com` -> (name, project)."""
    match = SA_EMAIL_RE.match(value.strip())
    if not match:
        raise SetupError(f"{value.strip()!r} is not a service account email "
                         "(NAME@PROJECT.iam.gserviceaccount.com)")
    return match.group(1), match.group(2)


def render(plan: Plan, *, today: _dt.date | None = None) -> str:
    """The setup script for this plan."""
    values = {
        "@@DATE@@": (today or _dt.date.today()).isoformat(),
        "@@ACCOUNT@@": shlex.quote(check_account(plan.account)),
        "@@SA_NAME@@": shlex.quote(check_sa_name(plan.sa_name)),
        "@@HOME_PROJECT@@": shlex.quote(check_project(plan.home_project)),
        "@@ROLES@@": " ".join(shlex.quote(r) for r in plan.roles),
        "@@HOME_APIS@@": " ".join(shlex.quote(a) for a in HOME_APIS),
    }
    text = SCRIPT
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def write(plan: Plan, path: Path, *, force: bool = False) -> Path:
    if path.exists() and not force:
        raise SetupError(f"{path} already exists; pass --force to replace it")
    path.write_text(render(plan), encoding="utf-8")
    path.chmod(0o755)
    return path


SCRIPT = r"""#!/usr/bin/env bash
# Sets up the gca auditor service account. Written by `gca setup` on @@DATE@@.
# gca sent nothing to Google to make this file. Read it before you run it: every command below
# runs as YOUR account, and `create`, `key`, `impersonate` and `grant --apply` change IAM.
#
#   ./gca-setup.sh create          create the service account (skipped if it exists) and
#                                  enable the APIs gca needs in its home project
#   ./gca-setup.sh key             create a key file for it (some organizations forbid this)
#   ./gca-setup.sh impersonate     or: let your account act as it, instead of a key
#   ./gca-setup.sh find            list projects where you can grant roles -> grantable.txt
#   ./gca-setup.sh grant           dry run: print the grant commands for grantable.txt
#   ./gca-setup.sh grant --apply   make the grants
#
# Then: gca guard-check, and gca table --all -o reports/
set -euo pipefail

ACCOUNT=@@ACCOUNT@@
SA_NAME=@@SA_NAME@@
HOME_PROJECT=@@HOME_PROJECT@@
SA="$SA_NAME@$HOME_PROJECT.iam.gserviceaccount.com"
ROLES=(@@ROLES@@)
HOME_APIS=(@@HOME_APIS@@)
LIST="${LIST:-grantable.txt}"
KEY_FILE="${KEY_FILE:-$HOME/.gca/$SA_NAME.json}"

gc() { gcloud --account="$ACCOUNT" "$@"; }

require_login() {
  if [[ -z "$(gcloud auth list --filter="account:$ACCOUNT" --format='value(account)' 2>/dev/null)" ]]; then
    echo "gcloud has no login for $ACCOUNT. Run: gcloud auth login $ACCOUNT" >&2
    exit 1
  fi
}

cmd_create() {
  if gc iam service-accounts describe "$SA" --project="$HOME_PROJECT" >/dev/null 2>&1; then
    echo "exists: $SA"
  else
    gc iam service-accounts create "$SA_NAME" --project="$HOME_PROJECT" \
      --display-name="gca read-only cost auditor"
    echo "created: $SA"
  fi
  # gca's calls to these APIs are charged to the service account's own project.
  gc services enable "${HOME_APIS[@]}" --project="$HOME_PROJECT"
  echo "enabled in $HOME_PROJECT: ${HOME_APIS[*]}"
}

cmd_key() {
  if [[ -e "$KEY_FILE" ]]; then
    echo "$KEY_FILE already exists; set KEY_FILE=... to write somewhere else" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$KEY_FILE")"
  chmod 700 "$(dirname "$KEY_FILE")"
  gc iam service-accounts keys create "$KEY_FILE" --iam-account="$SA" --project="$HOME_PROJECT"
  chmod 600 "$KEY_FILE"
  echo
  echo "Key written. Keep it out of version control, then:"
  echo "  export GOOGLE_APPLICATION_CREDENTIALS=\"$KEY_FILE\""
}

cmd_impersonate() {
  gc iam service-accounts add-iam-policy-binding "$SA" --project="$HOME_PROJECT" \
    --member="user:$ACCOUNT" --role=roles/iam.serviceAccountTokenCreator --condition=None \
    --quiet >/dev/null
  echo "$ACCOUNT may now act as $SA. Then run:"
  echo "  gcloud auth application-default login --impersonate-service-account=$SA"
  echo "  unset GOOGLE_APPLICATION_CREDENTIALS"
  echo "The grant can take a minute or two to take effect."
}

cmd_find() {
  local total failed
  token=$(gcloud auth print-access-token "$ACCOUNT")
  work=$(mktemp -d)
  export token work
  total=$(gc projects list --filter='lifecycleState=ACTIVE' --format='value(projectId)' | tee "$work/all" | wc -l | tr -d ' ')
  echo "checking $total projects as $ACCOUNT (a few minutes: Google rate-limits these checks) ..." >&2
  # One check per project. A 429 (rate limit) or 5xx is retried with a growing wait; any other
  # failure, or a check still failing after the retries, goes to $LIST.errors, never to "no".
  xargs -P 8 -n 1 sh -c '
    p=$1 n=0
    while :; do
      code=$(curl -s -o "$work/r.$p" -w "%{http_code}" -X POST \
        -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
        -d "{\"permissions\":[\"resourcemanager.projects.setIamPolicy\"]}" \
        "https://cloudresourcemanager.googleapis.com/v1/projects/${p}:testIamPermissions")
      case "$code" in
        200) grep -q setIamPolicy "$work/r.$p" && echo "$p" >> "$work/yes"; break ;;
        429|500|502|503|504)
          n=$((n + 1))
          [ "$n" -ge 8 ] && { echo "$code $p" >> "$work/errors"; break; }
          sleep $((n * 5)) ;;
        *) echo "$code $p" >> "$work/errors"; break ;;
      esac
    done' _ < "$work/all"
  {
    echo "# Projects where $ACCOUNT can grant roles (found $(date -u +%Y-%m-%dT%H:%MZ))."
    echo "# Delete any line you do not want audited, then run: $0 grant"
    sort "$work/yes" 2>/dev/null || true
  } > "$LIST"
  failed=0
  if [[ -s "$work/errors" ]]; then
    sort "$work/errors" > "$LIST.errors"
    failed=$(wc -l < "$LIST.errors" | tr -d ' ')
  else
    rm -f "$LIST.errors"
  fi
  rm -rf "$work"
  echo "$(grep -cv '^#' "$LIST" || true) of $total written to $LIST" >&2
  if [[ "$failed" != 0 ]]; then
    echo "$failed projects could not be checked (HTTP status and id in $LIST.errors); run find again" >&2
    return 1
  fi
}

cmd_grant() {
  local apply="${1:-}" project role failed=0
  [[ -f "$LIST" ]] || { echo "no $LIST; run: $0 find" >&2; exit 1; }
  echo "service account: $SA" >&2
  echo "roles: ${ROLES[*]}" >&2
  echo "projects: $(grep -cv '^\s*\(#\|$\)' "$LIST" || true)" >&2
  while read -r project; do
    [[ -z "$project" || "$project" == \#* ]] && continue
    for role in "${ROLES[@]}"; do
      cmd=(gcloud --account="$ACCOUNT" projects add-iam-policy-binding "$project"
           "--member=serviceAccount:$SA" "--role=$role" --condition=None --quiet)
      if [[ "$apply" == "--apply" ]]; then
        if "${cmd[@]}" </dev/null >/dev/null 2>&1; then
          echo "ok    $project $role"
        else
          echo "FAIL  $project $role" >&2
          failed=1
        fi
      else
        echo "${cmd[*]}"
      fi
    done
  done < "$LIST"
  [[ "$apply" == "--apply" ]] || echo "(dry run; add --apply to make these grants)" >&2
  return "$failed"
}

case "${1:-}" in
  create)      require_login; cmd_create ;;
  key)         require_login; cmd_key ;;
  impersonate) require_login; cmd_impersonate ;;
  find)        require_login; cmd_find ;;
  grant)       require_login; cmd_grant "${2:-}" ;;
  *)           sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
"""
