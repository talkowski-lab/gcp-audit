"""cli.py — `gca table`, and the one place that joins the parts.

Three verbs, and the product is the first one:

    gca table  --project P [--project Q] [-o DIR]      the table
    gca guard-check                                     what a pass would be allowed to do
    gca setup                                           write a script that sets up the auditor

Failure paths:

* Zero projects resolved is an error, not an empty table (which would read as "you have nothing").
* A price-sheet failure prices every row `REFUSED` and still writes the CSV.
* A refusal inside one collector becomes a gap for that collector; a layer-1 scope refusal stops
  the run with exit 2.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from gcp_audit import guard, methods, model, pricing, setup_script, table
from gcp_audit import projects as projects_mod

#: APIs this version reads nothing from, named on every run so their absence is a stated gap
#: (`docs/API-EVIDENCE.md` §APIs not read).
NOT_ENUMERATED = (
    "container.googleapis.com", "cloudfunctions.googleapis.com", "run.googleapis.com",
    "pubsub.googleapis.com", "dataproc.googleapis.com", "artifactregistry.googleapis.com",
    "sqladmin.googleapis.com", "spanner.googleapis.com",
)

#: The services the collectors do read, for the diff against what a project has enabled.
COVERED = ("compute.googleapis.com", "storage.googleapis.com", "bigquery.googleapis.com",
           "monitoring.googleapis.com", "cloudbilling.googleapis.com",
           "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com")

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2

#: `(kind, state)` rows withheld from `resources.csv` unless asked for, and counted in README.txt.
#: A TERMINATED VM bills no vCPU or memory (`docs/API-EVIDENCE.md` §`Instance.status`); its disks
#: keep their own rows. `STOPPED` is deliberately not included.
OMITTED_BY_DEFAULT = frozenset({("Instance", "TERMINATED")})

#: The README line's denominator, and the read whose gap marks that count as partial.
INSTANCE_KIND = "Instance"
INSTANCE_READ = "compute.instances.aggregatedList"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gca",
        description="Read-only GCP resource, cost and last-used table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Read-only by construction: docs/SAFETY.md. Nothing here can change a cloud resource.",
    )
    sub = ap.add_subparsers(dest="verb", required=True)

    table_parser = sub.add_parser("table", help="write resources.csv for a scope")
    table_parser.add_argument("-p", "--project", action="append", default=[],
                              metavar="ID", help="project id; repeat for several")
    table_parser.add_argument("--folder", metavar="NUM",
                              help="every project under this folder id")
    table_parser.add_argument("--label", metavar="KEY=VALUE",
                              help="projects carrying this label (inside --folder if given)")
    table_parser.add_argument("--all", action="store_true",
                              help="every ACTIVE project the credential can see (inside --folder if given)")
    table_parser.add_argument("-o", "--out", type=Path, default=Path("."),
                              metavar="DIR", help="where to write (default: here)")
    table_parser.add_argument("--read-bq-storage", action="store_true",
                              help="size BigQuery tables with one INFORMATION_SCHEMA.TABLE_STORAGE "
                                   "query per dataset location, charged to the project's own "
                                   "slot time and capped at 100 MiB billed per query. It adds each table's last data write "
                                   "(storage_last_modified_time). Without it, tables are sized by "
                                   "a free per-table metadata read and dated by creationTime.")
    table_parser.add_argument("--include-terminated", action="store_true",
                              help="list Instance rows whose status is TERMINATED. By default they "
                                   "are left out of resources.csv and counted in README.txt: a VM in "
                                   "that state is billed no vCPU and no memory, so its money cell is "
                                   "what the machine costs switched on, not what it costs today. Its "
                                   "persistent disks are billed whatever its state and keep their "
                                   "own rows, and nothing in this build reads the IP addresses it "
                                   "may still hold.")
    table_parser.add_argument("--quiet", action="store_true", help="print only the file paths")
    table_parser.set_defaults(func=cmd_table)

    guard_parser = sub.add_parser("guard-check",
                                  help="print what a pass would be allowed to do; makes no request")
    guard_parser.set_defaults(func=cmd_guard_check)

    setup_parser = sub.add_parser(
        "setup", help="write a script that creates the service account and grants its roles; "
                      "sends nothing itself",
        description="Asks for your Google account and the auditor service account, then writes "
                    "a bash script of gcloud commands for you to review and run. gca itself "
                    "sends nothing and changes nothing. Prompts for anything not given as a flag.")
    setup_parser.add_argument("--account", metavar="EMAIL",
                              help="your own Google account, which runs the script's commands")
    setup_parser.add_argument("--service-account", metavar="EMAIL",
                              help="an existing (or to-be-created) auditor service account")
    setup_parser.add_argument("--create", metavar="NAME",
                              help="create a new service account with this name ...")
    setup_parser.add_argument("--home-project", metavar="ID",
                              help="... in this project")
    setup_parser.add_argument("--bq-jobs", action="store_true",
                              help="also grant roles/bigquery.jobUser, needed for --read-bq-storage")
    setup_parser.add_argument("-o", "--out", type=Path, default=setup_script.DEFAULT_SCRIPT,
                              metavar="FILE", help="where to write the script "
                                                   f"(default: {setup_script.DEFAULT_SCRIPT})")
    setup_parser.add_argument("--force", action="store_true", help="replace FILE if it exists")
    setup_parser.set_defaults(func=cmd_setup)
    return ap


def cmd_table(args: argparse.Namespace) -> int:
    refusals: dict[str, str] = {}
    try:
        resolved = projects_mod.resolve_projects(
            projects=args.project, folder=args.folder, label=args.label,
            everything=args.all, refusals=refusals)
    except guard.AuditError as exc:
        print(f"gca: refusing to run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not resolved:
        print("gca: the scope resolved to zero projects. Nothing was written — an empty table "
              "reads as 'you have nothing' and this tool does not assert that from an empty "
              "answer. Name a --project, widen the --label, or check the credential's access.",
              file=sys.stderr)
        for name, reason in sorted(refusals.items()):
            print(f"  {name}: {reason}", file=sys.stderr)
        return EXIT_REFUSED

    rates = pricing.load_rates()
    resources: list[model.Resource] = []
    #: Typed projects the cloud would not confirm, named as gaps.
    gaps: list[model.Gap] = _unconfirmed_named_projects(args.project, resolved, refusals)
    enabled: dict[str, tuple[list[str], str | None]] = {}
    for project in resolved:
        for collect in _collectors_for(project, read_bq_storage=args.read_bq_storage):
            resources.extend(collect.resources)
            gaps.extend(collect.gaps)
        names, reason = projects_mod.enabled_services(project)
        # Kept as the (names, reason) pair: "could not tell" is printed, not treated as "none on".
        enabled[project] = (names, reason)
        gaps.extend(_coverage_gaps(project, names, reason))

    # Counted before they are withheld, so README.txt can state how many are missing.
    withheld = [res for res in resources if (res.kind, res.state) in OMITTED_BY_DEFAULT]
    instances_seen = sum(1 for res in resources if res.kind == INSTANCE_KIND)
    if not args.include_terminated:
        resources = [res for res in resources if (res.kind, res.state) not in OMITTED_BY_DEFAULT]

    rows = [model.Row.of(res, *pricing.price(res, rates)) for res in resources]
    rows.sort(key=lambda r: (r.project, r.service, r.kind, r.item))

    meta = {
        "run_utc": model.now_utc(),
        "scope": _describe_scope(args),
        "projects": resolved,
        "layer1": guard.layer1_line(),
        "stats": guard.stats(),
        "gaps": gaps,
        "not_enumerated": list(NOT_ENUMERATED),
        "services": enabled,
        "reads": methods.READ_COUNT,
        "bq_storage": bool(args.read_bq_storage),
        # All four keys are required by `table._terminated_line`.
        "terminated_instances": {
            "collected": instances_seen,
            "terminated": len(withheld),
            "listed": bool(args.include_terminated),
            "read_incomplete": any(gap.read == INSTANCE_READ for gap in gaps),
        },
    }
    written = table.write_bundle(args.out, rows, meta=meta)

    for path in written:
        print(path)
    if args.quiet:
        return EXIT_OK
    unpriced = sum(1 for r in rows if r.amount_state != model.MEASURED)
    undated = sum(1 for r in rows if not r.last_activity)
    print(f"{len(rows)} resources across {len(resolved)} project(s); "
          f"{unpriced} with no honest monthly cost, {undated} with no date anyone read.")
    if withheld and not args.include_terminated:
        print(f"  {len(withheld)} TERMINATED Instance row(s) are not in resources.csv — a stopped VM "
              "bills no vCPU and no memory, and its disks already have their own rows. "
              "--include-terminated lists them.")
    _print_gaps(gaps)
    print("  monthly costs are measured quantity x a dated list price, not an invoice. See README.txt.")
    return EXIT_OK


#: How many gap lines the console gets before it defers to `README.txt`.
GAP_PREVIEW = 12


def _print_gaps(gaps: list[model.Gap]) -> None:
    """A preview of the gap list. Read failures (which explain a `NA`) lead; coverage gaps follow."""
    if not gaps:
        print("  no read failed; the table is everything these services reported.")
        return
    standing = "not-read"
    ordered = sorted(gaps, key=lambda g: (g.read == standing, g.project, g.service, g.read))
    for gap in ordered[:GAP_PREVIEW]:
        print(f"  gap: {gap.project} {gap.service} {gap.read} — {gap.reason}")
    if len(gaps) > GAP_PREVIEW:
        coverage = sum(1 for g in gaps if g.read == standing)
        print(f"  ... and {len(gaps) - GAP_PREVIEW} more ({coverage} of {len(gaps)} are services "
              "this version does not enumerate), all listed in README.txt")


def _unconfirmed_named_projects(named: Sequence[str], resolved: Sequence[str],
                                refusals: dict[str, str] | None = None) -> list[model.Gap]:
    """A typed project the cloud did not confirm, as a gap that names it rather than a silence."""
    refusals = refusals or {}
    return [model.Gap(project=name, service="cloudresourcemanager.googleapis.com",
                      read="cloudresourcemanager.projects.get",
                      reason="the cloud did not answer with this project as ACTIVE, so nothing in it "
                             "was read; this is a statement about this credential's access, not "
                             "about the project being empty"
                             + (f" ({refusals[name]})" if name in refusals else "")
            ) for name in sorted(set(named) - set(resolved))]


def _collectors_for(project: str, *, read_bq_storage: bool) -> list[model.Collect]:
    """The collectors, in a fixed order. One that raises becomes a gap rather than ending the run.

    `MethodNotAllowed` is caught because collectors interpolate cloud-derived values the guard may
    refuse. The layer-1 scope refusal happens before this and still stops the run.
    """
    from gcp_audit import collect_bigquery, collect_compute, collect_storage

    calls = [lambda: collect_compute.collect_compute(project),
             lambda: collect_storage.collect_storage(project),
             lambda: collect_bigquery.collect_bigquery(project,
                                                       read_storage_info=read_bq_storage)]
    out: list[model.Collect] = []
    for call in calls:
        try:
            out.append(call())
        except (guard.AuditError, methods.MethodNotAllowed, OSError, ValueError,
                  KeyError) as exc:
            out.append(model.Collect(service="unknown", gaps=[
                model.Gap(project=project, service="unknown", read="collector",
                          reason=f"{type(exc).__name__}: {exc}")]))
    return out


def _coverage_gaps(project: str, enabled: list[str],
                   reason: str | None) -> list[model.Gap]:
    """Enabled APIs this version reads nothing from, as gaps. If the enabled list could not be
    read, one gap says so instead."""
    if reason:
        return [model.Gap(project=project, service="serviceusage.googleapis.com",
                          read="serviceusage.services.list",
                          reason=f"cannot tell which APIs are enabled: {reason}")]
    missed = sorted(s for s in enabled if s not in COVERED)
    return [model.Gap(project=project, service=s, read="not-read",
                      reason="enabled in this project and not enumerated by this version; "
                             "see docs/API-EVIDENCE.md (APIs not read) for why")
            for s in missed]


def _describe_scope(args: argparse.Namespace) -> str:
    named = ", ".join(args.project) if args.project else ""
    parts = []
    if named:
        parts.append(f"projects: {named}")
    if args.folder:
        parts.append(f"folder: {args.folder}")
    if args.label:
        parts.append(f"label: {args.label}")
    if args.all:
        parts.append("every ACTIVE project the credential can see")
    if args.read_bq_storage:
        parts.append("BigQuery region query: authorised")
    if args.include_terminated:
        parts.append("TERMINATED instances: listed")
    return "; ".join(parts) or "unspecified"


def cmd_guard_check(_: argparse.Namespace) -> int:
    """Print the policy a pass would run under. Makes no request, including to tokeninfo."""
    import os

    print(f"{methods.READ_COUNT} reads are allowlisted; anything else is refused before a socket:")
    for method_id in sorted(methods.READS):
        read = methods.READS[method_id]
        gated = methods.GATED.get(method_id)
        print(f"  {read.verb:5s} {method_id:44s} {('behind ' + gated) if gated else ''}")
    print(f"\n{len(guard.SCOPE_ALLOWLIST)} scopes admissible, all narrow; a wider grant refuses:")
    for scope in sorted(guard.SCOPE_ALLOWLIST):
        print(f"  {scope}")
    configured = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                      or Path.home().joinpath(".config/gcloud/"
                                              "application_default_credentials.json").exists())
    print("\ncredential: " + ("an application-default credential is configured; a pass verifies "
                             "its grant off tokeninfo before its first read"
                             if configured else
                             "none configured, so `gca table` refuses. It will not fall back to a "
                             "user login: layer 1 refuses a writable token on sight."))
    print("not checked by anything, and no report line may claim it was: the IAM role set, "
          "IAM bindings, and `*.testIamPermissions` (gca never calls it).")
    return EXIT_OK


def cmd_setup(args: argparse.Namespace, *, ask=input, interactive: bool | None = None) -> int:
    """Collect the plan from flags, prompting for whatever is missing, and write the script."""
    if interactive is None:
        interactive = sys.stdin.isatty()
    try:
        plan = _setup_plan(args, ask=ask, interactive=interactive)
        if args.out.exists() and not args.force:
            if not (interactive and _yes(ask(f"{args.out} exists. Replace it? [y/N]: "))):
                raise setup_script.SetupError(f"{args.out} already exists; pass --force to replace it")
        path = setup_script.write(plan, args.out, force=True)
    except setup_script.SetupError as exc:
        print(f"gca setup: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except EOFError:
        print("\ngca setup: no input; nothing written", file=sys.stderr)
        return EXIT_ERROR
    run = str(path) if path.is_absolute() or path.parent != Path(".") else f"./{path}"
    print(f"\nWrote {path}. gca sent nothing; read the script, then run:\n"
          f"  {run} create          # create {plan.sa_email} if it does not exist\n"
          f"  {run} key             # or: {run} impersonate\n"
          f"  {run} find            # projects where {plan.account} can grant roles\n"
          f"  (edit grantable.txt to remove projects you do not want audited)\n"
          f"  {run} grant           # dry run\n"
          f"  {run} grant --apply\n"
          f"  gca table --all -o reports/")
    return EXIT_OK


def _setup_plan(args: argparse.Namespace, *, ask, interactive: bool) -> setup_script.Plan:
    def need(flag: str, prompt: str, check, default: str = ""):
        if not interactive:
            raise setup_script.SetupError(f"{flag} is required when not running interactively")
        while True:
            answer = ask(prompt).strip() or default
            try:
                return check(answer)
            except setup_script.SetupError as exc:
                print(f"  {exc}", file=sys.stderr)

    if args.service_account and (args.create or args.home_project):
        raise setup_script.SetupError("give --service-account, or --create with --home-project, "
                                      "not both")
    account = (setup_script.check_account(args.account) if args.account else
               need("--account", "Your Google account (it runs the setup commands): ",
                    setup_script.check_account))
    if args.service_account:
        name, home = setup_script.parse_sa_email(args.service_account)
    elif args.create or args.home_project:
        name = setup_script.check_sa_name(args.create or setup_script.DEFAULT_NAME)
        home = (setup_script.check_project(args.home_project) if args.home_project else
                need("--home-project", "Project to create it in: ", setup_script.check_project))
    elif not interactive:
        raise setup_script.SetupError("--service-account, or --create with --home-project, is "
                                      "required when not running interactively")
    elif _yes(ask("Do you already have an auditor service account? [y/N]: ")):
        name, home = need("--service-account", "Its email: ", setup_script.parse_sa_email)
    else:
        home = need("--home-project", "Project to create it in: ", setup_script.check_project)
        name = need("--create", f"Name for it [{setup_script.DEFAULT_NAME}]: ",
                    setup_script.check_sa_name, setup_script.DEFAULT_NAME)
    bq_jobs = args.bq_jobs or (interactive and _yes(ask(
        "Also grant roles/bigquery.jobUser, needed only for --read-bq-storage? [y/N]: ")))
    return setup_script.Plan(account=account, sa_name=name, home_project=home, bq_jobs=bq_jobs)


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except methods.MethodNotAllowed as exc:
        print(f"gca: the toolkit refused itself: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except guard.ScopeRefusal as exc:
        print(f"gca: layer 1 refused this run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("\ngca: interrupted; partial files may remain in the output directory.",
              file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
