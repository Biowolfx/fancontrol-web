# Refactoring Plan: Audit Findings Fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task.

**Goal:** Fix all code quality issues from comprehensive audit without breaking functionality

**Architecture:** Phased approach — each phase is a separate commit. No behavior changes, only structural improvements.

**Tech Stack:** Python/Flask/SocketIO backend, Vanilla JS/Tailwind frontend, SQLite telemetry

---

## Phase 1: Instant Fixes (no behavior change)

- [ ] Sync version strings across app.py, index.html, main.js
- [ ] Remove dead `updateFanListStatus()` in main.js
- [ ] Remove duplicate block in `checkForUpdates` (lines 1764-1773)
- [ ] Remove duplicate i18n keys in en.json and ru.json
- [ ] Remove unnecessary mkdir in Dockerfile

## Phase 2: Constants Extraction

- [ ] Extract Python magic numbers to module-level constants
- [ ] Extract JS magic numbers to module-level constants

## Phase 3: JS Deduplication

- [ ] Extract ACTIVE_CLASS/INACTIVE_CLASS to module constants
- [ ] Extract button CSS strings to constants
- [ ] Add show()/hide()/toggle() abstractions
- [ ] Extract setDiscoverButtonState()

## Phase 4: Python Deduplication

- [ ] Merge set_pwm_raw + set_pwm
- [ ] Extract _update_fan_state() helper

## Phase 5: Function Decomposition

- [ ] Break loop() into sub-functions
- [ ] Break test_fans() into sub-functions
- [ ] Break discover_fans_and_sensors() into sub-functions

## Phase 6: Performance Optimizations

- [ ] Cache get_state() snapshot
- [ ] Debounce save_config()
- [ ] SQLite connection pooling

## Phase 7: Frontend Polish

- [ ] Clean up i18n fallback pattern
- [ ] Cache settings in formatTemp()
