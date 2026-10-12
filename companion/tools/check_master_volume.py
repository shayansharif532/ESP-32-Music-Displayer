"""Developer check: proves the per-app volume calls leave the Windows MASTER volume (and every other app) alone.

Reads the master volume of every active playback device and the volume of every audio session, calls the companion's
volume-down / volume-up (which act on the displayed app), reads everything again and reports what changed.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\check_master_volume.py [http://127.0.0.1:8765]
Only run it while the displayed session is one you do not mind changing by 5 % (the test restores it).
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desk_media_companion import volume  # noqa: E402


def masters() -> dict:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, EDataFlow, IAudioEndpointVolume
    import comtypes
    comtypes.CoInitialize()
    out = {}
    enum = AudioUtilities.GetDeviceEnumerator()
    devs = enum.EnumAudioEndpoints(EDataFlow.eRender.value, 1)
    for i in range(devs.GetCount()):
        dev = devs.Item(i)
        ep = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
        out[f"device{i}"] = (round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute()))
    return out


def post(base: str, path: str) -> dict:
    req = urllib.request.Request(base + path, method="POST", headers={"X-Desk-Client": "check"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    m0 = masters()
    s0 = volume.list_sessions()
    r1 = post(base, "/control/volume-down")
    m1 = masters()
    s1 = volume.list_sessions()
    r2 = post(base, "/control/volume-up")
    m2 = masters()
    print("app:", r1.get("app"), " volume after down:", r1.get("volume"), " after up:", r2.get("volume"))
    print("master volume per device  before:", m0)
    print("master volume per device  during:", m1)
    print("master volume per device  after :", m2)
    print("MASTER VOLUME CHANGED:", m0 != m1 or m0 != m2)
    changed = [(a, b) for a, b in zip(s0, s1) if a["volume"] != b["volume"]]
    print("audio sessions whose volume changed during the down step:")
    for a, b in changed:
        print(f"   {a['process']} (pid {a['pid']}): {a['volume']} -> {b['volume']}")
    others = [a["process"] for a, b in changed if a["process"] != r1.get("app") and a["process"].lower() != str(r1.get("app")).lower()]
    print("UNRELATED APPS CHANGED:", others or "none")


if __name__ == "__main__":
    main()
