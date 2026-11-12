---
name: python-task-pipeline
description: Orchestrates a full Python task end to end with subagents. The python-developer agent implements the change and its tests, the tests are run, the python-code-reviewer agent reviews the result, and python-developer fixes the findings, with a bounded number of review rounds. Use this skill whenever the user asks to implement, add, build or change Python code in backend/ (a new Lambda, a new function, a bug fix, a refactor) and wants it done and checked without managing agents by hand, e.g. "implement X in backend/paint and make sure it's reviewed", "add a new Lambda for Y end to end", "сделай задачу целиком: код, тесты, ревью". For review-and-fix of existing code only, use python-review-and-fix instead.
---
# Python task pipeline

You are the orchestrator. You do not write the code or the review yourself. You brief the agents, pass artifacts between them, run the tests, and decide when the work is done. Keeping the roles separate means the reviewer never sees the developer's reasoning, only the result, so its findings are independent.

```
task -> [1 plan] -> [2 develop] -> [3 test] -> [4 review] -> [5 fix] -> back to 3 (max 2 rounds) -> [6 report]
        you          python-developer  you        python-code-reviewer  python-developer
```

## Step 1: Plan and scope

1. Restate the task in two or three sentences and identify the target module (`backend/<name>/`) and the files likely to change. Read the existing module and its tests first, since the agents start with no context.
2. Write a task brief to hand over:
   - the goal and acceptance criteria (what behavior must exist, what must not change);
   - target paths;
   - the conventions to read, as paths, because subagents do not inherit skills: `CLAUDE.md`, `.claude/skills/python-engineering-style/SKILL.md` (and its `references/` file for the area, e.g. `sqs-lambda.md`), `.claude/skills/code-documentation/SKILL.md`, `.claude/skills/python-unit-testing/SKILL.md`;
   - the module's test command (see `test-execution`).
3. If the task is ambiguous in a way that changes the design (which queue, which status, public contract), ask the user before developing. Otherwise state your assumptions in the brief and proceed.

## Step 2: Develop

Launch the `python-developer` agent with the brief. Require it to:
- implement only what the brief asks, preserving existing behavior and signatures unless the brief says otherwise;
- add or update tests for every changed behavior, following `python-unit-testing`;
- update docstrings and the module `README.md` in the same change (`code-documentation`);
- report the files changed and the tests added, and list any assumptions.

## Step 3: Test

Run the module's tests yourself using the `test-execution` skill (framework detection, non-zero test count). Do not trust the agent's claim that tests pass. If tests fail, send the failing output back to `python-developer` for a fix and re-run. Stop after two failed fix attempts and report the failure to the user instead of looping.

## Step 4: Review

Launch the `python-code-reviewer` agent on the changed files only (`git diff` plus untracked files in the module). Give it the same convention paths and ask for findings with location, severity, issue and concrete fix, plus a verdict. It should cover correctness, security, tests, and the three style areas from `python-review-and-fix` (style, architecture, narrating comments).

## Step 5: Consolidate and fix

Turn the review into a numbered table (`# | Severity | Category | Location | Issue | Proposed fix`), merge duplicates, and drop findings that contradict repo conventions. Show it to the user.

- Critical and Major findings are fixed automatically; this is what the user asked for by choosing a full pipeline.
- Minor and Nitpick findings are listed in the report but not fixed unless the user asks, to keep the diff focused.
- Findings you are unsure about are marked "needs decision" and left for the user.

Send the approved rows (the table, not a summary) to `python-developer` with the same constraints: fix only these, keep behavior, update docs and tests. Then return to Step 3.

Run at most two review rounds. If Critical or Major findings remain after the second round, stop and report them.

## Step 6: Report

Finish with a short report:
- what was implemented (files and behavior);
- test command and result (counts);
- findings fixed, left open, and "needs decision";
- assumptions made and anything that needs the user's attention.

Never say the work is done if tests fail or Critical/Major findings remain. State it plainly with the output. Do not commit or push unless the user asks.
