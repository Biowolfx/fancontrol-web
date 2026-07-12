---
name: validate-and-push
description: "Validate syntax of modified files (Python + JS), bump CONFIG_VERSION, commit with MiMoCode author, and push to origin/main. Use after completing any set of code changes."
---

# Validate and Push

Use this skill after making code changes that need to be committed.

## Steps

1. **Validate Python syntax** of all modified `.py` files:
```bash
python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in MODIFIED_PY_FILES]; print('OK')"
```

2. **Validate JS syntax** of modified `.js` files (if any):
```bash
npx acorn --ecma2020 --module templates/js/main.js > /dev/null 2>&1 && echo "OK" || echo "FAIL"
```

3. **Bump version** in `core/state.py` — `CONFIG_VERSION = "X.Y.Z"`
   - PATCH (3.12.x → 3.12.x+1): bugfix, dead code removal, small improvements
   - MINOR (3.12.x → 3.13.0): new features, protocol changes, breaking changes

4. **Commit and push**:
```bash
git add -A && \
GIT_AUTHOR_NAME="MiMoCode" GIT_AUTHOR_EMAIL="mimocode@local" \
GIT_COMMITTER_NAME="MiMoCode" GIT_COMMITTER_EMAIL="mimocode@local" \
git commit -m "descriptive message" && \
git push origin main
```

## Rules
- ALWAYS validate before committing
- ALWAYS include version bump in the commit
- NEVER push without validation passing
- Write commit messages in English, imperative mood
- Group related changes into one commit when possible
