# Python task pipeline

## What it does
Orchestrates a Python task end to end with subagents: `python-developer` implements the change and
tests, tests are run, `python-code-reviewer` reviews, and `python-developer` fixes Critical and Major
findings, for at most two review rounds.

## When to use it
When you want a feature, bug fix or refactor in `backend/` implemented, tested and reviewed without
launching agents yourself. For review and fix of existing code, use `python-review-and-fix`.

## How to ask for it
"Implement X in backend/paint and make sure it is reviewed", "add a new Lambda for Y end to end".

## Prerequisites
Agents `python-developer` and `python-code-reviewer`; skills `python-engineering-style`,
`code-documentation`, `python-unit-testing`, `test-execution`.
