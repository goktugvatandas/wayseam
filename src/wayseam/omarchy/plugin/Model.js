// Pure-JS state normalization and command construction for the Wayseam
// bar widget. No QML types in here so the file can be exercised from node
// (see the module.exports block at the bottom), mirroring power/Model.js.

var MODES = [
  { value: "wayseam", label: "Wayseam", icon: "󰖲" },
  { value: "desktop", label: "Desktop", icon: "󰍹" }
]

var PLACEMENTS = [
  { value: "apps", label: "Apps" },
  { value: "windows-apps", label: "Windows Apps" },
  { value: "both", label: "Both" },
  { value: "none", label: "Hidden" }
]

function clampIndex(index, length) {
  if (length <= 0) return 0
  return Math.max(0, Math.min(length - 1, index))
}

function clampInt(value, fallback, min, max) {
  var n = parseInt(String(value), 10)
  if (!isFinite(n)) n = fallback
  if (n < min) n = min
  if (n > max) n = max
  return n
}

function indexOfValue(options, value) {
  for (var i = 0; i < options.length; i++)
    if (options[i].value === value) return i
  return -1
}

// ---------------------------------------------------------------- state

function emptyState() {
  return {
    schema: 0,
    mode: "",
    menuPlacement: "",
    vmRunning: false,
    container: "",
    agentOk: false,
    agentVersion: "",
    desktopClientRunning: false,
    presenters: 0,
    guest: null,
    apps: [],
    errors: []
  }
}

function normalizeApp(raw) {
  var a = raw && typeof raw === "object" ? raw : {}
  var slug = String(a.slug || "").trim()
  var name = String(a.name || "").trim()
  return {
    slug: slug,
    name: name !== "" ? name : slug,
    hidden: a.hidden === true,
    icon: String(a.icon || "").trim(),
    presented: a.presented === true
  }
}

function normalizeState(json) {
  var s = emptyState()
  var j = json && typeof json === "object" ? json : {}
  var vm = j.vm && typeof j.vm === "object" ? j.vm : {}
  var agent = j.agent && typeof j.agent === "object" ? j.agent : {}
  var session = j.session && typeof j.session === "object" ? j.session : {}

  s.schema = Number(j.schema) || 0
  s.mode = String(j.mode || "").trim().toLowerCase()
  s.menuPlacement = String(j.menu_placement || "").trim().toLowerCase()
  s.vmRunning = vm.running === true
  s.container = String(vm.container || "")
  s.agentOk = agent.ok === true
  s.agentVersion = agent.version === null || agent.version === undefined ? "" : String(agent.version)
  s.desktopClientRunning = session.desktop_client_running === true
  s.presenters = Math.max(0, Number(session.presenters) || 0)
  s.guest = session.guest && typeof session.guest === "object" ? session.guest : null

  var apps = Array.isArray(j.apps) ? j.apps : []
  for (var i = 0; i < apps.length; i++) {
    var app = normalizeApp(apps[i])
    if (app.slug !== "") s.apps.push(app)
  }

  var errors = Array.isArray(j.errors) ? j.errors : []
  for (var e = 0; e < errors.length; e++) {
    var msg = String(errors[e] || "").trim()
    if (msg !== "") s.errors.push(msg)
  }
  return s
}

// Tolerates noise before the JSON object (a shell warning line, a deprecation
// notice) by parsing from the first "{". Anything that still isn't an object
// is reported as unavailable rather than thrown.
function parseState(raw) {
  var text = String(raw || "")
  var start = text.indexOf("{")
  if (start < 0) return { ok: false, error: "wayseam unavailable" }
  try {
    var json = JSON.parse(text.substring(start).trim())
    if (!json || typeof json !== "object" || Array.isArray(json)) return { ok: false, error: "wayseam unavailable" }
    return { ok: true, state: normalizeState(json) }
  } catch (e) {
    return { ok: false, error: "wayseam unavailable" }
  }
}

function presentedCount(apps) {
  var list = Array.isArray(apps) ? apps : []
  var n = 0
  for (var i = 0; i < list.length; i++) if (list[i] && list[i].presented === true) n++
  return n
}

function windowCount(state) {
  var s = state || emptyState()
  return Math.max(Number(s.presenters) || 0, presentedCount(s.apps))
}

// Uppercase status line under the hero title. The hero itself upper-cases.
function heroStatus(state, loaded, error) {
  if (!loaded) return error ? String(error) : "Checking…"
  var s = state || emptyState()
  if (!s.vmRunning) return "VM stopped"
  if (!s.agentOk) return "Agent offline"
  if (s.mode === "desktop") return "Desktop mode"
  var n = windowCount(s)
  return "Wayseam mode · " + n + (n === 1 ? " window" : " windows")
}

function modeLabel(value) {
  var idx = indexOfValue(MODES, value)
  return idx >= 0 ? MODES[idx].label : String(value || "")
}

function placementLabel(value) {
  var idx = indexOfValue(PLACEMENTS, value)
  return idx >= 0 ? PLACEMENTS[idx].label : String(value || "")
}

// The row list only carries what identifies an app (slug, name, icon).
// Visibility and presence live in side maps so a refresh that merely flips
// them does not hand the Repeater a new array — rebuilding every row loses
// the scroll position, reloads icons, and re-fires hover under a resting
// pointer, which would yank the keyboard cursor onto whatever row it is over.
function appListKey(apps) {
  var list = Array.isArray(apps) ? apps : []
  var parts = []
  for (var i = 0; i < list.length; i++) parts.push(String(list[i].slug) + "\t" + String(list[i].name) + "\t" + String(list[i].icon))
  return parts.join("\n")
}

function appIdentityList(apps) {
  var list = Array.isArray(apps) ? apps : []
  var out = []
  for (var i = 0; i < list.length; i++) out.push({ slug: list[i].slug, name: list[i].name, icon: list[i].icon })
  return out
}

// slug -> hidden. `pending` maps slug -> hidden for toggles whose CLI call
// has not reported back yet, so the switch throws immediately.
function hiddenMap(apps, pending) {
  var list = Array.isArray(apps) ? apps : []
  var overrides = pending && typeof pending === "object" ? pending : {}
  var out = {}
  for (var i = 0; i < list.length; i++) {
    var slug = list[i].slug
    out[slug] = Object.prototype.hasOwnProperty.call(overrides, slug) ? overrides[slug] === true : list[i].hidden === true
  }
  return out
}

// Idle line under the hero: "2 on screen · 1 hidden" (empty when nothing
// is discovered). Always occupies its line so transient action/error text
// never shifts the layout under a resting pointer.
function appSummary(apps, hidden) {
  var list = Array.isArray(apps) ? apps : []
  if (list.length === 0) return ""
  var hiddenBy = hidden && typeof hidden === "object" ? hidden : {}
  var onScreen = 0
  var hiddenCount = 0
  for (var i = 0; i < list.length; i++) {
    if (list[i].presented === true) onScreen++
    var slug = list[i].slug
    var isHidden = Object.prototype.hasOwnProperty.call(hiddenBy, slug) ? hiddenBy[slug] === true : list[i].hidden === true
    if (isHidden) hiddenCount++
  }
  var parts = [onScreen + " on screen"]
  if (hiddenCount > 0) parts.push(hiddenCount + " hidden from menu")
  return parts.join(" · ")
}

function presentedMap(apps) {
  var list = Array.isArray(apps) ? apps : []
  var out = {}
  for (var i = 0; i < list.length; i++) out[list[i].slug] = list[i].presented === true
  return out
}

function filterApps(apps, query) {
  var list = Array.isArray(apps) ? apps : []
  var q = String(query || "").trim().toLowerCase()
  if (q === "") return list
  var out = []
  for (var i = 0; i < list.length; i++) {
    var app = list[i]
    var hay = (String(app.name || "") + " " + String(app.slug || "")).toLowerCase()
    if (hay.indexOf(q) !== -1) out.push(app)
  }
  return out
}

// ------------------------------------------------------------- commands
//
// Every command is one bash -lc string so ~/.local/bin resolves through the
// login shell PATH and the executable can be swapped per widget setting.

function shellQuote(value) {
  return "'" + String(value || "").replace(/'/g, "'\\''") + "'"
}

function exe(command) {
  var c = String(command || "").trim()
  return shellQuote(c === "" ? "wayseam" : c)
}

function stateCommand(command) {
  return exe(command) + " state --json"
}

function modeCommand(command, mode) {
  return exe(command) + " mode set " + shellQuote(mode)
}

function placementCommand(command, placement) {
  return exe(command) + " menu placement set " + shellQuote(placement)
}

// action: "all" | "none" | "show" | "hide"; slug required for show/hide.
function visibilityCommand(command, action, slug) {
  var base = exe(command) + " apps visibility "
  if (action === "all") return base + "--all"
  if (action === "none") return base + "--none"
  if (action === "show") return base + "--show " + shellQuote(slug)
  if (action === "hide") return base + "--hide " + shellQuote(slug)
  return ""
}

function refreshAppsCommand(command) {
  return exe(command) + " apps refresh"
}

// VM lifecycle from the applet. `start` returns as soon as the container is
// up (the panel's state refresh shows readiness); `stop` and `restart` retire
// the presenters first so no window is left pointing at a gone guest.
function podCommand(command, action) {
  if (action === "start") return exe(command) + " pod start --no-wait"
  if (action === "stop") return exe(command) + " pod stop"
  if (action === "restart") return exe(command) + " pod restart"
  return ""
}

function podLabel(action) {
  if (action === "start") return "Start VM"
  if (action === "stop") return "Stop VM"
  if (action === "restart") return "Restart VM"
  return ""
}

function podConfirmText(action) {
  if (action === "stop") return "Stop the Windows VM? Open Windows apps will close."
  if (action === "restart") return "Restart the Windows VM? Open Windows apps will close."
  if (action === "start") return "Start the Windows VM?"
  return ""
}

// Split a (filtered) app list into the rows shown up top and the hidden ones
// that live under the collapsible "Hidden apps" group at the bottom.
function partitionHidden(apps, hiddenMap) {
  var list = Array.isArray(apps) ? apps : []
  var by = hiddenMap && typeof hiddenMap === "object" ? hiddenMap : {}
  var visible = [], hidden = []
  for (var i = 0; i < list.length; i++) {
    var slug = String(list[i].slug || "")
    if (by[slug] === true) hidden.push(list[i]); else visible.push(list[i])
  }
  return { visible: visible, hidden: hidden }
}

function runAppCommand(command, slug) {
  return exe(command) + " run " + shellQuote(slug)
}

if (typeof module !== "undefined") {
  module.exports = {
    MODES: MODES,
    PLACEMENTS: PLACEMENTS,
    clampIndex: clampIndex,
    clampInt: clampInt,
    indexOfValue: indexOfValue,
    emptyState: emptyState,
    normalizeApp: normalizeApp,
    normalizeState: normalizeState,
    parseState: parseState,
    presentedCount: presentedCount,
    windowCount: windowCount,
    heroStatus: heroStatus,
    modeLabel: modeLabel,
    placementLabel: placementLabel,
    appListKey: appListKey,
    appIdentityList: appIdentityList,
    hiddenMap: hiddenMap,
    appSummary: appSummary,
    presentedMap: presentedMap,
    filterApps: filterApps,
    shellQuote: shellQuote,
    stateCommand: stateCommand,
    modeCommand: modeCommand,
    placementCommand: placementCommand,
    visibilityCommand: visibilityCommand,
    refreshAppsCommand: refreshAppsCommand,
    runAppCommand: runAppCommand
  }
}
