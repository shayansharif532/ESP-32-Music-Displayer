"""Read-only analysis of a full ESP32-S3 flash dump. Prints layout + firmware identity.
Never prints NVS *values* (only namespaces/keys) so no secrets land in logs."""
import hashlib
import struct
import sys

path = sys.argv[1]
data = open(path, "rb").read()
print(f"size            : {len(data):,} bytes ({len(data) / 1048576:.2f} MiB)")
print(f"sha256          : {hashlib.sha256(data).hexdigest()}")
print(f"md5             : {hashlib.md5(data, usedforsecurity=False).hexdigest()}")

last = len(data) - 1
while last > 0 and data[last] == 0xFF:
    last -= 1
print(f"last non-0xFF   : 0x{last:08x}  (everything after is erased flash)")

# --- 2nd stage bootloader header @ 0x0 (ESP32-S3 bootloader lives at 0x0) ---
magic, segs, flash_mode, size_freq = struct.unpack_from("<BBBB", data, 0)
modes = {0: "QIO", 1: "QOUT", 2: "DIO", 3: "DOUT"}
sizes = {0: "1MB", 1: "2MB", 2: "4MB", 3: "8MB", 4: "16MB", 5: "32MB", 6: "64MB", 7: "128MB"}
freqs = {0: "80MHz", 1: "40MHz", 2: "26MHz", 3: "20MHz", 0xF: "120MHz"}
print(f"bootloader@0x0  : magic=0x{magic:02x} segments={segs} "
      f"flash_mode={modes.get(flash_mode, flash_mode)} "
      f"size={sizes.get(size_freq >> 4, size_freq >> 4)} freq={freqs.get(size_freq & 0xF, size_freq & 0xF)}")

# --- partition table @ 0x8000 ---
print("\npartition table @ 0x8000:")
PT = 0x8000
types = {0: "app", 1: "data"}
app_sub = {0x00: "factory", 0x20: "test"}
for i in range(0x10, 0x20):
    app_sub[i] = f"ota_{i - 0x10}"
data_sub = {0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump", 0x04: "nvs_keys", 0x05: "efuse_em",
            0x80: "esphttpd", 0x81: "fat", 0x82: "spiffs", 0x83: "littlefs"}
parts = []
for n in range(0, 0xC00 // 32):
    ent = data[PT + n * 32: PT + n * 32 + 32]
    m, t, st, off, sz, name, flags = struct.unpack("<HBBII16sI", ent)
    if m == 0xEBEB:  # MD5 marker
        print("  (md5 marker)")
        break
    if m != 0x50AA:  # ESP_PARTITION_MAGIC; bytes on flash are AA 50
        break
    name = name.split(b"\0")[0].decode(errors="replace")
    sub = (app_sub if t == 0 else data_sub).get(st, hex(st))
    parts.append((name, t, st, off, sz))
    print(f"  {name:<12} type={types.get(t, t):<4} sub={sub:<9} offset=0x{off:08x} size=0x{sz:08x} ({sz / 1024:>8.0f} KiB)")

# --- app images: print esp_app_desc_t ---
def app_desc(off):
    img = data[off: off + 0x200]
    if img[0] != 0xE9:
        return None
    # image header 24 bytes + first segment header 8 bytes -> app desc at +0x20
    d = img[0x20:0x20 + 0x100]
    if struct.unpack_from("<I", d, 0)[0] != 0xABCD5432:
        return None
    cs = lambda a, b: d[a:b].split(b"\0")[0].decode(errors="replace")
    return dict(version=cs(16, 48), project=cs(48, 80), time=cs(80, 96), date=cs(96, 112), idf=cs(112, 144))

print("\napp images:")
for name, t, st, off, sz in parts:
    if t == 0:
        a = app_desc(off)
        if a:
            print(f"  {name:<10} @0x{off:08x}: project='{a['project']}' version='{a['version']}' "
                  f"built {a['date']} {a['time']} IDF={a['idf']}")
        else:
            blank = all(b == 0xFF for b in data[off:off + 256])
            print(f"  {name:<10} @0x{off:08x}: {'EMPTY (erased)' if blank else 'no valid app header'}")

# --- NVS: list namespaces & key names only (NO values) ---
print("\nNVS contents (keys only, values deliberately not printed):")
for name, t, st, off, sz in parts:
    if t == 1 and st == 0x02:
        region = data[off: off + sz]
        ns = {}      # namespace index -> name
        entries = []  # (nsidx, key)
        for page in range(0, len(region), 4096):
            pg = region[page: page + 4096]
            if len(pg) < 4096:
                break
            if struct.unpack_from("<I", pg, 0)[0] == 0xFFFFFFFF:  # uninitialised page
                continue
            bitmap = pg[32:64]  # 2 bits/entry: 0b10 = written, 0b11 = empty, 0b00 = erased
            e = 0
            while e < 126:
                st = (bitmap[e // 4] >> ((e % 4) * 2)) & 3
                if st != 2:
                    e += 1
                    continue
                ent = pg[64 + e * 32: 64 + e * 32 + 32]
                nsidx, typ, span = ent[0], ent[1], ent[2]
                key = ent[8:24].split(b"\0")[0].decode("ascii", errors="replace")
                if nsidx == 0:
                    ns[ent[24]] = key  # namespace definition entry: data byte 0 = new ns index
                else:
                    entries.append((nsidx, key))
                e += max(span, 1)  # skip the data-only entries that follow strings/blobs
        if not entries and not ns:
            print("  (empty)")
        else:
            byns = {}
            for nsidx, key in entries:
                byns.setdefault(ns.get(nsidx, f"ns#{nsidx}"), []).append(key)
            for k, v in sorted(byns.items()):
                print(f"  [{k}] {', '.join(sorted(set(v)))}")
            empties = [n for n in ns.values() if n not in byns]
            if empties:
                print(f"  (namespaces with no live keys: {', '.join(empties)})")
