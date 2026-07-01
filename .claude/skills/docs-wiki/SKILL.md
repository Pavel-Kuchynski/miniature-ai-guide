---
name: docs-wiki
description: Use this skill whenever you need to create, read, review, or update project documentation for miniature-ai-guide. This project has no Confluence/Notion — all documentation (architecture notes, design decisions, module docs, planning) lives in the GitHub Wiki, not in docs/*.md inside the main repo. Trigger on requests like "update the docs", "write documentation for X", "review the wiki", "add an architecture page", "document this module".
---

# Working with project documentation (GitHub Wiki)

## Key fact: the wiki is a separate git repository

This project stores all documentation in the **GitHub Wiki**, not in Confluence, Notion, or (primarily) in `docs/*.md` inside the main repo. The `docs/` folder in the main repo only holds early raw sketches (e.g. `docs/globalIdea.md`, `docs/progect_structure.md`) that predate this setup — treat the wiki as the source of truth going forward.

The GitHub Wiki is backed by its own independent git repository, separate from the main project repo:

- Main repo: `https://github.com/Pavel-Kuchynski/miniature-ai-guide.git`
- Wiki repo: `https://github.com/Pavel-Kuchynski/miniature-ai-guide.wiki.git`

Locally, the wiki repo is cloned as a **sibling folder** next to the main project, not nested inside it:

```
C:\programm\miniature-ai-guide         <- main repo (code)
C:\programm\miniature-ai-guide.wiki    <- wiki repo (documentation)
```

Do not look for documentation only inside the main repo's `docs/` folder — always check `C:\programm\miniature-ai-guide.wiki` first. If that folder is missing, clone it:

```bash
cd C:\programm
git clone https://github.com/Pavel-Kuchynski/miniature-ai-guide.wiki.git
```

## Workflow

1. **Before reading or editing docs**, `cd` into `C:\programm\miniature-ai-guide.wiki` and run `git pull` to get the latest pages — someone (human or another agent) may have edited the wiki directly on GitHub.
2. Pages are plain `.md` files at the root of that repo (e.g. `Home.md`, `Architecture.md`). The wiki's entry point is `Home.md`.
3. Cross-link pages using wiki-link syntax `[[Page Name]]`, matching what's already used in `Home.md`.
4. After creating or editing pages, commit and push like a normal git repo:
   ```bash
   git add <file>.md
   git commit -m "<describe the doc change>"
   git push origin master
   ```
5. There is no PR/review flow for the wiki (GitHub wikis don't support PRs) — pushes to `master` go live immediately. Be careful and accurate; do not push half-finished pages.
6. Keep `Home.md` as the index: when adding a new page, add a link to it from `Home.md` (or from the relevant parent page) so it's discoverable.

## Related: issues

Task tracking (issues) for this project also lives on GitHub, managed via the `gh` CLI (already installed and authenticated for this machine):

```bash
gh issue list --repo Pavel-Kuchynski/miniature-ai-guide
gh issue view <number> --repo Pavel-Kuchynski/miniature-ai-guide
gh issue create --repo Pavel-Kuchynski/miniature-ai-guide --title "..." --body "..."
```

When documentation work is driven by or should update an issue, cross-reference the issue number in the wiki page and vice versa.
