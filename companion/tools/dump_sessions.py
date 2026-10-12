"""Developer tool: shows what Windows (GSMTC) really supplies for every media session - metadata plus the raw thumbnail.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\dump_sessions.py logs\\art-dump

It only READS (nothing is paused, skipped or changed). The raw thumbnail of each session is written next to a text report so the
artwork can be inspected with the image viewer.
"""
import asyncio
import io
import os
import sys

from PIL import Image
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SessionManager
from winrt.windows.storage.streams import DataReader


async def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    mgr = await SessionManager.request_async()
    sessions = list(mgr.get_sessions())
    print(f"{len(sessions)} session(s)")
    for i, s in enumerate(sessions):
        app = s.source_app_user_model_id
        print(f"\n[{i}] app={app!r}")
        try:
            props = await s.try_get_media_properties_async()
            info = s.get_playback_info()
        except Exception as exc:
            print(f"    not ready: {exc}")
            continue
        print(f"    status={info.playback_status!r} type={info.playback_type!r}")
        for name in ("title", "subtitle", "artist", "album_artist", "album_title", "album_track_count", "track_number"):
            print(f"    {name} = {ascii(getattr(props, name, None))}")
        try:
            print(f"    genres = {ascii(list(props.genres))}")
        except Exception as exc:
            print(f"    genres: {exc}")
        thumb = props.thumbnail
        if thumb is None:
            print("    thumbnail: None")
            continue
        stream = await thumb.open_read_async()
        size = int(stream.size)
        reader = DataReader(stream)
        await reader.load_async(size)
        data = bytearray(size)
        reader.read_bytes(data)
        raw = bytes(data)
        try:
            img = Image.open(io.BytesIO(raw))
            print(f"    thumbnail: {len(raw)} bytes, {img.format} {img.size[0]}x{img.size[1]} mode={img.mode}, content-type={stream.content_type!r}")
        except Exception as exc:
            print(f"    thumbnail: {len(raw)} bytes, not decodable: {exc}")
        safe = "".join(c if c.isalnum() else "_" for c in app)[:40]
        path = os.path.join(out_dir, f"{i}_{safe}.bin")
        with open(path, "wb") as f:
            f.write(raw)
        try:
            Image.open(io.BytesIO(raw)).convert("RGB").save(path.replace(".bin", ".png"))
        except Exception:
            pass
        print(f"    saved {path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "art-dump"))
