# Gridstack.js Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task.

**Goal:** Replace custom card drag/drop/resize with gridstack.js — battle-tested grid dashboard library.

**Architecture:** Add gridstack.js via CDN. Replace canvas CSS Grid with gridstack init. Cards rendered as gridstack widgets with HTML content. Position changes serialized back to existing card data format. Remove ~300 lines of custom drag/drop/resize/preview code.

**Tech Stack:** gridstack.js 10.x (CDN), existing Flask+SocketIO backend unchanged.

---

### Task 1: Add gridstack CDN to index.html

**Files:** `templates/index.html`

- [ ] Add gridstack CSS before `</head>` and gridstack JS before `</body>`
- [ ] Remove `position: relative` from canvas inline style (gridstack handles it)
- [ ] Bump version to 3.5.57

### Task 2: Initialize gridstack in main.js

**Files:** `templates/js/main.js`

- [ ] Add `initGridstack()` function called from `loadPickerCards()`
- [ ] Gridstack config: column=12, cellHeight=100, gap=8, float=false, disableOneColumnMode=true
- [ ] Store grid instance as `_grid` global

### Task 3: Rewrite renderPickerCard to use gridstack

**Files:** `templates/js/main.js`

- [ ] Remove all custom drag/drop handlers (onCardMouseDown, onCardMouseMove, onCardMouseUp, clone logic)
- [ ] Remove custom resize handlers (onCardResizeStart/Move/End)
- [ ] Render card HTML as gridstack widget content
- [ ] grid.addWidget({ id, x: col-1, y: row-1, w: colSpan, h: rowSpan, content: cardHtml })
- [ ] Attach resize/move event listeners via gridstack callbacks

### Task 4: Wire gridstack events to card data persistence

**Files:** `templates/js/main.js`

- [ ] grid.on('removed') — remove card from getPickerCards()
- [ ] grid.on('change') — update col/row/colSpan/rowSpan in getPickerCards()
- [ ] Keep existing setPickerCards/save flow

### Task 5: Handle groups with gridstack

**Files:** `templates/js/main.js`

- [ ] Groups remain as full-width gridstack widgets (w=12)
- [ ] Cards inside groups stay as DOM children of group element (not gridstack items)
- [ ] Group drag/reorder handled by gridstack

### Task 6: Remove dead code

**Files:** `templates/js/main.js`

- [ ] Remove: onCardMouseDown, onCardMouseMove, onCardMouseUp, _cardMouseDown, _cardDragClone, isCellOccupied, findFreePosition, getDragAfterElement, saveCardOrder, onCanvasCardDragOver, onCanvasCardDrop, _cardDropPreview, getGridCell, findNextPosition (if unused)
- [ ] Remove: card-resize-handle CSS, resize-related CSS from index.html
- [ ] Keep: updateCanvasColumns → becomes gridstack column update on resize

### Task 7: Preserve all non-grid functionality

**Files:** `templates/js/main.js`

- [ ] Live data updates (startPickerLiveUpdate) — unchanged
- [ ] SMART modal — unchanged
- [ ] Card config (⚙) — unchanged
- [ ] Card edit (✎) — unchanged
- [ ] Card remove (×) — via grid.removeWidget()
- [ ] Dashboard save/load — unchanged (setPickerCards/getPickerCards)
- [ ] Card picker modal — unchanged
- [ ] Update gridstack on window resize

### Task 8: Verify and commit

- [ ] Syntax check JS
- [ ] Version bump to 3.5.57
- [ ] Commit and push
