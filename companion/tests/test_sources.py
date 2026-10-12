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

    def test_manual_beats_whatever_is_playing(self):
        order = m.candidate_order([SPOTIFY, CHROME], current="Spotify.exe", manual="chrome.exe")
        self.assertEqual(order[0], "chrome.exe")                      # a paused, pinned session stays on screen
        self.assertEqual(order[1], "Spotify.exe")

    def test_manual_selection_beats_another_playing_app_in_the_order(self):
        louder = [Source("Spotify.exe", "paused", "t"), Source("chrome.exe", "playing", "t"), Source("msedge.exe", "playing", "t")]
        self.assertEqual(m.candidate_order(louder, current="msedge.exe", manual="Spotify.exe")[0], "Spotify.exe")

    def test_unknown_manual_app_is_ignored(self):
        order = m.candidate_order([SPOTIFY, CHROME], current="Spotify.exe", manual="gone.exe")
        self.assertEqual(order[0], "Spotify.exe")
        self.assertNotIn("gone.exe", order)

    def test_wanted_beats_manual(self):                                  # a command is aimed at what the screen shows
        order = m.candidate_order([SPOTIFY, CHROME], manual="chrome.exe", wanted="spotify.exe")
        self.assertEqual(order[0], "Spotify.exe")

    def test_prefer_is_a_development_override(self):
        self.assertEqual(m.candidate_order([SPOTIFY, CHROME], prefer="chrome.exe")[0], "chrome.exe")

    def test_every_session_appears_once(self):
        order = m.candidate_order([SPOTIFY, CHROME, EDGE, DEAD], current="chrome.exe", manual="msedge.exe", last_playing="Spotify.exe")
        self.assertEqual(sorted(order), sorted(["Spotify.exe", "chrome.exe", "msedge.exe", "PSExpressCore.exe"]))


def S(app, state, title="t", artist="a"):
    return Source(app, state, title, artist)


class IsSpotify(unittest.TestCase):
    def test_identity_only(self):
        self.assertTrue(m.is_spotify("Spotify.exe"))
        self.assertTrue(m.is_spotify("spotify.exe"))
        self.assertTrue(m.is_spotify("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"))
        for other in ("chrome.exe", "msedge.exe", "vlc.exe", "SpotifyHelper.exe", "NotSpotify.exe", "python.exe", ""):
            self.assertFalse(m.is_spotify(other), other)


class AutoOrder(unittest.TestCase):
    def test_most_recently_started_playing_session_wins(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "playing")]
        self.assertEqual(m.candidate_order(srcs, current="Spotify.exe", since={"Spotify.exe": 10.0, "chrome.exe": 20.0})[0], "chrome.exe")
        self.assertEqual(m.candidate_order(srcs, current="chrome.exe", since={"Spotify.exe": 30.0, "chrome.exe": 20.0})[0], "Spotify.exe")

    def test_ties_fall_back_to_the_current_session_then_windows_order(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "playing")]
        self.assertEqual(m.candidate_order(srcs, current="chrome.exe")[0], "chrome.exe")
        self.assertEqual(m.candidate_order(srcs, current="")[0], "Spotify.exe")

    def test_a_playing_session_beats_a_paused_one_whatever_the_times(self):
        srcs = [S("Spotify.exe", "paused"), S("chrome.exe", "playing")]
        self.assertEqual(m.candidate_order(srcs, since={"Spotify.exe": 99.0}, last_playing="Spotify.exe")[0], "chrome.exe")


class Machine(unittest.TestCase):
    """The AUTO / MANUAL_TEMP state machine on scripted timelines (cases A-E of the owner's request)."""

    def setUp(self):
        self.sel = m.Selector(idle_return_s=20.0)

    def look(self, srcs, now, current=""):
        notes = self.sel.observe(srcs, now)
        return self.sel.order(srcs, current=current)[0], notes

    def swipe_to(self, app, srcs, now, current=""):
        self.sel.observe(srcs, now)
        return self.sel.select(app, srcs, current, now)

    # -- the default ------------------------------------------------------------------------------------------------------
    def test_auto_follows_whatever_starts_playing(self):
        self.assertEqual(self.look([S("Spotify.exe", "playing"), S("chrome.exe", "paused")], 0)[0], "Spotify.exe")
        self.assertEqual(self.look([S("Spotify.exe", "paused"), S("chrome.exe", "playing")], 5)[0], "chrome.exe")
        self.assertEqual(self.look([S("Spotify.exe", "playing"), S("chrome.exe", "paused")], 9)[0], "Spotify.exe")

    def test_both_playing_prefers_the_one_that_started_last(self):
        self.look([S("Spotify.exe", "playing"), S("chrome.exe", "paused")], 0)
        self.assertEqual(self.look([S("Spotify.exe", "playing"), S("chrome.exe", "playing")], 8)[0], "chrome.exe")

    def test_nothing_playing_keeps_the_session_that_played_last(self):
        self.look([S("Spotify.exe", "playing"), S("chrome.exe", "paused")], 0)
        self.assertEqual(self.look([S("Spotify.exe", "paused"), S("chrome.exe", "paused")], 5, current="chrome.exe")[0], "Spotify.exe")

    # -- case A -------------------------------------------------------------------------------------------------------------
    def test_case_a_manual_browse_then_spotify_starts_again(self):
        both = lambda sp, yt: [S("Spotify.exe", sp), S("chrome.exe", yt)]
        self.look(both("playing", "paused"), 0)
        self.assertIs(self.swipe_to("chrome.exe", both("playing", "paused"), 10), m.Mode.MANUAL_TEMP)
        self.assertEqual(self.look(both("playing", "paused"), 12)[0], "chrome.exe")        # still browsing the paused video
        self.assertEqual(self.look(both("paused", "paused"), 14)[0], "chrome.exe")         # Spotify paused: nothing else to follow
        first, notes = self.look(both("playing", "paused"), 16)                            # Spotify STARTS playing again
        self.assertEqual(first, "Spotify.exe")
        self.assertIs(self.sel.mode, m.Mode.AUTO)
        self.assertTrue(any("started playing" in n for n in notes))

    def test_case_a_spotify_just_keeps_playing_returns_after_the_idle_time(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 10)
        self.assertEqual(self.look(srcs, 25)[0], "chrome.exe")                             # 15 s: still "being inspected"
        self.sel.touch(26, "chrome.exe")                                                   # the user presses something
        self.assertEqual(self.look(srcs, 45)[0], "chrome.exe")                             # timer restarted at 26
        first, notes = self.look(srcs, 47)
        self.assertEqual(first, "Spotify.exe")
        self.assertIs(self.sel.mode, m.Mode.AUTO)
        self.assertTrue(any("no interaction" in n for n in notes))

    # -- case B -------------------------------------------------------------------------------------------------------------
    def test_case_b_browse_to_spotify_then_youtube_resumes(self):
        both = lambda sp, yt: [S("Spotify.exe", sp), S("chrome.exe", yt)]
        self.look(both("paused", "playing"), 0)
        self.assertIs(self.swipe_to("Spotify.exe", both("paused", "playing"), 5), m.Mode.MANUAL_TEMP)
        self.assertEqual(self.look(both("paused", "paused"), 8)[0], "Spotify.exe")          # YouTube paused: browsing continues
        self.assertEqual(self.look(both("paused", "playing"), 10)[0], "chrome.exe")         # YouTube resumed -> back to AUTO
        self.assertIs(self.sel.mode, m.Mode.AUTO)

    # -- case C -------------------------------------------------------------------------------------------------------------
    def test_case_c_pressing_play_on_the_selected_session_keeps_it(self):
        self.look([S("Spotify.exe", "paused"), S("chrome.exe", "paused")], 0)
        self.swipe_to("chrome.exe", [S("Spotify.exe", "paused"), S("chrome.exe", "paused")], 5)
        playing_yt = [S("Spotify.exe", "paused"), S("chrome.exe", "playing")]
        self.assertEqual(self.look(playing_yt, 7)[0], "chrome.exe")
        self.assertEqual(self.look(playing_yt, 60)[0], "chrome.exe")                        # no idle return: it IS the playing one

    def test_case_c_even_with_spotify_playing_in_the_background(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        both = [S("Spotify.exe", "playing"), S("chrome.exe", "playing")]
        self.assertEqual(self.look(both, 7)[0], "chrome.exe")
        self.assertEqual(self.look(both, 100)[0], "chrome.exe")

    # -- case D -------------------------------------------------------------------------------------------------------------
    def test_case_d_selected_session_disappears(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        first, notes = self.look([S("Spotify.exe", "playing")], 6)
        self.assertEqual(first, "Spotify.exe")
        self.assertIs(self.sel.mode, m.Mode.AUTO)
        self.assertTrue(any("disappeared" in n for n in notes))

    # -- case E -------------------------------------------------------------------------------------------------------------
    def test_case_e_several_paused_sessions_stay_browsable(self):
        srcs = [S("Spotify.exe", "paused"), S("chrome.exe", "paused"), S("msedge.exe", "paused")]
        self.look(srcs, 0)
        shown = "chrome.exe"
        for i in range(6):                                                                  # six swipes round the ring, a minute apart
            shown = m.step(srcs, shown, +1)
            self.swipe_to(shown, srcs, 60 * (i + 1))
            self.assertEqual(self.look(srcs, 60 * (i + 1) + 55)[0], shown)                 # still shown 55 s later: nothing else plays
        self.assertEqual(shown, "chrome.exe")                                               # six steps of a 3-ring = back at the start

    # -- details ------------------------------------------------------------------------------------------------------------
    def test_swiping_onto_what_auto_would_show_is_just_auto(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.assertIs(self.swipe_to("Spotify.exe", srcs, 5), m.Mode.AUTO)

    def test_a_new_playing_session_appearing_ends_the_manual_selection(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        first, _ = self.look(srcs + [S("msedge.exe", "playing")], 7)
        self.assertEqual(first, "msedge.exe")
        self.assertIs(self.sel.mode, m.Mode.AUTO)

    def test_track_change_blips_do_not_count_as_starting_to_play(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        for now, state in ((6, "changing"), (7, "playing"), (8, "opened"), (9, "playing")):    # Spotify skipping to the next track
            self.assertEqual(self.look([S("Spotify.exe", state), S("chrome.exe", "paused")], now)[0], "chrome.exe", state)
        self.assertIs(self.sel.mode, m.Mode.MANUAL_TEMP)

    def test_unreadable_sessions_never_trigger_anything(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        self.look(srcs + [DEAD], 6)
        self.assertIs(self.sel.mode, m.Mode.MANUAL_TEMP)

    def test_touching_another_session_does_not_restart_the_timer(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        self.sel.touch(20, "Spotify.exe")                  # a volume key aimed at the other app
        self.assertEqual(self.look(srcs, 26)[0], "Spotify.exe")

    def test_explicit_return_to_auto(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.swipe_to("chrome.exe", srcs, 5)
        self.sel.to_auto()
        self.assertEqual(self.sel.order(srcs)[0], "Spotify.exe")

    def test_the_wanted_app_still_wins_over_the_mode(self):
        srcs = [S("Spotify.exe", "playing"), S("chrome.exe", "paused")]
        self.look(srcs, 0)
        self.assertEqual(self.sel.order(srcs, wanted="chrome.exe")[0], "chrome.exe")

    def test_first_observation_of_playing_sessions(self):
        self.assertEqual(self.look([S("Spotify.exe", "playing"), S("chrome.exe", "playing")], 123.0, current="chrome.exe")[0], "chrome.exe")


if __name__ == "__main__":
    unittest.main()
