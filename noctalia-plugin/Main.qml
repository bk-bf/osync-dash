// Main.qml — the plugin's single data source and shared state.
//
// This addon reuses osync-dash itself as its backend: it runs
// `osync-dash --json`, the stable machine-readable contract added to
// osync_core.py for exactly this purpose. No probing, health logic or log
// parsing is reimplemented here — the same gather()/health() code that powers
// the TUI and `--print` produces this JSON.
//
// BarWidget.qml and Panel.qml both read this instance through
// pluginApi.mainInstance, so one process runs per interval regardless of how
// many bars show the widget.

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

Item {
  id: root

  property var pluginApi

  // ── settings ────────────────────────────────────────────────────────────────
  readonly property var cfg: pluginApi ? pluginApi.pluginSettings : ({})
  readonly property string binPath: String((cfg && cfg.binPath) || "osync-dash").trim()
  readonly property int intervalMs: Math.max(5000, (cfg && cfg.intervalMs) || 20000)
  readonly property bool localOnly: (cfg && cfg.localOnly !== undefined) ? cfg.localOnly : false
  readonly property string barMetric: (cfg && cfg.barMetric) || "auto"
  readonly property bool colorByHealth: (cfg && cfg.colorByHealth !== undefined) ? cfg.colorByHealth : true
  readonly property string terminalCmd: String((cfg && cfg.terminalCmd) || "kitty").trim()

  // Run through `sh -c` so a leading ~ in the configured path expands.
  // (Consequence: the path must not contain spaces.)
  readonly property string command: binPath + " --json" + (localOnly ? " --local-only" : "")

  // ── state ───────────────────────────────────────────────────────────────────
  property var payload: null // last good --json body
  property string errorText: ""
  property bool loading: false
  property double lastOkMs: 0
  property int tick: 0

  readonly property bool ok: payload !== null && errorText === ""
  readonly property var summary: (payload && payload.summary) || null
  readonly property var connections: (payload && payload.connections) || []

  // Liveness from the ~3Hz lock-file poll: { name: running }. The full probe is
  // far too slow to catch a sync that lasts seconds, so this is the source of
  // truth for "is it running right now" and the full payload supplies the rest.
  property var liveRunning: ({})

  function isRunning(c) {
    if (!c)
      return false;
    var v = liveRunning[c.name];
    return (v === undefined) ? !!c.running : v;
  }

  // Live change counts summed across every connection — what is waiting to move
  // in each direction since the last successful sync.
  readonly property int pushChanges: {
    var n = 0;
    for (var i = 0; i < connections.length; i++)
      n += Number(connections[i].push_changes || 0);
    return n;
  }
  readonly property int pullChanges: {
    var n = 0;
    for (var i = 0; i < connections.length; i++)
      n += Number(connections[i].pull_changes || 0);
    return n;
  }
  readonly property bool anyRunning: {
    for (var i = 0; i < connections.length; i++) {
      if (isRunning(connections[i]))
        return true;
    }
    return false;
  }
  readonly property bool anyProblem: summary !== null && summary.problems > 0

  // "Offline" is reachability specifically — the tool failing to run, or a
  // replica we can't see — as opposed to a sync that merely errored.
  readonly property bool anyOffline: {
    if (!ok)
      return true;
    for (var i = 0; i < connections.length; i++) {
      var h = connections[i].health;
      if (h === "TARGET UNREACHABLE" || h === "NO LOCAL DIR")
        return true;
    }
    return false;
  }

  // Semantic colours lifted from the TUI so the panel reads identically to it.
  // The TUI uses the ayu palette; MINT is replaced by a slightly bluer green.
  readonly property color colorLive: "#6FB2A4"  // good / in sync / online
  readonly property color colorGold: "#E6B450"  // remote accent · transferring
  readonly property color colorSalmon: "#F28779" // bad / offline
  readonly property color colorBlue: "#73D0FF"  // pending counts
  readonly property color colorOffline: colorSalmon

  // core.health() colour names -> the same mapping the TUI's HC table uses
  function healthColorOf(name) {
    switch (name) {
    case "green":
      return colorLive;
    case "yellow":
      return colorGold;
    case "red":
      return colorSalmon;
    case "blue":
      return colorBlue;
    default:
      return Color.mOnSurfaceVariant;
    }
  }

  // One ↑push / ↓pull leg, matching pushpull_line() in the TUI exactly.
  // Returns { text, color }.
  function leg(c, arrow, verb, cnt) {
    var d = c.direction;
    var relevant = (verb === "push") ? (d === "send" || d === "bidir") : (d === "receive" || d === "bidir");
    if (!relevant)
      return {
        "text": arrow + " " + verb + " off",
        "color": Color.mOutline
      };
    // osync moves one direction at a time, so only a leg with something to
    // move shows "transferring…" — never both for nothing.
    if (isRunning(c) && (cnt === null || cnt === undefined || cnt > 0))
      return {
        "text": spinnerFrames2[spinFrame % spinnerFrames2.length] + " " + verb + " transferring…",
        "color": colorGold
      };
    if (cnt === null || cnt === undefined)
      return {
        "text": arrow + " " + verb + " —",
        "color": Color.mOutline
      };
    if (cnt > 0)
      return {
        // the verb is already in the label — "1 to push" said it twice
        "text": arrow + " " + verb + " " + cnt + " file" + (cnt === 1 ? "" : "s"),
        "color": colorBlue
      };
    return {
      "text": arrow + " " + verb + " in sync",
      "color": colorLive
    };
  }

  // the TUI's braille spinner, used for the transferring legs
  readonly property var spinnerFrames2: ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

  // Spinner frames cycled while a sync is in flight. BarPill renders its icon
  // internally so it can't be rotated from here; swapping frames is the way to
  // animate it.
  readonly property var spinnerFrames: ["loader-quarter", "loader-2", "loader-3", "loader"]
  property int spinFrame: 0

  // Colour of the status dot that leads the pill. Unreachable beats live, live
  // beats a stale/errored sync, and a healthy connection is green — the dot is
  // always coloured, since it *is* the at-a-glance signal.
  readonly property color healthColor: {
    if (anyOffline)
      return colorOffline;
    if (anyRunning || !anyProblem)
      return colorLive;
    return healthColorOf(summary ? summary.worst_color : "grey");
  }

  // ── formatting helpers (also used by Panel.qml) ─────────────────────────────
  function fmtSize(n) {
    if (n === null || n === undefined || isNaN(n))
      return "—";
    var v = Number(n);
    var units = ["B", "K", "M", "G", "T"];
    var i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return (v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)) + units[i];
  }

  function fmtAgeSecs(s) {
    if (s === null || s === undefined || isNaN(s))
      return "never";
    var t = Math.max(0, Math.round(Number(s)));
    if (t < 60)
      return t + "s ago";
    if (t < 3600)
      return Math.floor(t / 60) + "m ago";
    if (t < 86400)
      return Math.floor(t / 3600) + "h ago";
    return Math.floor(t / 86400) + "d ago";
  }

  // Live age from an absolute epoch stamp. The payload's *_age fields are
  // computed once, when the probe ran, so rendering those makes the label freeze
  // between probes and then jump. Deriving it from the timestamp against the
  // 1s tick is what makes it an actual ticking clock, like the TUI's _clock_tick.
  function fmtAgeFrom(ts) {
    tick; // dependency: re-evaluate every second
    if (!ts)
      return "never";
    return fmtAgeSecs(Date.now() / 1000 - Number(ts));
  }

  function fmtAgeMs(ms) {
    if (!ms)
      return "never";
    return fmtAgeSecs((Date.now() - ms) / 1000);
  }

  // What the last completed osync run actually moved. osync's own counters fold
  // deletions into each direction's total (pushed = updates + deletions), so the
  // deletion count is shown as a parenthetical rather than added on top.
  // Just the counts — callers supply the "moved" label, so this must not repeat
  // it. Empty string when the last run moved nothing, matching the TUI, which
  // omits the whole clause rather than saying "moved nothing".
  function lastRunText(c) {
    var lr = (c && c.state) ? c.state.last_run : null;
    if (!lr)
      return "";
    var out = Number(lr.pushed || 0);
    var inn = Number(lr.pulled || 0);
    if (out === 0 && inn === 0)
      return "";
    return "↑" + out + " ↓" + inn;
  }

  function dirGlyph(d) {
    switch (d) {
    case "send":
      return "→";
    case "receive":
      return "←";
    default:
      return "⇄";
    }
  }

  readonly property string lastOkLabel: {
    tick;
    return fmtAgeMs(lastOkMs);
  }

  // ── bar presentation ────────────────────────────────────────────────────────
  readonly property string barLabel: {
    if (!ok)
      return "osync ?";
    if (!summary || summary.total === 0)
      return "no jobs";

    switch (barMetric) {
    case "health":
      return String(summary.worst || "").toLowerCase();
    case "counts":
      return summary.healthy + "/" + summary.total;
    case "changes":
      return "↑" + pushChanges + " ↓" + pullChanges;
    default:
      // auto — always the pending counts, never a word. The icon already says
      // running (spinner) and unreachable (red), and swapping the label between
      // "syncing"/"offline"/counts resizes the pill, which shoves every widget
      // beside it along the bar. The label keeps one shape so the bar stays put.
      return "↑" + pushChanges + " ↓" + pullChanges;
    }
  }

  readonly property string barIconName: {
    if (!ok)
      return "alert-circle";
    if (anyRunning)
      return spinnerFrames[spinFrame % spinnerFrames.length];
    if (anyOffline)
      return "server-off";
    if (anyProblem)
      return "alert-triangle";
    // point-filled, not circle-filled: the latter fills the whole em box and
    // reads as an oversized blob next to the text.
    return "point-filled";
  }

  readonly property string tooltipText: {
    tick;
    if (!ok)
      return "osync unavailable\n" + (errorText || "no data") + "\n" + command;
    if (!summary || summary.total === 0)
      return "osync — no connections defined";

    var lines = [];
    for (var i = 0; i < connections.length; i++) {
      var c = connections[i];
      lines.push(c.name + "  " + dirGlyph(c.direction) + "  " + c.health.toLowerCase());
      if (c.running) {
        lines.push("   syncing…");
      } else {
        var moved = lastRunText(c);
        lines.push("   " + (moved ? "moved " + moved + "  ·  " : "") + "last sync " + fmtAgeFrom(c.last_sync_ts));
      }
    }
    lines.push("updated " + fmtAgeMs(lastOkMs));
    return lines.join("\n");
  }

  // ── running osync-dash ──────────────────────────────────────────────────────
  function refresh() {
    if (loading || proc.running)
      return;
    if (!binPath) {
      errorText = "no osync-dash path configured";
      return;
    }
    loading = true;
    proc.running = true;
  }

  function _ingest(text) {
    var s = String(text || "").trim();
    if (s === "")
      return; // failure path: exit handler reports it
    try {
      root.payload = JSON.parse(s);
      root.errorText = "";
      root.lastOkMs = Date.now();
    } catch (e) {
      root.errorText = "invalid JSON from " + root.binPath;
      Logger.w("osync-dash", "parse failed:", e);
    }
  }

  Process {
    id: proc
    command: ["sh", "-c", root.command]

    stdout: StdioCollector {
      onStreamFinished: root._ingest(this.text)
    }
    stderr: StdioCollector {
      id: errCollector
    }

    onExited: (exitCode, exitStatus) => {
      root.loading = false;
      if (exitCode === 0)
        return;
      var err = String(errCollector.text || "").trim();
      root.errorText = err !== "" ? err.split("\n").pop() : ("exited " + exitCode);
    }
  }

  // ── liveness poll ───────────────────────────────────────────────────────────
  // osync's lock windows are only ~1.5s, so catching a sync needs ~1Hz polling.
  // `osync-dash --status` would be correct but costs ~90ms of Python startup per
  // call — far too much to run every second forever. Instead we stat the lock
  // paths the core reported in `lock_file`, which is a couple of milliseconds and
  // keeps the path layout owned by the core rather than hardcoded here.
  // A start/stop transition immediately triggers the full probe, so the counts
  // are right the moment a sync ends instead of up to an interval later.
  readonly property string lockCmd: {
    var parts = [];
    for (var i = 0; i < connections.length; i++) {
      var c = connections[i];
      if (!c.lock_file)
        continue;
      parts.push('if [ -e "' + c.lock_file + '" ]; then echo "' + c.name + ' 1"; else echo "' + c.name + ' 0"; fi');
    }
    return parts.join("; ");
  }

  Process {
    id: statusProc
    command: ["sh", "-c", root.lockCmd]

    stdout: StdioCollector {
      onStreamFinished: {
        var s = String(this.text || "").trim();
        if (s === "")
          return;
        var next = {};
        var changed = false;
        var lines = s.split("\n");
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (line === "")
            continue;
          // "<name> <0|1>" — the name may contain spaces, so split off the flag
          var cut = line.lastIndexOf(" ");
          if (cut < 0)
            continue;
          var name = line.substring(0, cut);
          var run = line.substring(cut + 1) === "1";
          next[name] = run;
          if (run !== !!root.liveRunning[name])
            changed = true;
        }
        root.liveRunning = next;
        if (changed)
          root.refresh(); // a sync just started or ended — get real numbers
      }
    }
    stderr: StdioCollector {}
  }

  function pollStatus() {
    if (!statusProc.running && lockCmd !== "")
      statusProc.running = true;
  }

  function openTui() {
    if (terminalCmd && binPath)
      Quickshell.execDetached(["sh", "-c", terminalCmd + " -e " + binPath]);
  }

  onCommandChanged: {
    payload = null;
    errorText = "";
    refresh();
  }

  Timer {
    // Full probe: ssh + tree walk. Kept slow because liveness no longer depends
    // on it — statusProc catches starts/stops and forces a refresh on each.
    interval: root.anyRunning ? 3000 : root.intervalMs
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    // liveness — a stat is a rounding error next to what a CPU/RAM widget does
    // every second, so sample well inside osync's ~1.5s lock window
    interval: 300
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.pollStatus()
  }

  Timer {
    // spinner animation, matching the TUI's 0.12s tick
    interval: 120
    running: root.anyRunning
    repeat: true
    onTriggered: root.spinFrame = (root.spinFrame + 1) % root.spinnerFrames.length
  }

  Timer {
    // 1s: drives every relative-age label, same as the TUI's _clock_tick
    interval: 1000
    running: true
    repeat: true
    onTriggered: root.tick++
  }

  Component.onCompleted: Logger.i("osync-dash", "plugin started, running:", command)
}
