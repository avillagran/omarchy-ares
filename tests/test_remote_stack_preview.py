"""Remote stack tiles auto-load a bounded source preview without any click."""
import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTENDED = ROOT / "ExtendedWorkspace.qml"


def qml_function(name, source):
    match = re.search(r"^  function " + re.escape(name) + r"\([^)]*\) \{", source, re.M)
    if not match:
        raise AssertionError(f"missing QML function {name}")
    depth = 0
    for end in range(match.start(), len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():end + 1]
    raise AssertionError("unterminated function")


class RemoteStackPreviewTests(unittest.TestCase):
    def setUp(self):
        self.source = EXTENDED.read_text()

    def test_remote_tiles_request_bounded_source_preview_automatically(self):
        self.assertIn("function requestRemotePreview(roomId)", self.source)
        self.assertIn('bridge.request("GET", "/v1/rooms/" + encodeURIComponent(roomId) + "/preview", null',
                      self.source)
        # The remote branch runs before the local terminal-candidate early return.
        self.assertIn("if (tile.remotePeerRoom) {\n"
                      "        if (tile.warmViewport && tile.roomData) extended.requestRemotePreview(tile.roomData.id)",
                      self.source)

    def test_remote_preview_lines_render_until_terminal_paints(self):
        self.assertIn("readonly property var remoteCachedPreview: remotePeerRoom", self.source)
        self.assertIn("visible: tile.remotePeerRoom && !tileTerminalChat.surfacePainted", self.source)
        self.assertIn("model: tile.remoteCachedPreview", self.source)

    def test_terminalchat_hides_error_banner_when_preview_shows_content(self):
        terminal = (ROOT / "TerminalChat.qml").read_text()
        self.assertIn("property bool previewContentShown: false", terminal)
        self.assertIn(
            "&& !(terminalChat.previewReadOnly && terminalChat.previewContentShown && terminalChat.error !== \"\")",
            terminal)
        self.assertIn("previewContentShown: tile.remoteCachedPreview.length > 0", self.source)

    def test_request_remote_preview_maps_bounded_messages(self):
        function = qml_function("requestRemotePreview", self.source)
        script = function + """
var previewRequests = ({});
var roomPreviewCache = { rows: ({}), order: [] };
const extended = {
  get previewRequests() { return previewRequests },
  set previewRequests(value) { previewRequests = value },
  get roomPreviewCache() { return roomPreviewCache },
  set roomPreviewCache(value) { roomPreviewCache = value },
};
const TerminalPreviewCache = {
  store(cache, roomId, lines, cap) {
    const rows = Object.assign({}, cache.rows);
    rows[roomId] = lines;
    return { rows, order: cache.order.concat([roomId]) };
  },
};
const controller = { opened: true };
const bridge = {
  available: true,
  request(method, url, body, done) {
    done({ok: true, messages: [
      {role: 'user', text: 'first question'},
      {role: 'assistant', text: 'first answer'},
      {role: 'user', text: 'latest question   with   spaces'},
      {role: 'tool', text: 'not a chat message'},
    ]});
  },
};
requestRemotePreview('room_remote_1');
console.log(JSON.stringify({
  cached: extended.roomPreviewCache.rows['room_remote_1'],
  pending: extended.previewRequests,
}));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["pending"], {})
        self.assertEqual([line["summaryText"] for line in payload["cached"]], [
            "YOU · first question",
            "ASSISTANT · first answer",
            "YOU · latest question with spaces",
        ])


if __name__ == "__main__":
    unittest.main()
