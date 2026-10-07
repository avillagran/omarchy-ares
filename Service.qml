import QtQuick
import Quickshell.Io

Item {
  id: root
  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "").replace(/\/$/, "")

  Process {
    id: ensureService
    command: [root.pluginDir + "/bin/ares-server", "connection"]
    running: true
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
  }

  Timer {
    interval: 15000
    repeat: true
    running: true
    onTriggered: if (!ensureService.running) ensureService.running = true
  }
}
