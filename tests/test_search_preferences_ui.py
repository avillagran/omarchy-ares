"""Static contract: preference construction must never submit a write."""
import pathlib
import re
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "PreferencesWorkspace.qml"


class SearchPreferencesUiTests(unittest.TestCase):
    def test_general_shortcut_is_user_activation_only(self):
        source = SOURCE.read_text()
        general = source[source.index("id: generalColumn"):source.index('text: "TERMINAL APPEARANCE"')]
        selector = re.search(r'ComboBox\s*\{[^{}]*objectName:\s*"searchShortcutSelector"[^{}]*\}', general)
        self.assertIsNotNone(selector, "GENERAL needs a search shortcut ComboBox")
        assert selector is not None
        block = selector.group(0)
        self.assertIn('model: ["Ctrl+Space", "Alt+Space"]', block)
        self.assertRegex(block, r'currentIndex:\s*controller.searchShortcut === "Alt\+Space" \? 1 : 0')
        self.assertIn('onActivated: controller.setSearchShortcut(currentText)', block)
        self.assertNotIn('onCurrentIndexChanged', block)
        self.assertNotIn('onCurrentTextChanged', block)
        self.assertNotIn('Component.onCompleted', block)
        self.assertEqual(source.count('controller.setSearchShortcut('), 1)
        self.assertIn('qsTr("Search shortcut")', general)
        self.assertIn('only while Ares is focused', general)
        self.assertIn('does not change Hyprland bindings', general)


if __name__ == '__main__':
    unittest.main()
