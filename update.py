"""
Auto-update FHD+ IPTV playlist.
1. Fetch fresh streams from iptv-org API
2. Filter FHD+ (1080p, 2160p, 4320p)
3. Test URLs concurrently, remove dead ones
4. Add EPG guide URLs
5. Write final M3U with metadata
"""
import json, os, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen

STREAMS_API = "https://iptv-org.github.io/api/streams.json"
CHANNELS_API = "https://iptv-org.github.io/api/channels.json"
OUT = os.path.join(os.path.dirname(__file__), "fhd_channels.m3u")
README = os.path.join(os.path.dirname(__file__), "README.md")
WORKERS = 100
TIMEOUT = 10
FHD_QUALITIES = {"1080p", "2160p", "4320p"}

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


def check_url(url):
    try:
        req = Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        resp = urlopen(req, timeout=TIMEOUT)
        return resp.status < 400
    except Exception:
        try:
            req = Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            req.add_header("Range", "bytes=0-1024")
            resp = urlopen(req, timeout=TIMEOUT)
            return resp.status < 400
        except Exception:
            return False


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

    # Test URLs
    print(f"Testing {len(fhd)} URLs with {WORKERS} workers...")
    alive = []
    dead = 0
    done = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check_url, s["url"]): s for s in fhd}
        for fut in as_completed(futures):
            s = futures[fut]
            done += 1
            if fut.result():
                alive.append(s)
            else:
                dead += 1
            if done % 500 == 0 or done == len(fhd):
                print(f"  {done}/{len(fhd)} — {len(alive)} alive, {dead} dead")

    # Collect EPG URLs needed
    epg_urls = set()
    for s in alive:
        ch_id = s.get("channel")
        if ch_id and ch_id in ch_map:
            co = ch_map[ch_id].get("country", "")
            if co in EPG_SOURCES:
                epg_urls.add(EPG_SOURCES[co])

    # Build M3U
    epg_str = ",".join(sorted(epg_urls))
    lines = [f'#EXTM3U url-tvg="{epg_str}" refresh="14400"']

    from collections import Counter
    countries = Counter()
    categories = Counter()
    q_counts = Counter()

    for s in alive:
        title = s.get("title") or "Unknown"
        quality = s.get("quality", "")
        url = s.get("url", "")
        ch_id = s.get("channel")

        group = ""
        country = ""
        if ch_id and ch_id in ch_map:
            c = ch_map[ch_id]
            cats = c.get("categories", [])
            group = cats[0].title() if cats else ""
            country = c.get("country", "")
            countries[country] += 1
            for cat in cats:
                categories[cat] += 1
        q_counts[quality] += 1

        attrs = f'tvg-id="{ch_id or ""}"'
        if group:
            attrs += f' group-title="{group}"'
        if country:
            attrs += f' tvg-country="{country}"'

        display = f"{title} [{quality}]"
        if country:
            display = f"{title} [{country}] [{quality}]"

        lines.append(f"#EXTINF:-1 {attrs},{display}")
        if s.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={s["referrer"]}')
        if s.get("user_agent"):
            lines.append(f'#EXTVLCOPT:http-user-agent={s["user_agent"]}')
        lines.append(url)

    m3u = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(m3u)

    print(f"\nDone! {len(alive)} working channels, {dead} dead removed.")
    print(f"EPG sources: {len(epg_urls)}")

    # Update README
    top_countries = countries.most_common(15)
    top_cats = categories.most_common(10)
    readme = f"""# IPTV FHD+ Playlist

**{len(alive):,} Free Full HD & 4K IPTV Channels** (auto-updated daily)

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

    readme += f"""
| Top Countries | Channels |
|---------------|----------|
"""
    for co, n in top_countries:
        readme += f"| {co} | {n:,} |\n"

    readme += """
## Auto-Update

This playlist is updated daily via GitHub Actions:
- Fresh streams fetched from iptv-org API
- Dead channels automatically removed
- EPG guide data included for 17 countries

## Source

[iptv-org/iptv](https://github.com/iptv-org/iptv) | EPG from [epg.pw](https://epg.pw)
"""
    with open(README, "w", encoding="utf-8") as f:
        f.write(readme)


if __name__ == "__main__":
    main()
