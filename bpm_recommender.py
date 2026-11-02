#!/usr/bin/env python3
"""
BPM-based song recommender.

Usage:
    python bpm_recommender.py "Blinding Lights"
    python bpm_recommender.py "Blinding Lights - The Weeknd" --tolerance 4 --max 15

Setup:
    pip install requests
    export LASTFM_API_KEY=...      # https://www.last.fm/api/account/create (free)
    export GETSONGBPM_API_KEY=...  # https://getsongbpm.com/api (free)

How it works:
    1. Last.fm resolves your input to a canonical artist + title.
    2. GetSongBPM looks up that song's tempo.
    3. Last.fm's track.getSimilar supplies candidates that match the vibe/genre
       (based on listening behavior and tags).
    4. Each candidate's BPM is looked up, and only those within +/- tolerance
       of the original are kept, sorted by closeness.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
GETSONGBPM_URL = "https://api.getsong.co"  # check https://getsongbpm.com/api if this changes
CACHE_FILE = Path.home() / ".bpm_recommender_cache.json"

LASTFM_KEY = os.environ.get("LASTFM_API_KEY")
BPM_KEY = os.environ.get("GETSONGBPM_API_KEY")

session = requests.Session()
session.headers["User-Agent"] = "bpm-recommender/1.0"


# ---------- cache (avoids re-spending API quota on repeat lookups) ----------

def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


# ---------- Last.fm ----------

def lastfm(method: str, **params) -> dict:
    params.update(method=method, api_key=LASTFM_KEY, format="json")
    r = session.get(LASTFM_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def resolve_song(query: str) -> tuple[str, str] | None:
    """Turn free text like 'Blinding Lights' or 'Blinding Lights - The Weeknd'
    into a canonical (artist, title)."""
    title, artist = query, None
    if " - " in query:
        title, artist = [p.strip() for p in query.split(" - ", 1)]

    params = {"track": title, "limit": 1}
    if artist:
        params["artist"] = artist
    data = lastfm("track.search", **params)
    matches = data.get("results", {}).get("trackmatches", {}).get("track", [])
    if not matches:
        return None
    return matches[0]["artist"], matches[0]["name"]


def similar_tracks(artist: str, title: str, limit: int = 100) -> list[tuple[str, str]]:
    data = lastfm("track.getsimilar", artist=artist, track=title,
                  limit=limit, autocorrect=1)
    tracks = data.get("similartracks", {}).get("track", [])
    return [(t["artist"]["name"], t["name"]) for t in tracks]


# ---------- GetSongBPM ----------

def lookup_bpm(artist: str, title: str, cache: dict) -> int | None:
    key = f"{artist.lower()}||{title.lower()}"
    if key in cache:
        return cache[key]

    bpm = None
    try:
        r = session.get(
            f"{GETSONGBPM_URL}/search/",
            params={
                "api_key": BPM_KEY,
                "type": "both",
                "lookup": f"song:{title} artist:{artist}",
            },
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("search")
        if isinstance(results, list) and results:
            top = results[0]
            tempo = top.get("tempo")
            if not tempo and top.get("id"):  # some responses need a second call
                s = session.get(f"{GETSONGBPM_URL}/song/",
                                params={"api_key": BPM_KEY, "id": top["id"]},
                                timeout=15)
                s.raise_for_status()
                tempo = s.json().get("song", {}).get("tempo")
            if tempo:
                bpm = int(round(float(tempo)))
    except (requests.RequestException, ValueError, KeyError):
        return None  # don't cache failures; they may be transient

    cache[key] = bpm  # cache "not found" too (None) so we don't retry it
    time.sleep(0.2)   # be polite to the API
    return bpm


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Find songs with a similar BPM and vibe.")
    ap.add_argument("song", nargs="?", help='Song name, or "Song - Artist"')
    ap.add_argument("--tolerance", type=int, default=5, help="max BPM difference (default 5)")
    ap.add_argument("--max", type=int, default=20, help="max recommendations (default 20)")
    args = ap.parse_args()

    if not LASTFM_KEY or not BPM_KEY:
        print("Set LASTFM_API_KEY and GETSONGBPM_API_KEY environment variables first.")
        return 1

    query = args.song or input("Enter a song name: ").strip()
    if not query:
        return 1

    cache = load_cache()

    resolved = resolve_song(query)
    if not resolved:
        print(f"Couldn't find '{query}'. Try 'Song - Artist'.")
        return 1
    artist, title = resolved

    base_bpm = lookup_bpm(artist, title, cache)
    if base_bpm is None:
        print(f"Found '{title}' by {artist}, but couldn't find its BPM.")
        save_cache(cache)
        return 1
    print(f"\n{title} - {artist}: {base_bpm} BPM")
    print(f"Searching for similar songs within +/-{args.tolerance} BPM...\n")

    candidates = similar_tracks(artist, title)
    matches = []
    for i, (c_artist, c_title) in enumerate(candidates, 1):
        print(f"\rChecking candidate {i}/{len(candidates)}", end="", file=sys.stderr)
        bpm = lookup_bpm(c_artist, c_title, cache)
        if bpm is not None and abs(bpm - base_bpm) <= args.tolerance:
            matches.append((abs(bpm - base_bpm), c_title, c_artist, bpm))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    save_cache(cache)

    if not matches:
        print("No similar songs within that BPM range. Try a larger --tolerance.")
        return 0

    matches.sort()
    print(f"Recommendations ({len(matches[:args.max])}):")
    for n, (diff, c_title, c_artist, bpm) in enumerate(matches[:args.max], 1):
        print(f"{n:>2}. {c_title} - {c_artist}  ({bpm} BPM, {diff:+d} off)".replace("+0 off", "exact"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
