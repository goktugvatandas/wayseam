import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Windows (Wayseam) bar widget: one bar icon and one panel that reads
// `wayseam state --json` and drives the mode switch, Omarchy menu
// placement, per-app visibility, and app launching through the wayseam CLI.
Panel {
  id: root
  moduleName: "goktugvatandas.wayseam"
  ipcTarget: "goktugvatandas.wayseam"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // ---------------------------------------------------------------- settings
  readonly property string wayseamCommand: {
    var v = String(setting("wayseamCommand", "wayseam") || "").trim()
    return v === "" ? "wayseam" : v
  }
  readonly property int refreshIntervalSec: Model.clampInt(setting("refreshIntervalSec", 15), 15, 5, 3600)

  // ------------------------------------------------------------------- state
  //
  // `snapshot` is the last good parse of the state command. Transient
  // failures keep it and surface `lastError` instead, so the panel never
  // collapses mid-refresh. (`state` is taken by Item, hence the name.)
  property var snapshot: Model.emptyState()
  property bool loaded: false
  property string lastError: ""
  property string actionStatus: ""
  property string pendingMode: ""
  property string pendingPlacement: ""
  property var pendingVisibility: ({})
  property string query: ""

  // Row identities (slug/name/icon) only; replaced when that set changes so
  // the Repeater keeps its rows across ordinary refreshes. Visibility and
  // presence come from the side maps below.
  property var appList: []
  property string appListKey: ""

  readonly property bool actionBusy: actionProc.running
  readonly property bool refreshingApps: refreshProc.running
  readonly property bool vmRunning: snapshot.vmRunning === true
  readonly property string mode: pendingMode !== "" ? pendingMode : String(snapshot.mode || "")
  readonly property string placement: pendingPlacement !== "" ? pendingPlacement : String(snapshot.menuPlacement || "")
  readonly property var hiddenBySlug: Model.hiddenMap(snapshot.apps, pendingVisibility)
  readonly property var presentedBySlug: Model.presentedMap(snapshot.apps)
  readonly property var filteredApps: Model.filterApps(appList, query)
  readonly property string heroStatus: Model.heroStatus(snapshot, loaded, lastError)
  readonly property string agentVersion: String(snapshot.agentVersion || "")
  readonly property var modes: Model.MODES
  readonly property var placements: Model.PLACEMENTS
  // Tallest the app list grows before it scrolls; keeps the footer on screen.
  readonly property int appListCap: Style.space(300)

  // ------------------------------------------------------------------ cursor
  //
  // Sections top to bottom. "apps" only exists while the filtered list has
  // rows so j/k never lands on an empty list.
  property bool cursorActive: false
  property string focusSection: "mode"
  property int modeIndex: 0
  property int placementIndex: 0
  property int headerIndex: 0
  property int appIndex: 0
  property int appColumn: 0
  property int footerIndex: 0

  readonly property var sections: {
    var list = ["mode", "placement", "header", "search"]
    if (filteredApps.length > 0) list.push("apps")
    list.push("footer")
    return list
  }

  function ensureCursor() {
    if (sections.indexOf(focusSection) < 0) focusSection = "footer"
    modeIndex = Model.clampIndex(modeIndex, modes.length)
    placementIndex = Model.clampIndex(placementIndex, placements.length)
    headerIndex = Model.clampIndex(headerIndex, 2)
    appIndex = Model.clampIndex(appIndex, filteredApps.length)
    appColumn = Model.clampIndex(appColumn, 2)
    footerIndex = Model.clampIndex(footerIndex, 2)
  }

  function moveSection(delta) {
    var idx = sections.indexOf(focusSection)
    var next = Model.clampIndex(idx + delta, sections.length)
    if (next === idx) return
    focusSection = sections[next]
    if (focusSection === "apps") appIndex = delta > 0 ? 0 : filteredApps.length - 1
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    pointerGate.reset()
    ensureCursor()
    if (dy !== 0) {
      if (focusSection === "apps") {
        if (dy > 0 && appIndex < filteredApps.length - 1) appIndex++
        else if (dy < 0 && appIndex > 0) appIndex--
        else moveSection(dy)
      } else {
        moveSection(dy)
      }
    } else if (dx !== 0) {
      if (focusSection === "mode") modeIndex = Model.clampIndex(modeIndex + dx, modes.length)
      else if (focusSection === "placement") placementIndex = Model.clampIndex(placementIndex + dx, placements.length)
      else if (focusSection === "header") headerIndex = Model.clampIndex(headerIndex + dx, 2)
      else if (focusSection === "apps") appColumn = Model.clampIndex(appColumn + dx, 2)
      else if (focusSection === "footer") footerIndex = Model.clampIndex(footerIndex + dx, 2)
    }
    ensureCursor()
    scrollCursorIntoView()
  }

  function activateCursor() {
    ensureCursor()
    if (focusSection === "mode") setMode(modes[modeIndex].value)
    else if (focusSection === "placement") setPlacement(placements[placementIndex].value)
    else if (focusSection === "header") setVisibilityAll(headerIndex === 0)
    else if (focusSection === "search") focusSearch()
    else if (focusSection === "apps") {
      var app = selectedApp()
      if (!app) return
      if (appColumn === 1) launchApp(app)
      else toggleApp(app)
    } else if (focusSection === "footer") {
      if (footerIndex === 0) refreshApps()
      else setMode("desktop")
    }
  }

  function selectedApp() {
    if (filteredApps.length === 0) return null
    return filteredApps[Model.clampIndex(appIndex, filteredApps.length)]
  }

  function setCursor(section, index, column) {
    cursorActive = true
    focusSection = section
    if (section === "mode") modeIndex = index
    else if (section === "placement") placementIndex = index
    else if (section === "header") headerIndex = index
    else if (section === "apps") { appIndex = index; if (column !== undefined) appColumn = column }
    else if (section === "footer") footerIndex = index
  }

  function scrollItemIntoView(item) {
    if (!appsFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(appsFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = appsFlick.contentY
      var viewBottom = viewTop + appsFlick.height
      var maxY = Math.max(0, appsFlick.contentHeight - appsFlick.height)
      if (top < viewTop + margin) appsFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) appsFlick.contentY = Math.min(maxY, bottom + margin - appsFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "apps" && appColumnItem && appIndex >= 0 && appIndex < appColumnItem.children.length) scrollItemIntoView(appColumnItem.children[appIndex])
  }

  function focusSearch() {
    cursorActive = true
    focusSection = "search"
    Qt.callLater(function() { if (searchField) searchField.forceActiveFocus() })
  }

  function leaveSearch(toApps) {
    if (toApps && filteredApps.length > 0) { focusSection = "apps"; appIndex = 0; appColumn = 0 }
    keyCatcher.forceActiveFocus()
    scrollCursorIntoView()
  }

  // ----------------------------------------------------------------- actions

  function refresh() {
    if (stateProc.running) return
    stateProc.command = ["bash", "-lc", Model.stateCommand(root.wayseamCommand)]
    stateProc.running = true
  }

  function applyState(raw) {
    var parsed = Model.parseState(raw)
    if (!parsed.ok) {
      lastError = parsed.error
      return
    }
    lastError = ""
    loaded = true
    snapshot = parsed.state
    var key = Model.appListKey(snapshot.apps)
    if (key !== appListKey) {
      appListKey = key
      appList = Model.appIdentityList(snapshot.apps)
    }
    pendingMode = ""
    pendingPlacement = ""
    pendingVisibility = {}
    ensureCursor()
  }

  function runAction(command, status) {
    if (!command || actionProc.running) return false
    actionStatus = status || ""
    actionProc.command = ["bash", "-lc", command]
    actionProc.running = true
    return true
  }

  function setMode(value) {
    if (!vmRunning || value === "" || value === mode) return
    if (runAction(Model.modeCommand(wayseamCommand, value), "Switching to " + Model.modeLabel(value) + " Mode…")) pendingMode = value
  }

  function setPlacement(value) {
    if (value === "" || value === placement) return
    if (runAction(Model.placementCommand(wayseamCommand, value), "Updating Omarchy menu…")) pendingPlacement = value
  }

  function setVisibilityAll(show) {
    if (!runAction(Model.visibilityCommand(wayseamCommand, show ? "all" : "none", ""), show ? "Showing every app…" : "Hiding every app…")) return
    var next = {}
    for (var i = 0; i < appList.length; i++) next[appList[i].slug] = !show
    pendingVisibility = next
  }

  function toggleApp(app) {
    if (!app || !app.slug) return
    var hide = hiddenBySlug[app.slug] !== true
    if (!runAction(Model.visibilityCommand(wayseamCommand, hide ? "hide" : "show", app.slug), (hide ? "Hiding " : "Showing ") + app.name + "…")) return
    var next = {}
    for (var key in pendingVisibility) next[key] = pendingVisibility[key]
    next[app.slug] = hide
    pendingVisibility = next
  }

  function launchApp(app) {
    if (!app || !app.slug) return
    Quickshell.execDetached(["bash", "-lc", Model.runAppCommand(wayseamCommand, app.slug)])
    actionStatus = "Launching " + app.name + "…"
    actionStatusTimer.restart()
  }

  function refreshApps() {
    if (refreshProc.running) return
    actionStatus = "Refreshing apps…"
    refreshProc.command = ["bash", "-lc", Model.refreshAppsCommand(wayseamCommand)]
    refreshProc.running = true
  }

  function actionFinished(exitCode, label) {
    if (exitCode !== 0) {
      lastError = label + " failed (exit " + exitCode + ")"
      pendingMode = ""
      pendingPlacement = ""
      pendingVisibility = {}
    }
    actionStatus = ""
    refresh()
  }

  // --------------------------------------------------------------- lifecycle

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    focusSection = "mode"
    pointerGate.reset()
    if (appsFlick) appsFlick.contentY = 0
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onFilteredAppsChanged: ensureCursor()
  onAppIndexChanged: scrollCursorIntoView()
  onModeChanged: {
    var idx = Model.indexOfValue(modes, mode)
    if (idx >= 0 && !(cursorActive && focusSection === "mode")) modeIndex = idx
  }
  onPlacementChanged: {
    var idx = Model.indexOfValue(placements, placement)
    if (idx >= 0 && !(cursorActive && focusSection === "placement")) placementIndex = idx
  }

  Component.onCompleted: refresh()

  // Rows only take the cursor on real pointer motion. Delegates that slide
  // under a stationary pointer (a status line changing, the list refreshing)
  // produce synthetic hover that must not hijack keyboard navigation.
  PointerMoveGate {
    id: pointerGate
    referenceItem: panelBody
  }

  Process {
    id: stateProc
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyState(text) }
    stderr: StdioCollector { waitForEnd: true }
  }

  Process {
    id: actionProc
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) { root.actionFinished(exitCode, "Action") }
  }

  Process {
    id: refreshProc
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) { root.actionFinished(exitCode, "App refresh") }
  }

  // Poll while open; a slow background poll while closed keeps the bar icon's
  // VM-running dimming honest without hammering the CLI.
  Timer {
    interval: root.refreshIntervalSec * 1000
    running: root.opened
    repeat: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: Math.max(60, root.refreshIntervalSec * 4) * 1000
    running: !root.opened
    repeat: true
    onTriggered: root.refresh()
  }

  Timer {
    id: actionStatusTimer
    interval: 2500
    onTriggered: if (!root.actionBusy && !root.refreshingApps) root.actionStatus = ""
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
    function status(): string { return root.heroStatus }
    function version(): string { return "0.1.0" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰖳"
    foreground: root.vmRunning ? root.barForeground : Qt.darker(root.barForeground, 1.55)
    tooltipText: root.loaded ? "Windows · " + root.heroStatus : "Windows"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; root.ensureCursor(); return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "/") root.focusSearch()
        else if (t === "r" || t === "R") root.refresh()
      }

      // The panel itself does not scroll: hero, chips, and the footer stay put
      // and only the app list (appsFlick) scrolls, so the actions are always
      // reachable no matter how many apps discovery found.
      Item {
        id: panelBody
        anchors.fill: parent

        Column {
          id: column
          width: panelBody.width
          spacing: Style.space(12)

          // ---------- Hero: glyph · title/status · agent version ----------
          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroVersion.implicitHeight)

            Text {
              id: heroIcon
              text: "󰖳"
              color: root.foreground
              opacity: root.vmRunning ? 1.0 : 0.5
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter

              Behavior on opacity { NumberAnimation { duration: 200 } }
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: heroVersion.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "Windows"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                text: root.heroStatus.toUpperCase()
                color: !root.loaded && root.lastError !== "" ? root.urgent : Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
                width: parent.width
              }
            }

            Text {
              id: heroVersion
              visible: text !== ""
              text: root.agentVersion !== "" ? "agent " + root.agentVersion : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
            }
          }

          // ---------- Status / error line ----------
          // Always one line tall. Action feedback and errors replace the idle
          // summary in place rather than inserting a row, so the sections
          // below never jump under a resting pointer (hover would otherwise
          // re-fire and steal the keyboard cursor).
          Item {
            width: parent.width
            implicitHeight: Math.max(statusText.implicitHeight, Math.ceil(Style.font.bodySmall * 1.4))

            readonly property string errorText: root.loaded && root.lastError !== "" ? root.lastError : root.snapshot.errors.join(" · ")

            Text {
              id: statusText
              width: parent.width
              anchors.verticalCenter: parent.verticalCenter
              text: root.actionStatus !== "" ? root.actionStatus
                : (parent.errorText !== "" ? parent.errorText : Model.appSummary(root.snapshot.apps, root.hiddenBySlug))
              color: root.actionStatus === "" && parent.errorText !== "" ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
          }

          // ---------- Mode ----------
          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "MODE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            ChipRow {
              id: modeRow
              options: root.modes
              value: root.mode
              section: "mode"
              cursorIndex: root.modeIndex
              enabled: root.vmRunning && root.loaded
              onChosen: function(value) { root.setMode(value) }
            }

            Text {
              visible: root.loaded && !root.vmRunning
              width: parent.width
              text: "Start the Windows VM to switch between Wayseam Mode and Desktop Mode."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ---------- Omarchy menu placement ----------
          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "IN OMARCHY MENU"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            ChipRow {
              id: placementRow
              options: root.placements
              value: root.placement
              section: "placement"
              cursorIndex: root.placementIndex
              enabled: root.loaded
              onChosen: function(value) { root.setPlacement(value) }
            }
          }

          // ---------- Apps ----------
          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(8)

            Item {
              id: appsHeader
              width: parent.width
              implicitHeight: Math.max(appsTitle.implicitHeight, headerActions.implicitHeight)

              PanelSectionHeader {
                id: appsTitle
                text: "APPS" + (root.appList.length > 0 ? " · " + root.appList.length : "")
                foreground: root.foreground
                fontFamily: root.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Row {
                id: headerActions
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(4)

                Repeater {
                  model: ["All", "None"]
                  Button {
                    required property var modelData
                    required property int index
                    text: modelData
                    fontSize: Style.font.caption
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                    horizontalPadding: Style.space(8)
                    verticalPadding: Style.space(3)
                    enabled: root.loaded && root.appList.length > 0
                    opacity: enabled ? 1.0 : 0.4
                    hasCursor: root.cursorActive && root.focusSection === "header" && root.headerIndex === index
                    onClicked: root.setVisibilityAll(index === 0)
                    onHovered: function(h) { if (h) root.setCursor("header", index) }
                  }
                }
              }
            }

            TextField {
              id: searchField
              width: parent.width
              foreground: root.foreground
              font.family: root.fontFamily
              placeholderText: "Search apps  ( / )"
              text: root.query
              hasCursor: root.cursorActive && root.focusSection === "search"
              onTextChanged: {
                root.query = text
                root.appIndex = 0
              }
              onHoveredChanged: if (hovered) root.setCursor("search", 0)
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Down || (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                  root.leaveSearch(true)
                  event.accepted = true
                } else if (event.key === Qt.Key_Escape) {
                  if (text !== "") text = ""
                  else root.leaveSearch(false)
                  event.accepted = true
                } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                  root.leaveSearch(false)
                  event.accepted = true
                }
              }
            }

            Text {
              visible: root.loaded && root.filteredApps.length === 0
              width: parent.width
              text: root.appList.length === 0 ? "No Windows apps discovered yet. Use Refresh apps." : "No apps match."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignHCenter
              topPadding: Style.space(6)
              bottomPadding: Style.space(6)
            }

            Flickable {
              id: appsFlick
              width: parent.width
              height: Math.min(appColumnItem.implicitHeight, root.appListCap)
              contentWidth: width
              contentHeight: appColumnItem.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds
              flickableDirection: Flickable.VerticalFlick
              interactive: contentHeight > height
              ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

              Column {
                id: appColumnItem
                width: appsFlick.width
                spacing: Style.space(4)

                Repeater {
                  model: root.filteredApps
                  AppRow {
                    required property var modelData
                    required property int index
                    width: appColumnItem.width
                    app: modelData
                    gate: pointerGate
                    hidden: root.hiddenBySlug[modelData.slug] === true
                    presented: root.presentedBySlug[modelData.slug] === true
                    rowIndex: index
                    rowHasCursor: root.cursorActive && root.focusSection === "apps" && root.appIndex === index
                    cursorColumn: root.appColumn
                    busy: root.actionBusy
                    foreground: root.foreground
                    dim: root.dim
                    fontFamily: root.fontFamily
                    onEntered: function(i) { root.setCursor("apps", i) }
                    onColumnHovered: function(i, c) { root.setCursor("apps", i, c) }
                    onToggleRequested: function(a) { root.toggleApp(a) }
                    onLaunchRequested: function(a) { root.launchApp(a) }
                  }
                }
              }
            }
          }

          // ---------- Footer actions ----------
          PanelSeparator { foreground: root.foreground }

          Row {
            id: footerRow
            width: parent.width
            spacing: Style.space(6)

            readonly property real cellWidth: (width - spacing) / 2

            Button {
              width: footerRow.cellWidth
              iconText: "󰑐"
              iconSpinning: root.refreshingApps
              text: root.refreshingApps ? "Refreshing…" : "Refresh apps"
              fontSize: Style.font.bodySmall
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: !root.refreshingApps
              hasCursor: root.cursorActive && root.focusSection === "footer" && root.footerIndex === 0
              tooltipText: "Rediscover Windows apps and sync the Omarchy menu"
              onClicked: root.refreshApps()
              onHovered: function(h) { if (h) root.setCursor("footer", 0) }
            }

            Button {
              width: footerRow.cellWidth
              iconText: "󰍹"
              text: "Open desktop"
              fontSize: Style.font.bodySmall
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: root.vmRunning && root.mode !== "desktop"
              opacity: enabled ? 1.0 : 0.5
              hasCursor: root.cursorActive && root.focusSection === "footer" && root.footerIndex === 1
              tooltipText: root.vmRunning ? "Switch to Desktop Mode" : "Windows VM is not running"
              onClicked: root.setMode("desktop")
              onHovered: function(h) { if (h) root.setCursor("footer", 1) }
            }
          }
        }
      }
    }
  }

  // Equal-width chip row in the power panel's profile-picker style: bordered
  // Buttons with an optional icon, the current value drawn selected, and the
  // panel cursor walking chips with h/l.
  component ChipRow: Row {
    id: chipRow
    property var options: []
    property string value: ""
    property string section: ""
    property int cursorIndex: -1
    signal chosen(string value)

    width: parent.width
    spacing: Style.space(6)
    opacity: enabled ? 1.0 : 0.45

    readonly property real cellWidth: options.length > 0
      ? (width - spacing * (options.length - 1)) / options.length
      : 0

    Behavior on opacity { NumberAnimation { duration: 120 } }

    Repeater {
      model: chipRow.options
      Button {
        required property var modelData
        required property int index
        width: chipRow.cellWidth
        iconText: modelData.icon !== undefined ? String(modelData.icon) : ""
        iconSize: Style.font.title
        text: String(modelData.label)
        fontSize: Style.font.bodySmall
        foreground: root.foreground
        fontFamily: root.fontFamily
        horizontalPadding: Style.spacing.controlPaddingX
        verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
        bordered: true
        selected: chipRow.value === String(modelData.value)
        hasCursor: root.cursorActive && root.focusSection === chipRow.section && chipRow.cursorIndex === index
        onClicked: if (chipRow.enabled) chipRow.chosen(String(modelData.value))
        onHovered: function(h) { if (h) root.setCursor(chipRow.section, index) }
      }
    }
  }
}
