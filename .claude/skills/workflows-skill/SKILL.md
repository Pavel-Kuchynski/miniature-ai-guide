---
name: workflows-skill
description: Create a new GitHub Actions workflow or change an existing one in .github/workflows/ by delegating to the devops subagent with this repository's workflow rules. Use this skill whenever the user asks to add a deploy workflow for a new backend Lambda, change a trigger, test step, packaging step or AWS auth in an existing workflow, add a CI job, fix a failing pipeline run, or says things like "create a workflow for backend/<module>", "update deploy_paint_lambda.yml", "add concurrency/environment to the deploy", "workflow for the frontend". Not for application code (use python-developer / javascript-developer) and not for provisioning AWS resources.
---
# Workflows skill

You are the orchestrator. You do not write the workflow yourself. You gather the facts, brief the `devops` subagent, then verify the result. The rules live in `references/` next to this file; subagents do not inherit skills, so the brief must give the agent the absolute paths of the files it has to read.

```
request -> [1 gather facts] -> [2 brief devops] -> [3 verify diff] -> [4 report]
            you                 devops subagent     you
```

## Rules index

Read the files that match the task (and pass the same paths to the agent). Paths are relative to `.claude/skills/workflows-skill/`.

| File | Covers | Read when |
|---|---|---|
| `references/lambda-deploy-workflow.md` | context (no IaC), naming, header comment, triggers, permissions/concurrency/env, test and deploy jobs, PR checks | any backend Lambda workflow, new or changed |
| `references/packaging.md` | rsync excludes, boto3 filtering, Lambda-platform wheels, compiled-dependency check | the workflow zips code or bundles dependencies |
| `references/frontend-workflow.md` | Vite build, S3 sync, CloudFront, environment gate | `deploy-frontend.yml` |
| `references/security.md` | OIDC, permissions, action pinning, injection, IAM scoping | always |

## Reference workflows

Copy conventions from the closest existing file in `.github/workflows/`:

| Need | Copy from |
|---|---|
| Plain Lambda, no bundled deps, pytest | `deploy_start_job.yml` |
| Plain Lambda, unittest | `deploy_lambda_upload.yml` |
| Bundled pure-Python deps, boto3 filtered | `deploy_bedrock_lambda.yml`, `deploy_open_connection.yml` |
| Compiled deps plus verify step | `deploy_paint_lambda.yml` |
| Static site to S3 plus CloudFront | `deploy-frontend.yml` |
| PR test matrix | `run_unit_tests.yml` |

## Step 1: Gather facts

Decide whether this is a new workflow or a change to an existing one, then collect what the agent needs. Read these yourself so the brief is concrete:

- **New backend Lambda:** `backend/<module>/` (handler, `requirements.txt`, `requirements-dev.txt`, `tests/`, `README.md`). Determine the test framework, whether dependencies must be bundled, and whether any are compiled (pydantic, Pillow, cryptography).
- **Change to an existing workflow:** the current file plus the closest sibling from the table above.
- **Pipeline failure:** the run link or log. If `gh` is available, `gh run view <id> --log-failed`.

Ask the user only for what you cannot infer: a function-name variable that deviates from `<MODULE>_FUNCTION_NAME`, whether prod needs manual approval, and any trigger other than push to `main`. Otherwise use the defaults in the references.

## Step 2: Brief the devops subagent

Launch the `devops` agent (Agent tool, `subagent_type: "devops"`). The prompt must contain:
- the task and the exact files to create or edit;
- the facts from Step 1 (module, test framework, dependencies, env vars, trigger);
- the absolute paths of the reference files to read first (at least `lambda-deploy-workflow.md` and `security.md`, plus `packaging.md` or `frontend-workflow.md` as relevant) and the sibling workflow to copy from;
- constraints: edit files only, do not commit or push, do not run mutating AWS CLI commands, report in under 200 words what changed per file and anything skipped.

## Step 3: Verify

Do not trust the summary. Run `git diff -- .github/workflows` and check against the reference files and the checklist below. Validate YAML syntax of every changed file (`python -c "import yaml; yaml.safe_load(open(f))"`). If something is off, send the agent a specific correction (SendMessage to the same agent). One correction round is normally enough.

## Step 4: Report

Two or three sentences: which files changed, anything left for the user to configure in GitHub/AWS (repo variables, IAM role permissions, Environment reviewers, Lambda env vars), and that nothing was committed.

## Checklist before reporting

- [ ] File name, `name:`, header comment and prerequisites match the module.
- [ ] Path filter includes the module directory and the workflow file.
- [ ] `permissions`, `concurrency` and `env` blocks are present and match the pattern.
- [ ] `test` gates `deploy`; the test command matches the module's framework.
- [ ] Packaging excludes tests and requirements files; boto3 and stubs are not bundled; compiled deps use the Lambda platform flags.
- [ ] OIDC auth, function name from a repo variable, `wait function-updated` present.
- [ ] No secrets, no hardcoded ARNs or names, YAML parses.
- [ ] Report lists the repo variables and AWS setup the user still has to do.
