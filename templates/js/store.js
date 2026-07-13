/**
 * FanControl Web — Centralized State Store
 * Replaces 65+ scattered global variables with a single organized object.
 */

// ============================================================================
// CORE APPLICATION STATE
// ============================================================================

export const store = {
    // Server state (updated via Socket.IO 'update' event)
    state: {},

    // UI navigation
    currentFanId: null,
    currentView: 'dashboard',
    selectedNodeId: null,
    wasOnMainScreen: false,
    currentRemoteNodeId: null,

    // Multi-node
    nodesData: [],

    // Connection
    serverAvailable: true,

    // Chart
    chart: null,
    lastChartUpdate: 0,

    // Sensors & fans
    allSensors: [],
    fanConfigs: {},

    // Wizard
    wizardStep: 'intro',
    wizardHardwareData: null,

    // PWM slider
    isDragging: false,

    // UI refresh throttle
    lastUIUpdate: 0,
};

// ============================================================================
// CONSTANTS
// ============================================================================

export const CHART_UPDATE_INTERVAL = 60000;
export const RELOAD_DELAY = 10000;
export const SCHEDULE_CELL_SIZE = 18;
export const SPARKLINE_MAX = 20;

export const BTN_ACTIVE = 'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30';
export const BTN_INACTIVE = 'bg-cyber-accent text-gray-400 border-gray-700 hover:text-white';
export const BTN_MANUAL_ACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-purple';
export const BTN_MANUAL_INACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple hover:border-neon-purple';
export const BTN_AUTO_ACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-cyan bg-opacity-20 text-neon-cyan border border-neon-cyan border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-cyan';
export const BTN_AUTO_INACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan';

// ============================================================================
// PERSISTENT SETTINGS (localStorage)
// ============================================================================

export const settingsDefaults = {
    tempUnit: 'celsius',
    refreshInterval: 0,
    compactMode: false,
    autoUpdateCheck: 21600000,
};

export const settings = {
    _cache: null,
    _cacheTime: 0,
    CACHE_TTL: 1000,
};

// ============================================================================
// I18N
// ============================================================================

export const i18n = {
    currentLang: localStorage.getItem('fancontrol_lang') || 'en',
    translations: {},
};

// ============================================================================
// SCHEDULE
// ============================================================================

export const schedule = {
    data: {},
    selection: [],
    isDragging: false,
    dragStartCell: null,
    editingCells: [],
    editorSensors: [],
    expandedRuleGroups: new Set(),
};

// ============================================================================
// DASHBOARD CARDS
// ============================================================================

export const dashboard = {
    cards: null,
    groups: null,
    hiddenSensors: null,
    loaded: false,
    saveTimer: null,
    liveTimer: null,
    sparklineHistory: {},
};

// ============================================================================
// CARD DRAG & DROP
// ============================================================================

export const cardDrag = {
    occurred: false,
    dropTarget: null,
    mouseDown: null,
    dragClone: null,
    gridCache: null,
    dropPreview: null,
};

// ============================================================================
// CARD RESIZE
// ============================================================================

export const cardResize = {
    resizing: null,
    startX: 0,
    startY: 0,
    startW: 0,
    startH: 0,
    minRowSpan: 1,
};

// ============================================================================
// CARD EDIT / CONFIG
// ============================================================================

export const cardEdit = {
    editingCardId: null,
    configuringCardId: null,
};

// ============================================================================
// SMART MODAL
// ============================================================================

export const smart = {
    modalCardId: null,
    modalDiskId: null,
    modalSource: 'local',
    attributes: [],
    attrType: 'sata',
    cache: {},
    historyCache: {},  // Key: "diskId:attrKey" -> [{value, ts}]
    fetchGeneration: 0,
};

// ============================================================================
// GROUP RESIZE & DRAG
// ============================================================================

export const groupDrag = {
    resizingGroupId: null,
    resizeStartY: 0,
    resizeStartH: 0,
    draggedGroup: null,
    dropTarget: null,
};

// ============================================================================
// SYSTEM TIMER
// ============================================================================

export const timers = {
    system: null,
    autoUpdate: null,
};

// ============================================================================
// DSM SCHEME EDITOR
// ============================================================================

export const dsm = {
    schemes: [],
    activeScheme: null,
};

// ============================================================================
// LOGGING
// ============================================================================

export const logging = {
    level: 'INFO',
    retention: 30,
};

// ============================================================================
// UPDATE SYSTEM
// ============================================================================

export const update = {
    checked: false,
    agentStates: {},
    resolve: null,
};

// ============================================================================
// CONFIG CONFLICT
// ============================================================================

export const conflict = {
    data: null,
};

// ============================================================================
// DEBUG PANEL
// ============================================================================

export const debug = {
    open: false,
};

// ============================================================================
// SPARKLINE (const-like, but mutable object)
// ============================================================================

export const sparklineHistory = {};
