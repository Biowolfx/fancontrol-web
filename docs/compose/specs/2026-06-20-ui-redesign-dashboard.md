# UI Redesign: Node Tree + Custom Dashboard

## [S1] Problem

Current multi-node UI is not informative:
- Node list in sidebar shows only names, no context
- Nodes view requires clicking into each node to see details
- No way to see elements from multiple servers on one screen
- No custom monitoring view

## [S2] Solution Overview

Two major changes:
1. **Variant D: Split Panel with Node Tree** — Left panel becomes a tree of nodes with expandable fan/sensor lists. Right panel is the inspector for selected items.
2. **Custom Dashboard** — A canvas-based monitoring screen where users add cards (fans, sensors, disks, system metrics) from any server, resize and group them freely. Empty by default.

## [S3] Navigation Structure

### Tab System

Three tabs in left panel header:
- **Dashboard** — Custom monitoring canvas (empty by default)
- **Nodes** — Variant D tree view (all servers with fans/sensors/disks)
- **Settings** — Current settings panel

### Left Panel: Node Tree (Nodes Tab)

```
┌──────────────────┐
│ FanControl  v3.4 │
│ [Dashboard][Nodes]│
│ ──────────────── │
│ 🖥 My Server  ▼  │
│  ├ 🌀 Fan 1     │
│  ├ 🌀 Fan 2     │
│  ├ 🌀 Fan 3     │
│  ├ 🌡 CPU: 45°C │
│  ├ 🌡 Sys: 38°C │
│  └ 💾 4 disks   │
│ ──────────────── │
│ 🖥 Server 2   ▼  │
│  ├ 🌀 Fan A     │
│  ├ 🌀 Fan B     │
│  └ 🌡 CPU: 52°C │
│ ──────────────── │
│ 🖥 Server 3   ▼  │
│  └ ⚠ offline    │
│                  │
│ + Add Node       │
└──────────────────┘
```

Tree behavior:
- Click node name → expand/collapse
- Click fan → show inspector on right
- Click sensor → show sensor detail on right
- Online/offline indicator per node
- Fan count badge per node

### Right Panel: Inspector

Same as current — shows selected fan/sensor details, controls, schedule.

## [S4] Custom Dashboard

### Canvas Concept

Empty canvas with drag-and-drop cards. Users:
1. Click "+" button → picker shows available cards
2. Select card type + source server → card appears on canvas
3. Drag card to position
4. Resize by dragging corner handle
5. Create named groups by dragging cards into a group zone
6. Remove cards with "×" button

### Card Types

| Type | Content | Min Size |
|------|---------|----------|
| Fan | Name, RPM, %, mode badge, mini sparkline | 150×80 |
| Temperature | Name, value °C, color gradient | 120×60 |
| Disk | Name, temp °C, type badge | 120×60 |
| System | Metric name, value, unit | 100×60 |

### Card Layout

```
┌─────────────────────────┐
│ × Fan 1 — My Server     │
│ ┌───────────────────┐   │
│ │ 1200 RPM    45%   │   │
│ │ [sparkline graph]  │   │
│ └───────────────────┘   │
│ AUTO  ▪ nominal         │
└────────────── ⋮ ────────┘
                   resize handle
```

Card elements:
- Header: close button (×), name, source server
- Body: primary metric + secondary metric
- Footer: status badges
- Corner: resize handle

### Grouping

Users can create named groups:
1. Click "+ Group" button
2. Enter group name (e.g., "CPU Cooling", "Storage")
3. Group zone appears as a bordered area
4. Drag cards into the group zone
5. Group has its own header with name + collapse button

```
┌─── CPU Cooling ──────────────┐
│ ┌──────┐ ┌──────┐ ┌──────┐  │
│ │Fan 1 │ │Fan 2 │ │CPU   │  │
│ │1200rpm│ │800rpm │ │45°C  │  │
│ └──────┘ └──────┘ └──────┘  │
└──────────────────────────────┘

┌─── Storage ──────────────────┐
│ ┌──────┐ ┌──────┐           │
│ │sda   │ │sdb   │           │
│ │32°C  │ │35°C  │           │
│ └──────┘ └──────┘           │
└──────────────────────────────┘
```

### Card Resize

Cards support free-form resize via corner handle. No fixed sizes. Minimum constraints per card type. Resize is continuous (not snapped to grid).

### Persistence

Dashboard config stored in `config.json`:

```json
{
  "dashboard": {
    "groups": [
      {
        "id": "group-1",
        "name": "CPU Cooling",
        "x": 0, "y": 0, "w": 400, "h": 200
      }
    ],
    "cards": [
      {
        "id": "card-1",
        "type": "fan",
        "source": "local",
        "fan_id": "dev-abc123",
        "x": 10, "y": 10, "w": 200, "h": 120,
        "group_id": "group-1"
      },
      {
        "id": "card-2",
        "type": "temperature",
        "source": "server2-node-id",
        "sensor_id": "temp1",
        "x": 220, "y": 10, "w": 150, "h": 80,
        "group_id": null
      }
    ]
  }
}
```

## [S5] Card Picker

Triggered by "+" button. Modal/dropdown with:

```
┌─ Add Card ──────────────────┐
│                              │
│ Type: [Fan ▼]               │
│                              │
│ Source:                      │
│  ○ My Server (local)         │
│  ○ Server 2 (192.168.0.101)  │
│  ○ Server 3 (192.168.0.102)  │
│                              │
│ Element:                     │
│  ☐ Fan 1 (1200 RPM)         │
│  ☐ Fan 2 (800 RPM)          │
│  ☐ Fan 3 (1500 RPM)         │
│                              │
│ [Add] [Cancel]               │
└──────────────────────────────┘
```

User selects type → source → element → clicks Add → card appears on canvas.

## [S6] Data Flow

1. WebSocket `update` event pushes state for all nodes
2. Dashboard JS filters relevant data for each card
3. Card renders live data (RPM, temp, etc.)
4. Position/size changes → debounce save to config
5. Config persisted via existing `save_config()`

## [S7] Files to Modify

| File | Change |
|------|--------|
| `templates/index.html` | New tab structure, canvas container, card picker modal, group zones |
| `templates/js/main.js` | Tab switching, canvas rendering, drag/resize, card CRUD, group CRUD |
| `static/lang/en.json` | Translations for new UI elements |
| `static/lang/ru.json` | Translations for new UI elements |
| `server/routes.py` | Dashboard config save/load endpoint (or reuse existing config API) |
| `core/config.py` | Add `dashboard` to config schema |
| `core/state.py` | Add dashboard state |

## [S8] Implementation Phases

### Phase 1: Node Tree (Variant D)
- Restructure left panel with tabs
- Build tree view with expandable nodes
- Node status indicators
- Click-to-inspect flow

### Phase 2: Custom Dashboard Canvas
- Empty canvas component
- Card picker modal
- Card rendering (fan, temp, disk, system)
- Free-form drag and drop
- Free-form resize

### Phase 3: Groups
- Group creation with name
- Drag cards into groups
- Group collapse/expand
- Group persistence

### Phase 4: Polish
- Card hover effects
- Live data updates
- Empty state messaging
- Responsive layout
