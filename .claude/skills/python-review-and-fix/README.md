# Python review and fix

## What it does
Runs a review-then-fix pipeline on Python code. The `python-code-reviewer` agent finds problems
(style, architecture, oversized comment blocks, plus correctness and security), the findings are
consolidated into a numbered table, and after confirmation the `python-developer` agent fixes them.

## When to use it
When asked to review, audit, clean up or refactor Python files or a backend Lambda module.

## How to ask for it
"Review backend/paint", "check this handler for problems", "find architecture issues and fix them".

## Prerequisites
Both agents read `python-engineering-style`, `code-documentation` and `python-unit-testing`.
Tests run with the module's own command from its README (`pytest` or `unittest`).
