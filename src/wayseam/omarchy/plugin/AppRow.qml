import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

// One discovered Windows app: icon, name, a visibility switch, and a launch
// button. The row is a CursorSurface so the panel's keyboard cursor and mouse
// hover paint one highlight; `cursorColumn` picks which of the two controls
// on the trailing edge carries the ring (0 = switch, 1 = launch).
CursorSurface {
  id: row

  property var app: null
  property bool hidden: false
  property bool presented: false
  property int rowIndex: 0
  property bool rowHasCursor: false
  property int cursorColumn: 0
  property bool busy: false
  property bool launchEnabled: true
  // PointerMoveGate owned by the panel; when set, only genuine pointer
  // motion over the row moves the cursor onto it.
  property var gate: null
  property color dim: Qt.darker(foreground, 1.55)
  property string fontFamily: Style.font.family

  signal entered(int index)
  signal columnHovered(int index, int column)
  signal toggleRequested(var app)
  signal launchRequested(var app)

  // The ⋯ menu is per row and closes when the cursor leaves the row or the
  // app's visibility changes.
  property bool menuOpen: false
  onRowHasCursorChanged: if (!rowHasCursor) menuOpen = false
  onHiddenChanged: menuOpen = false

  readonly property string slug: app ? String(app.slug || "") : ""
  readonly property string name: app ? String(app.name || slug) : ""
  readonly property real iconSize: Style.space(22)

  // The discovery step exports a PNG next to each app; that wins. Without one
  // fall back to the icon theme by slug, and finally to a glyph.
  readonly property string iconSource: {
    if (!app) return ""
    var file = String(app.icon || "").trim()
    if (file !== "") return file.indexOf("file://") === 0 ? file : "file://" + file
    if (slug === "") return ""
    var themed = Quickshell.iconPath(slug, true)
    return themed ? String(themed) : ""
  }

  hasCursor: rowHasCursor
  implicitHeight: Math.max(labels.implicitHeight, more.implicitHeight) + Style.spacing.lg * 2

  MouseArea {
    id: rowMouse
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.NoButton
    cursorShape: Qt.ArrowCursor
    onPositionChanged: function(mouse) {
      if (!row.gate || row.gate.moved(rowMouse, mouse)) row.entered(row.rowIndex)
    }
  }

  RowLayout {
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(8)
    anchors.rightMargin: Style.space(6)
    spacing: Style.space(10)

    Item {
      Layout.preferredWidth: row.iconSize
      Layout.preferredHeight: row.iconSize
      Layout.alignment: Qt.AlignVCenter
      opacity: row.hidden ? 0.45 : 1.0

      Image {
        id: icon
        anchors.fill: parent
        source: row.iconSource
        sourceSize.width: row.iconSize * 2
        sourceSize.height: row.iconSize * 2
        fillMode: Image.PreserveAspectFit
        smooth: true
        asynchronous: true
      }

      Text {
        anchors.centerIn: parent
        visible: icon.status !== Image.Ready
        text: "󰖳"
        color: row.foreground
        font.family: row.fontFamily
        font.pixelSize: Style.font.icon
      }
    }

    ColumnLayout {
      id: labels
      Layout.fillWidth: true
      spacing: Style.space(1)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          text: row.name
          color: row.hidden ? row.dim : row.foreground
          font.family: row.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }
      }

      Text {
        Layout.fillWidth: true
        text: row.presented ? "On screen · " + row.slug : row.slug
        color: row.dim
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    // Presence dot: this app currently has at least one Wayseam Window up.
    Rectangle {
      visible: row.presented
      Layout.preferredWidth: Style.space(6)
      Layout.preferredHeight: Style.space(6)
      Layout.alignment: Qt.AlignVCenter
      radius: width / 2
      color: Color.accent
    }

    // Row actions live behind a ⋯ button: press it to reveal Hide/Show (with
    // an eye icon) so the list stays quiet until you ask for the controls.
    Button {
      id: visibilityAction
      visible: row.menuOpen
      Layout.alignment: Qt.AlignVCenter
      iconText: row.hidden ? "󰈈" : "󰈉"
      text: row.hidden ? "Show" : "Hide"
      fontSize: Style.font.caption
      foreground: row.foreground
      fontFamily: row.fontFamily
      bordered: true
      enabled: !row.busy
      tooltipText: row.hidden ? "Show in Omarchy menu" : "Hide from Omarchy menu"
      onClicked: {
        row.menuOpen = false
        row.toggleRequested(row.app)
      }
    }

    PanelActionButton {
      id: more
      Layout.alignment: Qt.AlignVCenter
      iconText: "󰇘"
      tooltipText: row.menuOpen ? "Close" : "More"
      hasCursor: row.rowHasCursor && row.cursorColumn === 0
      foreground: row.foreground
      fontFamily: row.fontFamily
      onHovered: function(on) { if (on) row.columnHovered(row.rowIndex, 0) }
      onClicked: row.menuOpen = !row.menuOpen
    }

    PanelActionButton {
      id: launch
      Layout.alignment: Qt.AlignVCenter
      iconText: "󰐊"
      tooltipText: "Launch " + row.name
      enabled: row.launchEnabled
      hasCursor: row.rowHasCursor && row.cursorColumn === 1
      foreground: row.foreground
      fontFamily: row.fontFamily
      onHovered: function(on) { if (on) row.columnHovered(row.rowIndex, 1) }
      onClicked: row.launchRequested(row.app)
    }
  }
}
