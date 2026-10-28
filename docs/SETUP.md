# Setup

`gca` runs as a service account with read-only roles in the projects you audit. It refuses any
token whose OAuth scopes go beyond five read-only ones ([`SAFETY.md`](SAFETY.md)). `gca` never
creates the account or grants roles itself: you run those commands, once, under your own login.

## Quick start: `gca setup`

`gca setup` asks for your Google account and the service account (new or existing), then writes a
script of `gcloud` commands, `gca-setup.sh`. `gca` sends nothing; you read the script and run it.

```bash
gca setup                              # answer the prompts; writes ./gca-setup.sh
./gca-setup.sh create                  # create the service account if needed; enable its home APIs
./gca-setup.sh key                     # create a key in ~/.gca/ (or: impersonate, see below)
./gca-setup.sh find                    # list projects where you can grant roles -> grantable.txt
# edit grantable.txt: delete every project you do not want audited
./gca-setup.sh grant                   # dry run: print the grant commands
./gca-setup.sh grant --apply           # grant the roles in section 2, project by project
export GOOGLE_APPLICATION_CREDENTIALS=~/.gca/gca-audit.json
gca table --all -o reports/
```

Every command in the script runs as the account you named (`gcloud --account=...`), so log in with
`gcloud auth login <you>` first. Flags skip the prompts: `gca setup --account you@example.org
--service-account gca-audit@home-project.iam.gserviceaccount.com`.

`find` asks Google, for each active project you can see, whether you hold
`resourcemanager.projects.setIamPolicy` there. Owner and Project IAM Admin have it; **Editor does
not**, so Editor alone is not enough to grant roles. Access through groups and folders counts.
With about 1,000 projects, `find` takes a minute or two.

The rest of this page is what the script does, step by step, for anyone who prefers to run the
commands by hand.

## 1. Create the service account

```bash
gcloud iam service-accounts create gca-audit \
  --project=<HOME-PROJECT> --display-name="read-only cost auditor"
SA=gca-audit@<HOME-PROJECT>.iam.gserviceaccount.com
```

The service account's home project must have the **Cloud Resource Manager** and **Cloud Billing**
APIs enabled (and Service Usage, which is usually on). Google charges `gca`'s calls to those APIs to
the caller's own project, not to the project being audited, so if one is off there, every run fails:
named projects are reported as unconfirmed, with "API has not been used in project … or it is
disabled". `./gca-setup.sh create` enables them; by hand:

```bash
gcloud services enable cloudresourcemanager.googleapis.com cloudbilling.googleapis.com \
  serviceusage.googleapis.com --project=<HOME-PROJECT>
```

## 2. Grant roles in each project

Grant roles on **each project**, never on a folder or the organization.

| role | needed for |
|---|---|
| `roles/compute.viewer` | VMs, disks, images, snapshots, machine types and reserved IP addresses. It also includes `resourcemanager.projects.get` and `list` and `serviceusage.services.list`, which `gca` uses to confirm the project, find it with `--all`, and list its enabled APIs. |
| `roles/storage.viewer` | listing buckets |
| `roles/monitoring.viewer` | the bucket size metric (`storage/total_bytes`) |
| `roles/bigquery.metadataViewer` | datasets, tables, and the `TABLE_STORAGE` view |
| `roles/bigquery.jobUser` | `--read-bq-storage`, because the `TABLE_STORAGE` query runs as a job billed to the project. Without it, every table in the project is `UNSIZED`. It lets the account run any query job in the project, billed there; drop it from the script's `ROLES` if that is unwanted, and run that project without `--read-bq-storage`. |

```bash
for ROLE in compute.viewer storage.viewer monitoring.viewer bigquery.metadataViewer bigquery.jobUser; do
  gcloud projects add-iam-policy-binding <TARGET-PROJECT> \
    --member="serviceAccount:$SA" --role="roles/$ROLE"
done
```

Price lists come from the Cloud Billing Catalog, which needs no role. Google's
[access-control page](https://cloud.google.com/billing/docs/access-control) says: "No permissions are
required to use the Cloud Billing Catalog API (Services list and SKUs list)." It does need the
`cloud-billing.readonly` scope, which `gca` requests itself.

Two optional roles:

* **`roles/storage.objectViewer`**, for checking empty buckets. When the size metric reports nothing
  for a bucket, `gca` lists one object to tell an empty bucket from an unmeasured one. That needs
  `storage.objects.list`, which `roles/storage.viewer` does not include
  ([Storage roles](https://cloud.google.com/storage/docs/access-control/iam-roles)). The catch is
  that `roles/storage.objectViewer` can also read object contents. Without it, such buckets stay
  `UNSIZED` and `README.txt` says why.
* **`roles/browser`: not needed, and best avoided.** `roles/compute.viewer` already includes the
  project permissions `gca` needs ([Compute roles](https://cloud.google.com/compute/docs/access/iam)),
  and `roles/browser` also includes `resourcemanager.projects.getIamPolicy`
  ([Browser role](https://cloud.google.com/iam/docs/roles-permissions/browser)), which would let the
  account see who has access to each project.

**Do not grant** `roles/viewer`, `roles/editor`, `roles/owner`, or any `*.admin` role.

Google changes what roles contain from time to time. To check a role before you grant it:

```bash
gcloud iam roles describe roles/compute.viewer --format='json(includedPermissions)' \
  | python3 -c "import json,sys; p=json.load(sys.stdin)['includedPermissions']; \
print(len(p), 'permissions;', 'compute.instances.list' in p)"
```

## 3. Give `gca` the credential

`gca` uses Application Default Credentials. With a service-account key:

```bash
gcloud iam service-accounts keys create gcp-audit-key.json --iam-account=$SA
export GOOGLE_APPLICATION_CREDENTIALS="$PWD/gcp-audit-key.json"
gca guard-check          # prints the allowlist and scopes; sends nothing
```

A key-based token carries exactly the five scopes `gca` asks for. Keep the key file out of
version control.

If your organization blocks key creation, impersonate the service account instead; no key file is
needed. Your own account needs `roles/iam.serviceAccountTokenCreator` on the service account
(`./gca-setup.sh impersonate` grants it). Then:

```bash
gcloud auth application-default login --impersonate-service-account=$SA
unset GOOGLE_APPLICATION_CREDENTIALS
```

The login page asks for `cloud-platform`, but the impersonated token `gca` gets carries only the
five scopes it asks for, so layer 1 accepts it (tested 2026-10-28). A new token-creator grant can
take a minute or two to take effect; until then the run fails with "Permission
'iam.serviceAccounts.getAccessToken' denied". `gcloud auth application-default revoke` undoes the
login.

Your personal `gcloud auth application-default login`, without impersonation, carries the broad
`cloud-platform` scope, so `gca` refuses it, as intended.

## 4. Try one project first

```bash
gca table --project <TARGET-PROJECT> -o /tmp/gca-canary
```

Read `/tmp/gca-canary/README.txt` before auditing more projects:

* the `layer1 = …` line names the account that actually ran;
* the list of failed reads gives the API's reason for each. A missing role, a missing scope and a
  disabled API each produce a different reason.

Use `--read-bq-storage` only on projects whose owners have agreed to pay for the query. The cost is
small: one query per dataset location, billed at BigQuery's 10 MB on-demand minimum, and each query
is capped at 100 MiB billed.

`gca table --all` covers every project where the service account has
`resourcemanager.projects.get`. In practice that means the projects where you granted roles, so
the audit reaches exactly as far as its grants.
