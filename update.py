"""
Auto-update FHD+ IPTV playlist.
1. Fetch fresh streams from iptv-org API
2. Filter FHD+ (1080p, 2160p, 4320p)
3. Test URLs with iptv-checker (ffprobe-based, honors per-channel UA/referrer), remove dead ones
4. Add EPG guide URLs
5. Write final M3U with metadata
"""
import json, os, re, shutil, subprocess, tempfile
from urllib.request import Request, urlopen

STREAMS_API = "https://iptv-org.github.io/api/streams.json"
CHANNELS_API = "https://iptv-org.github.io/api/channels.json"
OUT = os.path.join(os.path.dirname(__file__), "fhd_channels.m3u")
README = os.path.join(os.path.dirname(__file__), "README.md")
FHD_QUALITIES = {"1080p", "2160p", "4320p"}
CHECKER_PARALLEL = 50
CHECKER_TIMEOUT_MS = 15000

# EPG sources by country code — publicly available XMLTV guides
EPG_SOURCES = {
    "US": "https://epg.pw/xmltv/epg_US.xml.gz",
    "UK": "https://epg.pw/xmltv/epg_UK.xml.gz",
    "DE": "https://epg.pw/xmltv/epg_DE.xml.gz",
    "FR": "https://epg.pw/xmltv/epg_FR.xml.gz",
    "ES": "https://epg.pw/xmltv/epg_ES.xml.gz",
    "IT": "https://epg.pw/xmltv/epg_IT.xml.gz",
    "NL": "https://epg.pw/xmltv/epg_NL.xml.gz",
    "TR": "https://epg.pw/xmltv/epg_TR.xml.gz",
    "RU": "https://epg.pw/xmltv/epg_RU.xml.gz",
    "IN": "https://epg.pw/xmltv/epg_IN.xml.gz",
    "SA": "https://epg.pw/xmltv/epg_SA.xml.gz",
    "BR": "https://epg.pw/xmltv/epg_BR.xml.gz",
    "CA": "https://epg.pw/xmltv/epg_CA.xml.gz",
    "AU": "https://epg.pw/xmltv/epg_AU.xml.gz",
    "PL": "https://epg.pw/xmltv/epg_PL.xml.gz",
    "CN": "https://epg.pw/xmltv/epg_CN.xml.gz",
    "IR": "https://epg.pw/xmltv/epg_IR.xml.gz",
}


def fetch_json(url):
    req = Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def build_entry(s, ch_map):
    """Render one stream as an m3u EXTINF+url block, iptv-checker style headers included."""
    title = s.get("title") or "Unknown"
    quality = s.get("quality", "")
    url = s.get("url", "")
    ch_id = s.get("channel")

    group, country = "", ""
    if ch_id and ch_id in ch_map:
        c = ch_map[ch_id]
        cats = c.get("categories", [])
        group = cats[0].title() if cats else ""
        country = c.get("country", "")

    attrs = f'tvg-id="{ch_id or ""}"'
    if group:
        attrs += f' group-title="{group}"'
    if country:
        attrs += f' tvg-country="{country}"'

    display = f"{title} [{country}] [{quality}]" if country else f"{title} [{quality}]"

    lines = [f"#EXTINF:-1 {attrs},{display}"]
    if s.get("referrer"):
        lines.append(f'#EXTVLCOPT:http-referrer={s["referrer"]}')
    if s.get("user_agent"):
        lines.append(f'#EXTVLCOPT:http-user-agent={s["user_agent"]}')
    lines.append(url)
    return "\n".join(lines)


def run_iptv_checker(m3u_text):
    """Runs the freearhey/iptv-checker CLI (ffprobe-based) and returns the surviving m3u body."""
    workdir = tempfile.mkdtemp(prefix="iptv-checker-")
    infile = os.path.join(workdir, "in.m3u")
    outdir = os.path.join(workdir, "out")
    with open(infile, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n" + m3u_text)

    npx = shutil.which("npx") or "npx"
    subprocess.run(
        [
            npx, "--yes", "iptv-checker", infile,
            "-o", outdir,
            "-p", str(CHECKER_PARALLEL),
            "-t", str(CHECKER_TIMEOUT_MS),
        ],
        check=True,
        shell=(os.name == "nt"),
    )

    online_path = os.path.join(outdir, "online.m3u")
    with open(online_path, "r", encoding="utf-8") as f:
        online = f.read()

    shutil.rmtree(workdir, ignore_errors=True)

    # strip the bare "#EXTM3U" header the checker writes
    lines = online.split("\n")
    if lines and lines[0].strip() == "#EXTM3U":
        lines = lines[1:]
    return "\n".join(lines).strip()


def main():
    print("Fetching streams...")
    streams = fetch_json(STREAMS_API)
    print(f"  Total streams: {len(streams)}")

    print("Fetching channels...")
    channels = fetch_json(CHANNELS_API)
    ch_map = {c["id"]: c for c in channels}
    print(f"  Total channels: {len(channels)}")

    # Filter FHD+
    fhd = [s for s in streams if s.get("quality") in FHD_QUALITIES]
    # Remove NSFW
    fhd = [s for s in fhd if not (s.get("channel") and s["channel"] in ch_map and ch_map[s["channel"]].get("is_nsfw"))]
    print(f"  FHD+ (non-NSFW): {len(fhd)}")

    raw_m3u = "\n".join(build_entry(s, ch_map) for s in fhd)

    print(f"Checking {len(fhd)} streams with iptv-checker (ffprobe)...")
    survivors_m3u = run_iptv_checker(raw_m3u)
    alive_count = survivors_m3u.count("#EXTINF")
    print(f"  {alive_count} alive, {len(fhd) - alive_count} dead removed")
    if alive_count == 0:
        raise SystemExit("checker returned 0 alive streams — refusing to overwrite the playlist")

    # Recompute EPG sources / stats from surviving tvg-id's
    from collections import Counter
    countries = Counter()
    categories = Counter()
    q_counts = Counter()
    epg_urls = set()

    for line in survivors_m3u.split("\n"):
        if not line.startswith("#EXTINF"):
            continue
        m = re.search(r'tvg-id="([^"]*)"', line)
        ch_id = m.group(1) if m else ""
        qm = re.search(r"\[(\d+p)\]", line)
        if qm:
            q_counts[qm.group(1)] += 1
        if ch_id and ch_id in ch_map:
            c = ch_map[ch_id]
            country = c.get("country", "")
            if country:
                countries[country] += 1
                if country in EPG_SOURCES:
                    epg_urls.add(EPG_SOURCES[country])
            for cat in c.get("categories", []):
                categories[cat] += 1

    epg_str = ",".join(sorted(epg_urls))
    m3u = f'#EXTM3U url-tvg="{epg_str}" refresh="14400"\n' + survivors_m3u + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(m3u)

    print(f"\nDone! {alive_count} working channels.")
    print(f"EPG sources: {len(epg_urls)}")

    # Update README
    top_countries = countries.most_common(15)
    top_cats = categories.most_common(10)
    readme = f"""# IPTV FHD+ Playlist

**{alive_count:,} Free Full HD & 4K IPTV Channels** (auto-updated daily)

Filtered from [iptv-org/iptv](https://github.com/iptv-org/iptv) — only 1080p+ working streams.

## Usage

Copy this URL into any IPTV player (VLC, TiviMate, IPTV Smarters, Kodi):

```
https://raw.githubusercontent.com/Samhouston010/iptv-fhd/main/fhd_channels.m3u
```

EPG (program guide) is included automatically for supported countries.

## Stats

| Quality | Channels |
|---------|----------|
| 4K (2160p) | {q_counts.get("2160p", 0):,} |
| FHD (1080p) | {q_counts.get("1080p", 0):,} |

| Category | Count |
|----------|-------|
"""
    for cat, n in top_cats:
        readme += f"| {cat.title()} | {n:,} |\n"

    readme += """
| Top Countries | Channels |
|---------------|----------|
"""
    for co, n in top_countries:
        readme += f"| {co} | {n:,} |\n"

    readme += """
## Auto-Update

This playlist is updated daily via GitHub Actions:
- Fresh streams fetched from iptv-org API
- Dead channels removed via [iptv-checker](https://github.com/freearhey/iptv-checker) (real ffprobe playback test, not just HTTP status)
- EPG guide data included for 17 countries

## Source

[iptv-org/iptv](https://github.com/iptv-org/iptv) | EPG from [epg.pw](https://epg.pw)
"""
    with open(README, "w", encoding="utf-8") as f:
        f.write(readme)


if __name__ == "__main__":
    main()
