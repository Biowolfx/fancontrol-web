---
description: "Validate, version bump, commit, and push — full deployment cycle"
---

# /deploy

Complete deployment cycle: validate → bump → commit → push.

## Implementation

1. **Validate** all modified files (Python + JS syntax check)
2. **Bump** CONFIG_VERSION in `core/state.py`:
   - PATCH: bugfixes, dead code, small improvements
   - MINOR: new features, protocol changes
3. **Commit** with MiMoCode author:
   ```
   git add -A
   GIT_AUTHOR_NAME="MiMoCode" GIT_AUTHOR_EMAIL="mimocode@local" \
   GIT_COMMITTER_NAME="MiMoCode" GIT_COMMITTER_EMAIL="mimocode@local" \
   git commit -m "type: description"
   ```
4. **Push** to origin/main
5. **Verify** the push succeeded

## Post-deploy
After push, if user requests: trigger server update via `POST /api/update/apply` and monitor agent updates.
