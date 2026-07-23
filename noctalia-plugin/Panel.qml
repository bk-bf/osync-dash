// Panel.qml — the detail view opened by clicking the bar pill.
//
// This is a one-to-one port of the TUI's connection card (card_body() in
// osync_tui.py): same rows, same order, same wording, same semantic colours.
// Anything shown here is shown there — no invented terminology, no reordering.
//
//   <name>  ⇄
//   ● HEALTHY    last sync 3m ago  (2026-07-23 06:04)
//   ↑ push in sync        ↓ pull in sync
//   result synced  ·  remote synced  ·  resume 0 clean  ·  moved ↑2 ↓0
//
//   ▎ local   my-laptop   ● online   rsync ✓   ↳ my-laptop · 100.x.y.z
//       350 files · 235M    disk ███████──── 41%  300G free
//   ▎ remote  my-server   ● online   rsync ✓   ↳ my-server · 100.a.b.c
//       350 files · 235M    disk █████────── 28%  534G free
//
//   local      ~/docs
//   remote     ubuntu@my-server:/srv/docs
//   via        Tailscale
//   auto-sync  every 1min
//   safety     soft-delete on 0/0 30d   conflict-bkp on 0/0 30d   winner newest
//   log        ~/.cache/osync/…

import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Widgets

Item {
  id: root

  property var pluginApi
  readonly property var svc: pluginApi ? pluginApi.mainInstance : null

  property int contentPreferredWidth: 560
  property int contentPreferredHeight: Math.min(820, header.implicitHeight + list.implicitHeight + Style.marginL * 3)
  property color panelBackgroundColor: Color.mSurface

  readonly property color cMuted: Color.mOnSurfaceVariant
  readonly property color cLine: Color.mOutline

  function dirArrow(d) {
    return d === "send" ? "→" : (d === "receive" ? "←" : "⇄");
  }

  // MODE_LABEL from the TUI
  function viaLabel(via) {
    if (!via)
      return "—";
    var m = via.mode;
    var base = m === "ts" ? "Tailscale" : (m === "ssh" ? "plain SSH" : (m === "both" ? "both · TS→SSH" : "—"));
    if (via.endpoint_used && m === "both")
      base += "  · live " + (via.endpoint_used === "ts" ? "TS" : "SSH");
    return base;
  }

  // auto_line() from the TUI
  function autoLabel(a) {
    if (!a || a.mode === "off" || !a.mode)
      return "off — manual";
    if (a.mode === "change")
      return "on file change";
    if (a.at)
      return "at " + a.at;
    return "every " + (a.interval || "15min");
  }

  function diskPct(rep) {
    if (!rep || !rep.disk_total || rep.disk_used === null || rep.disk_used === undefined)
      return -1;
    var t = Number(rep.disk_total);
    return t > 0 ? Math.round(Number(rep.disk_used) / t * 100) : -1;
  }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: Style.marginL
    spacing: Style.marginM

    // ── header ───────────────────────────────────────────────────────────────
    RowLayout {
      id: header
      Layout.fillWidth: true
      spacing: Style.marginS

      NIcon {
        icon: root.svc ? root.svc.barIconName : "point-filled"
        pointSize: Style.fontSizeL
        color: root.svc ? root.svc.healthColor : Color.mOnSurface
      }

      ColumnLayout {
        spacing: 0
        Layout.fillWidth: true
        NText {
          text: "osync"
          pointSize: Style.fontSizeL
          font.weight: Style.fontWeightBold
        }
        NText {
          text: {
            if (!root.svc)
              return "";
            if (!root.svc.ok)
              return root.svc.errorText || "no data";
            var n = root.svc.summary ? root.svc.summary.total : 0;
            return n + (n === 1 ? " connection" : " connections") + " · updated " + root.svc.lastOkLabel;
          }
          pointSize: Style.fontSizeXS
          color: root.cMuted
          elide: Text.ElideRight
          Layout.fillWidth: true
        }
      }

      NIconButton {
        icon: "refresh"
        tooltipText: "Re-probe every connection"
        baseSize: Style.baseWidgetSize * 0.8
        onClicked: if (root.svc)
          root.svc.refresh()
      }
      NIconButton {
        icon: "external-link"
        tooltipText: "Open the osync-dash TUI"
        baseSize: Style.baseWidgetSize * 0.8
        onClicked: if (root.svc)
          root.svc.openTui()
      }
    }

    // ── error ────────────────────────────────────────────────────────────────
    NBox {
      Layout.fillWidth: true
      forceOpaque: true
      visible: root.svc !== null && !root.svc.ok
      implicitHeight: errCol.implicitHeight + Style.marginM * 2

      ColumnLayout {
        id: errCol
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginXXS
        NText {
          text: "Can't run osync-dash"
          font.weight: Style.fontWeightSemiBold
          color: Color.mError
        }
        NText {
          text: root.svc ? root.svc.errorText : ""
          pointSize: Style.fontSizeXS
          color: root.cMuted
          wrapMode: Text.WordWrap
          Layout.fillWidth: true
        }
      }
    }

    // ── connection cards ─────────────────────────────────────────────────────
    NScrollView {
      id: scroller
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: root.svc !== null && root.svc.ok

      ColumnLayout {
        id: list
        width: scroller.availableWidth
        spacing: Style.marginS

        Repeater {
          model: root.svc ? root.svc.connections : []

          NBox {
            id: card
            required property var modelData

            Layout.fillWidth: true
            forceOpaque: true
            implicitHeight: cardCol.implicitHeight + Style.marginM * 2

            readonly property color accent: root.svc ? root.svc.healthColorOf(modelData.color) : Color.mOnSurface
            readonly property var rep_remote: modelData.remote
            readonly property var st: modelData.state || ({})

            ColumnLayout {
              id: cardCol
              anchors.fill: parent
              anchors.margins: Style.marginM
              spacing: 1

              // title:  <name>  ⇄
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.marginXS
                NText {
                  text: card.modelData.name
                  font.weight: Style.fontWeightBold
                  elide: Text.ElideRight
                }
                NText {
                  text: root.dirArrow(card.modelData.direction)
                  color: root.cMuted
                }
                Item {
                  Layout.fillWidth: true
                }
              }

              // ● HEALTHY    last sync 3m ago  (2026-07-23 06:04)
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.marginXXS
                NText {
                  text: "●"
                  color: card.accent
                  pointSize: Style.fontSizeS
                }
                NText {
                  text: card.modelData.health
                  font.weight: Style.fontWeightBold
                  color: card.accent
                  pointSize: Style.fontSizeS
                }
                NText {
                  text: "    last sync " + (root.svc ? root.svc.fmtAgeFrom(card.modelData.last_sync_ts) : "—")
                  pointSize: Style.fontSizeXS
                  color: root.cMuted
                }
                NText {
                  // only once "N d ago" stops being precise enough — below a
                  // day the wall-clock stamp just repeats the relative age
                  visible: card.modelData.last_sync_ts ? (Date.now() / 1000 - card.modelData.last_sync_ts) >= 86400 : false
                  text: "(" + (card.modelData.last_run_at || "never") + ")"
                  pointSize: Style.fontSizeXXS
                  color: root.cLine
                }
                Item {
                  Layout.fillWidth: true
                }
              }

              // ↑ push …        ↓ pull …
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.marginL

                readonly property var pushLeg: root.svc ? root.svc.leg(card.modelData, "↑", "push", card.modelData.push_changes) : null
                readonly property var pullLeg: root.svc ? root.svc.leg(card.modelData, "↓", "pull", card.modelData.pull_changes) : null

                NText {
                  text: parent.pushLeg ? parent.pushLeg.text : ""
                  color: parent.pushLeg ? parent.pushLeg.color : root.cMuted
                  pointSize: Style.fontSizeXS
                }
                NText {
                  text: parent.pullLeg ? parent.pullLeg.text : ""
                  color: parent.pullLeg ? parent.pullLeg.color : root.cMuted
                  pointSize: Style.fontSizeXS
                }
                Item {
                  Layout.fillWidth: true
                }
              }

              // result … · remote … · resume … · moved ↑x ↓y
              Flow {
                Layout.fillWidth: true
                Layout.topMargin: Style.marginXXS
                spacing: 0

                NText {
                  // "local"/"remote", not "result"/"remote": these are the two
                  // sides' last actions and the card speaks local/remote elsewhere
                  text: "local "
                  pointSize: Style.fontSizeXS
                  color: root.cMuted
                }
                NText {
                  text: card.st.init_action || "—"
                  pointSize: Style.fontSizeXS
                  color: card.st.init_action === "synced" ? root.svc.colorLive : (card.st.init_action ? root.svc.colorSalmon : root.cMuted)
                }
                NText {
                  text: "  ·  remote "
                  pointSize: Style.fontSizeXS
                  color: root.cMuted
                }
                NText {
                  text: card.st.tgt_action || "—"
                  pointSize: Style.fontSizeXS
                  color: card.st.tgt_action === "synced" ? root.svc.colorLive : root.cMuted
                }
                NText {
                  text: "  ·  resume "
                  pointSize: Style.fontSizeXS
                  color: root.cMuted
                }
                NText {
                  text: (card.st.resume === null || card.st.resume === undefined || card.st.resume === "0") ? "0 clean" : (card.st.resume + " retried")
                  pointSize: Style.fontSizeXS
                  color: (card.st.resume === null || card.st.resume === undefined || card.st.resume === "0") ? root.svc.colorLive : root.svc.colorSalmon
                }
                NText {
                  visible: {
                    var lr = card.st.last_run;
                    return lr && ((lr.pushed || 0) > 0 || (lr.pulled || 0) > 0);
                  }
                  text: "  ·  moved "
                  pointSize: Style.fontSizeXS
                  color: root.cMuted
                }
                NText {
                  visible: {
                    var lr = card.st.last_run;
                    return lr && ((lr.pushed || 0) > 0 || (lr.pulled || 0) > 0);
                  }
                  text: {
                    var lr = card.st.last_run || {};
                    return "↑" + (lr.pushed || 0) + " ↓" + (lr.pulled || 0);
                  }
                  pointSize: Style.fontSizeXS
                  color: (card.st.last_run && card.st.last_run.ok) ? root.svc.colorLive : root.svc.colorSalmon
                }
              }

              Item {
                implicitHeight: Style.marginXS
              }

              // ── replicas ───────────────────────────────────────────────────
              Repeater {
                model: [
                  {
                    "role": "local",
                    "rep": card.modelData.local,
                    "accent": root.svc ? root.svc.colorLive : Color.mOnSurface
                  },
                  {
                    "role": "remote",
                    "rep": card.rep_remote,
                    "accent": root.svc ? root.svc.colorGold : Color.mOnSurface
                  }
                ]

                ColumnLayout {
                  required property var modelData
                  readonly property var rep: modelData.rep || ({})
                  readonly property bool reach: rep.reach === true

                  Layout.fillWidth: true
                  spacing: 0

                  // ▎ local   host   ● online   rsync ✓   ↳ ts · ip
                  RowLayout {
                    Layout.fillWidth: true
                    spacing: Style.marginXXS

                    NText {
                      text: "▎"
                      color: modelData.accent
                      font.weight: Style.fontWeightBold
                    }
                    NText {
                      text: modelData.role
                      font.weight: Style.fontWeightBold
                      color: modelData.accent
                      pointSize: Style.fontSizeXS
                      Layout.preferredWidth: 52
                    }
                    NText {
                      text: rep.host || (rep.ts ? rep.ts.name : "") || "—"
                      font.weight: Style.fontWeightBold
                      pointSize: Style.fontSizeXS
                      elide: Text.ElideRight
                    }
                    NText {
                      text: "   ● " + (reach ? "online" : "offline")
                      pointSize: Style.fontSizeXS
                      color: reach ? root.svc.colorLive : root.svc.colorSalmon
                    }
                    NText {
                      text: "   rsync"
                      pointSize: Style.fontSizeXS
                      color: root.cMuted
                    }
                    NText {
                      text: rep.rsync ? "✓" : (reach ? "✗" : "—")
                      pointSize: Style.fontSizeXS
                      color: rep.rsync ? root.svc.colorLive : root.cMuted
                    }
                    NText {
                      visible: rep.ts && rep.ts.name
                      text: "   ↳ " + (rep.ts ? rep.ts.name : "")
                      pointSize: Style.fontSizeXXS
                      color: root.cMuted
                      elide: Text.ElideRight
                    }
                    NText {
                      visible: rep.ts && rep.ts.ip
                      text: "· " + (rep.ts ? rep.ts.ip : "")
                      pointSize: Style.fontSizeXXS
                      color: root.cLine
                    }
                    Item {
                      Layout.fillWidth: true
                    }
                  }

                  // files · size    disk ▓▓▓░░ 41%  300G free
                  RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: Style.marginM
                    spacing: Style.marginXXS
                    visible: reach

                    NText {
                      visible: rep.files !== null && rep.files !== undefined
                      text: (rep.files || 0) + " files"
                      pointSize: Style.fontSizeXXS
                    }
                    NText {
                      visible: rep.size !== null && rep.size !== undefined
                      text: "· " + (root.svc ? root.svc.fmtSize(rep.size) : "")
                      pointSize: Style.fontSizeXXS
                      color: root.cMuted
                    }
                    NText {
                      visible: root.diskPct(rep) >= 0
                      text: "    disk"
                      pointSize: Style.fontSizeXXS
                      color: root.cMuted
                    }
                    NLinearGauge {
                      visible: root.diskPct(rep) >= 0
                      Layout.preferredWidth: 90
                      implicitHeight: 5
                      Layout.alignment: Qt.AlignVCenter
                      orientation: Qt.Horizontal
                      ratio: Math.max(0, Math.min(1, root.diskPct(rep) / 100))
                      fillColor: root.diskPct(rep) >= 90 ? root.svc.colorSalmon : root.svc.colorLive
                    }
                    NText {
                      visible: root.diskPct(rep) >= 0
                      text: root.diskPct(rep) + "%"
                      pointSize: Style.fontSizeXXS
                      color: root.svc ? root.svc.colorLive : Color.mOnSurface
                    }
                    NText {
                      visible: rep.free !== null && rep.free !== undefined
                      text: (root.svc ? root.svc.fmtSize(rep.free) : "") + " free"
                      pointSize: Style.fontSizeXXS
                      color: root.cMuted
                    }
                    Item {
                      Layout.fillWidth: true
                    }
                  }

                  // ! <err>
                  NText {
                    visible: !reach && rep.err !== undefined && rep.err !== null
                    Layout.leftMargin: Style.marginM
                    text: "! " + (rep.err || "unreachable")
                    pointSize: Style.fontSizeXXS
                    color: root.svc ? root.svc.colorSalmon : Color.mError
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                  }
                }
              }

              Item {
                implicitHeight: Style.marginXS
              }

              // ── key/value rows, same keys and order as the TUI ─────────────
              GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: Style.marginS
                rowSpacing: 1

                // local
                NText {
                  text: "local"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                  Layout.preferredWidth: 68
                }
                NText {
                  text: (card.modelData.paths || {}).local || "—"
                  pointSize: Style.fontSizeXXS
                  elide: Text.ElideMiddle
                  Layout.fillWidth: true
                }

                // remote
                NText {
                  text: "remote"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                }
                NText {
                  text: (card.modelData.paths || {}).remote_display || "—"
                  pointSize: Style.fontSizeXXS
                  elide: Text.ElideMiddle
                  Layout.fillWidth: true
                }

                // via
                NText {
                  text: "via"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                }
                NText {
                  text: root.viaLabel(card.modelData.via)
                  pointSize: Style.fontSizeXXS
                  color: root.svc ? root.svc.colorLive : Color.mOnSurface
                  elide: Text.ElideRight
                  Layout.fillWidth: true
                }

                // auto-sync
                NText {
                  text: "auto-sync"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                }
                NText {
                  text: root.autoLabel(card.modelData.auto)
                  pointSize: Style.fontSizeXXS
                  color: (card.modelData.auto && card.modelData.auto.mode !== "off") ? root.svc.colorLive : root.cMuted
                  elide: Text.ElideRight
                  Layout.fillWidth: true
                }

                // safety
                NText {
                  text: "safety"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                }
                NText {
                  text: {
                    var s = card.modelData.safety || {};
                    var l = card.modelData.local || {};
                    var r = card.rep_remote || {};
                    var rd = (r.deleted === null || r.deleted === undefined) ? "—" : r.deleted;
                    var rb = (r.backup === null || r.backup === undefined) ? "—" : r.backup;
                    return "soft-delete " + (s.soft_delete ? "on" : "off") + " " + (l.deleted || 0) + "/" + rd + " " + (s.soft_delete_days || "?") + "d" + "   conflict-bkp " + (s.conflict_backup ? "on" : "off") + " " + (l.backup || 0) + "/" + rb + " " + (s.conflict_backup_days || "?") + "d" + "   winner " + (s.winner || "newest");
                  }
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                  wrapMode: Text.WordWrap
                  Layout.fillWidth: true
                }

                // log
                NText {
                  text: "log"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                }
                NText {
                  text: card.modelData.log || "—"
                  pointSize: Style.fontSizeXXS
                  color: root.cLine
                  elide: Text.ElideMiddle
                  Layout.fillWidth: true
                }

                // excludes (only when set, same as the TUI)
                NText {
                  visible: (card.modelData.excludes || "") !== ""
                  text: "excludes"
                  pointSize: Style.fontSizeXXS
                  color: root.cMuted
                }
                NText {
                  visible: (card.modelData.excludes || "") !== ""
                  text: card.modelData.excludes || ""
                  pointSize: Style.fontSizeXXS
                  color: root.svc ? root.svc.colorGold : Color.mOnSurface
                  elide: Text.ElideRight
                  Layout.fillWidth: true
                }
              }
            }
          }
        }
      }
    }
  }
}
