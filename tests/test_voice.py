import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice


class CommandForTests(unittest.TestCase):
    def test_on(self):
        for text in ("sound on", "Sound ON!", "Unmute.", "turn the sound on", "Turn sound on."):
            self.assertEqual(voice.command_for(text, False), "on", text)

    def test_off(self):
        for text in ("sound off", "Mute", "turn the sound off.", "no sound"):
            self.assertEqual(voice.command_for(text, False), "off", text)
            self.assertEqual(voice.command_for(text, True), "off", text)

    def test_lyrics(self):
        self.assertEqual(voice.command_for("Lyrics", False), "lyrics")
        self.assertEqual(voice.command_for("show lyrics!", True), "lyrics")
        self.assertEqual(voice.command_for("Hide lyrics.", False), "hide-lyrics")
        self.assertEqual(voice.command_for("lyrics off", True), "hide-lyrics")

    def test_fillers(self):
        self.assertEqual(voice.command_for("sound on please", False), "on")
        self.assertEqual(voice.command_for("okay unmute", False), "on")
        self.assertEqual(voice.command_for("Hey Marquee, sound on", False), "on")
        self.assertEqual(voice.command_for("marquee lyrics", False), "lyrics")
        self.assertEqual(voice.command_for("lyrics please", False), "lyrics")
        self.assertEqual(voice.command_for("marquee mute", True), "off")
        self.assertIsNone(voice.command_for("please mute", True))

    def test_ambient_speech(self):
        for text in (
            "I can't hear it, speak up",
            "I turned it on last week",
            "is the volume up in here",
            "can you turn it on",
            "we watched it with sound",
            "the lyrics are really good",
            "I need to mute my phone",
        ):
            self.assertIsNone(voice.command_for(text, False), text)
            self.assertIsNone(voice.command_for(text, True), text)

    def test_empty(self):
        self.assertIsNone(voice.command_for("", False))
        self.assertIsNone(voice.command_for("   ", True))
        self.assertIsNone(voice.command_for("please", False))


if __name__ == "__main__":
    unittest.main()
