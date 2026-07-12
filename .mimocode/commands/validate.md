---
description: "Quick syntax validation of all modified Python and JS files"
---

# /validate

Validate syntax of all recently modified files in the project.

## Implementation

1. Find modified files: `git diff --name-only HEAD~1` or check `git status`
2. For `.py` files: `python3 -c "import py_compile; py_compile.compile(f, doraise=True)"`
3. For `.js` files in `templates/js/`: `npx acorn --ecma2020 --module <file>`
4. Report pass/fail for each file
5. If any file fails, show the error and suggest fix
