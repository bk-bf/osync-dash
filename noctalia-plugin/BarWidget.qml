// BarWidget.qml — the pill in the Noctalia bar.
//
// Pure presentation over Main.qml (via pluginApi.mainInstance). Left click
// opens the panel, middle click re-probes, right click gives a context menu.
// Nothing here can start a sync — that stays an explicit action in the TUI.

import QtQuick
import Quickshell
import qs.Commons
import qs.Modules.Bar.Extras
import qs.Services.UI
import qs.Widgets

Item {
  id: root

  property ShellScreen screen
  property var pluginApi
  property string widgetId: ""
  property string section: ""
  property int sectionWidgetIndex: -1
  property int sectionWidgetsCount: 0

  readonly property var svc: pluginApi ? pluginApi.mainInstance : null

  property var widgetMetadata: BarWidgetRegistry.widgetMetadata[widgetId] ?? ({})
  readonly property string screenName: screen ? screen.name : ""

  property var widgetSettings: {
    if (section && sectionWidgetIndex >= 0 && screenName) {
      var widgets = Settings.getBarWidgetsForScreen(screenName)[section];
      if (widgets && sectionWidgetIndex < widgets.length)
        return widgets[sectionWidgetIndex];
    }
    return {};
  }

  readonly property string barPosition: Settings.getBarPositionForScreen(screenName)
  readonly property bool isBarVertical: barPosition === "left" || barPosition === "right"
  readonly property string displayMode: widgetSettings.displayMode !== undefined ? widgetSettings.displayMode : widgetMetadata.displayMode
  readonly property string iconColorKey: widgetSettings.iconColor !== undefined ? widgetSettings.iconColor : widgetMetadata.iconColor
  readonly property string textColorKey: widgetSettings.textColor !== undefined ? widgetSettings.textColor : widgetMetadata.textColor

  // The status dot is always coloured — it is the whole point of the widget.
  // The label stays on the user's configured bar colour so it remains readable.
  readonly property bool tinted: svc !== null && svc.colorByHealth

  implicitWidth: pill.width
  implicitHeight: pill.height

  NPopupContextMenu {
    id: contextMenu

    model: [
      {
        "label": "Re-probe now",
        "action": "refresh",
        "icon": "refresh"
      },
      {
        "label": "Open osd TUI",
        "action": "tui",
        "icon": "external-link"
      },
      {
        "label": "Widget settings",
        "action": "widget-settings",
        "icon": "settings"
      },
    ]

    onTriggered: action => {
                   contextMenu.close();
                   PanelService.closeContextMenu(screen);

                   if (action === "refresh") {
                     if (root.svc)
                       root.svc.refresh();
                   } else if (action === "tui") {
                     if (root.svc)
                       root.svc.openTui();
                   } else if (action === "widget-settings") {
                     BarService.openWidgetSettings(screen, section, sectionWidgetIndex, widgetId, widgetSettings);
                   }
                 }
  }

  BarPill {
    id: pill

    screen: root.screen
    oppositeDirection: BarService.getPillDirection(root)

    icon: root.svc ? root.svc.barIconName : "point-filled"
    text: root.svc ? root.svc.barLabel : "…"

    // dot on the left, counts to its right — regardless of bar side
    iconPosition: "left"

    customIconColor: root.tinted ? root.svc.healthColor : Color.resolveColorKeyOptional(root.iconColorKey)
    customTextColor: Color.resolveColorKeyOptional(root.textColorKey)

    autoHide: false
    forceOpen: !root.isBarVertical && root.displayMode === "alwaysShow"
    forceClose: root.isBarVertical || root.displayMode === "alwaysHide"

    tooltipText: root.svc ? root.svc.tooltipText : ""

    onClicked: {
      if (root.pluginApi)
        root.pluginApi.togglePanel(root.screen, this);
    }
    onMiddleClicked: {
      if (root.svc)
        root.svc.refresh();
    }
    onRightClicked: PanelService.showContextMenu(contextMenu, pill, screen)
  }
}
