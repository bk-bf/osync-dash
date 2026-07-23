// Settings.qml — shown by Noctalia's plugin settings popup.
// The popup calls saveSettings() on Apply.

import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Widgets

Item {
  id: root

  property var pluginApi
  property int preferredWidth: 480

  readonly property var cfg: pluginApi ? pluginApi.pluginSettings : ({})

  implicitWidth: preferredWidth
  implicitHeight: col.implicitHeight

  function saveSettings() {
    if (!pluginApi)
      return;
    var s = pluginApi.pluginSettings || {};
    s.binPath = binInput.text.trim();
    s.intervalMs = Math.max(5, intervalSpin.value) * 1000;
    s.localOnly = localToggle.checked;
    s.barMetric = metricCombo.currentKey || "auto";
    s.colorByHealth = colorToggle.checked;
    s.terminalCmd = termInput.text.trim();
    pluginApi.pluginSettings = s;
    pluginApi.saveSettings();
  }

  ColumnLayout {
    id: col
    width: parent.width
    spacing: Style.marginM

    NTextInput {
      id: binInput
      Layout.fillWidth: true
      label: "osync-dash path"
      description: "The launcher installed by osync-dash's install.sh. A leading ~ is expanded; the path must not contain spaces."
      placeholderText: "~/.local/bin/osync-dash"
      text: (root.cfg && root.cfg.binPath) || ""
    }

    NComboBox {
      id: metricCombo
      Layout.fillWidth: true
      label: "Bar shows"
      description: "Which summary the pill displays."
      model: [
        {
          "key": "auto",
          "name": "Auto — syncing / problem / pending changes"
        },
        {
          "key": "health",
          "name": "Worst health across connections"
        },
        {
          "key": "counts",
          "name": "Healthy / total connections"
        },
        {
          "key": "changes",
          "name": "Pending changes (↑push ↓pull)"
        }
      ]
      currentKey: (root.cfg && root.cfg.barMetric) || "auto"
      onSelected: key => metricCombo.currentKey = key
    }

    NSpinBox {
      id: intervalSpin
      Layout.fillWidth: true
      label: "Probe interval"
      description: "Full probe (walks the local tree, ssh's to the remote). Liveness is polled separately every 1.5s, so this only governs how often counts and disk usage refresh."
      from: 5
      to: 600
      stepSize: 5
      suffix: " s"
      value: Math.max(5, Math.round(((root.cfg && root.cfg.intervalMs) || 20000) / 1000))
    }

    NToggle {
      id: localToggle
      Layout.fillWidth: true
      label: "Local only"
      description: "Skip the remote ssh probe. Much cheaper and works offline, but pull counts and the remote replica go unknown."
      checked: (root.cfg && root.cfg.localOnly !== undefined) ? root.cfg.localOnly : false
      onToggled: checked => localToggle.checked = checked
    }

    NToggle {
      id: colorToggle
      Layout.fillWidth: true
      label: "Colour the pill by health"
      description: "When off, the widget always uses your configured bar colours."
      checked: (root.cfg && root.cfg.colorByHealth !== undefined) ? root.cfg.colorByHealth : true
      onToggled: checked => colorToggle.checked = checked
    }

    NTextInput {
      id: termInput
      Layout.fillWidth: true
      label: "Terminal command"
      description: "Used by \"Open osync-dash TUI\". Launched as `<terminal> -e <osync-dash>`."
      placeholderText: "kitty"
      text: (root.cfg && root.cfg.terminalCmd) || ""
    }
  }
}
