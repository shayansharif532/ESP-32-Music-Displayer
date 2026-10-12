"""Session-switching model (companion/desk_media_companion/sources.py): which sessions are offered, in which order a vertical
swipe walks through them, and which session the display follows. Pure logic, no Windows APIs needed.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion import sources as m                       # noqa: E402
from desk_media_companion.sources import Source                      # noqa: E402

SPOTIFY = Source("Spotify.exe", "playing", "Ready", "Lil Baby")
CHROME = Source("chrome.exe", "paused", "Always Lone", "Men I Trust")
EDGE = Source("msedge.exe", "paused", "Some video", "Channel")
DEAD = Source("PSExpressCore.exe", "", readable=False)
BLANK = Source("blank.exe", "stopped", "", "")


class FriendlyName(unittest.TestCase):
    def test_known_apps(self):
        self.assertEqual(m.friendly_name("Spotify.exe"), "Spotify")
        self.assertEqual(m.friendly_name("chrome.exe"), "Chrome")
        self.assertEqual(m.friendly_name("msedge.exe"), "Edge")
        self.assertEqual(m.friendly_name("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic"), "Media Player")
        self.assertEqual(m.friendly_name("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"), "Spotify")

    def test_unknown_apps_are_tidied(self):
        self.assertEqual(m.friendly_name("SomePlayer.exe"), "SomePlayer")
        self.assertEqual(m.friendly_name("foo_player.exe"), "Foo player")
        self.assertEqual(m.friendly_name(""), "Unknown")
        self.assertLessEqual(len(m.friendly_name("A" * 80 + ".exe")), 23)


class Usable(unittest.TestCase):
    def test_usable(self):
        self.assertTrue(m.is_usable(SPOTIFY))
        self.assertTrue(m.is_usable(CHROME))                         # paused sessions with metadata count
        self.assertTrue(m.is_usable(Source("x.exe", "playing", "", "Only an artist")))

    def test_not_usable(self):
        self.assertFalse(m.is_usable(DEAD))                          # device not ready
        self.assertFalse(m.is_usable(BLANK))                         # nothing to show
        self.assertFalse(m.is_usable(Source("x.exe", "closed", "Title")))
        self.assertFalse(m.is_usable(Source("x.exe", "paused", "   ", "  ")))


class Ring(unittest.TestCase):
    def test_stable_alphabetical_ring_without_junk(self):
        srcs = [SPOTIFY, DEAD, CHROME, BLANK, EDGE]
        self.assertEqual(m.ring(srcs), ["chrome.exe", "msedge.exe", "Spotify.exe"])
        self.assertEqual(m.ring(list(reversed(srcs))), m.ring(srcs))   # does not depend on the order Windows lists them in

    def test_displayed_session_is_always_in_the_ring(self):
        self.assertEqual(m.ring([SPOTIFY, BLANK], "blank.exe"), ["blank.exe", "Spotify.exe"])
        self.assertEqual(m.ring([SPOTIFY], "gone.exe"), ["Spotify.exe"])    # not a session at all: ignored

    def test_state_changes_do_not_reorder_the_ring(self):
        a = [Source("Spotify.exe", "playing", "t"), Source("chrome.exe", "paused", "t")]
        b = [Source("Spotify.exe", "paused", "t"), Source("chrome.exe", "playing", "t")]
        self.assertEqual(m.ring(a), m.ring(b))


class Stepping(unittest.TestCase):
    def test_next_and_previous_wrap_around(self):
        srcs = [SPOTIFY, CHROME, EDGE]                                # ring: chrome, msedge, Spotify
        self.assertEqual(m.step(srcs, "chrome.exe", +1), "msedge.exe")
        self.assertEqual(m.step(srcs, "msedge.exe", +1), "Spotify.exe")
        self.assertEqual(m.step(srcs, "Spotify.exe", +1), "chrome.exe")     # wraps
        self.assertEqual(m.step(srcs, "chrome.exe", -1), "Spotify.exe")     # wraps backwards
        self.assertEqual(m.step(srcs, "Spotify.exe", -1), "msedge.exe")

    def test_two_sources_toggle(self):
        srcs = [SPOTIFY, CHROME]
        self.assertEqual(m.step(srcs, "Spotify.exe", +1), "chrome.exe")
        self.assertEqual(m.step(srcs, "chrome.exe", +1), "Spotify.exe")
        self.assertEqual(m.step(srcs, "Spotify.exe", -1), "chrome.exe")

    def test_one_source_does_nothing(self):
        self.assertIsNone(m.step([SPOTIFY], "Spotify.exe", +1))
        self.assertIsNone(m.step([SPOTIFY, DEAD, BLANK], "Spotify.exe", -1))
        self.assertIsNone(m.step([], "", +1))

    def test_unknown_displayed_app_starts_at_the_ends(self):
        srcs = [SPOTIFY, CHROME]
        self.assertEqual(m.step(srcs, "gone.exe", +1), "chrome.exe")
        self.assertEqual(m.step(srcs, "gone.exe", -1), "Spotify.exe")

    def test_case_insensitive(self):
        self.assertEqual(m.step([SPOTIFY, CHROME], "SPOTIFY.EXE", +1), "chrome.exe")

    def test_position(self):
        srcs = [SPOTIFY, CHROME, EDGE]
        self.assertEqual(m.position(srcs, "chrome.exe"), (1, 3))
        self.assertEqual(m.position(srcs, "Spotify.exe"), (3, 3))
        self.assertEqual(m.position([SPOTIFY], "Spotify.exe"), (1, 1))


class Candidates(unittest.TestCase):
    def test_auto_prefers_the_playing_current_session(self):
        self.assertEqual(m.candidate_order([CHROME, SPOTIFY], current="chrome.exe")[0], "Spotify.exe")   # current is paused
        playing_chrome = Source("chrome.exe", "playing", "Video")
        self.assertEqual(m.candidate_order([SPOTIFY, playing_chrome], current="chrome.exe")[0], "chrome.exe")

    def test_auto_falls_back_to_last_playing_then_current(self):
        both_paused = [Source("Spotify.exe", "paused", "t"), CHROME]
        self.assertEqual(m.candidate_order(both_paused, current="chrome.exe", last_playing="Spotify.exe")[0], "Spotify.exe")
        self.assertEqual(m.candidate_order(both_paused, current="chrome.exe")[0], "chrome.exe")

    def test_pin_beats_whatever_is_playing(self):
        order = m.candidate_order([SPOTIFY, CHROME], current="Spotify.exe", pinned="chrome.exe")
        self.assertEqual(order[0], "chrome.exe")                      # a paused, pinned session stays on screen
        self.assertEqual(order[1], "Spotify.exe")

    def test_pin_survives_another_app_becoming_active(self):
        louder = [Source("Spotify.exe", "paused", "t"), Source("chrome.exe", "playing", "t"), Source("msedge.exe", "playing", "t")]
        self.assertEqual(m.candidate_order(louder, current="msedge.exe", pinned="Spotify.exe")[0], "Spotify.exe")

    def test_vanished_pin_falls_back_to_automatic(self):
        order = m.candidate_order([SPOTIFY, CHROME], current="Spotify.exe", pinned="gone.exe")
        self.assertEqual(order[0], "Spotify.exe")
        self.assertNotIn("gone.exe", order)

    def test_wanted_beats_pin(self):                                  # a command is aimed at what the screen shows
        order = m.candidate_order([SPOTIFY, CHROME], pinned="chrome.exe", wanted="spotify.exe")
        self.assertEqual(order[0], "Spotify.exe")

    def test_prefer_is_a_development_pin(self):
        self.assertEqual(m.candidate_order([SPOTIFY, CHROME], prefer="chrome.exe")[0], "chrome.exe")

    def test_every_session_appears_once(self):
        order = m.candidate_order([SPOTIFY, CHROME, EDGE, DEAD], current="chrome.exe", pinned="msedge.exe", last_playing="Spotify.exe")
        self.assertEqual(sorted(order), sorted(["Spotify.exe", "chrome.exe", "msedge.exe", "PSExpressCore.exe"]))


if __name__ == "__main__":
    unittest.main()
