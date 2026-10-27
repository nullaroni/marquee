"""Cinemark showtimes for the cinema board.

The public site renders showtimes as HTML. Each movie block carries a JSON
model with the trailer, poster, and times. No API key.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = "marquee-kiosk"
SHOWTIMES = "https://www.cinemark.com/umbraco/surface/Showtimes/GetByTheaterId"
THEATERS_TEXT = "https://www.cinemark.com/umbraco/surface/theaters/GetTheatresByText"
THEATERS_GEO = "https://www.cinemark.com/umbraco/surface/theaters/GetTheatresByGeo"
LAKELAND = {
    "id": 1078,
    "name": "Cinemark Lakeland Square Mall and XD",
    "city": "Lakeland",
    "state": "FL",
}
WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary/"
DIRECTOR_CACHE = os.path.expanduser("~/.local/share/marquee/directors.json")
_directors: dict | None = None


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _youtube_id(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"(?:embed/|watch\?v=|youtu\.be/)([A-Za-z0-9_-]{6,})", url)
    return match.group(1) if match else ""


def _when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=ET)
    except ValueError:
        return None


def _short_format(name: str) -> str:
    name = (name or "Standard").replace("Standard Format", "Standard")
    name = name.replace("Luxury Lounger", "Lounger")
    return " ".join(name.split())


def _load_directors() -> dict:
    global _directors
    if _directors is None:
        try:
            with open(DIRECTOR_CACHE, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("cache is not an object")
            _directors = data
        except FileNotFoundError:
            _directors = {}
        except Exception as exc:
            print(f"director cache unreadable: {exc}", flush=True)
            _directors = {}
    return _directors


def _save_directors() -> None:
    if _directors is None:
        return
    tmp = f"{DIRECTOR_CACHE}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(DIRECTOR_CACHE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_directors, handle)
        os.replace(tmp, DIRECTOR_CACHE)
    except OSError as exc:
        print(f"director cache not saved: {exc}", flush=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass


_LETTER = r"[^\W\d_]"
_NAME_TOKEN = re.compile(rf"({_LETTER}\.|{_LETTER}(?:{_LETTER}|['\u2019-])*)")
_DIRECTED_BY = re.compile(r"[Dd]irected by[ \t]+")


def director_from_extract(text: str) -> str:
    """Take the first stated director out of a synopsis paragraph."""
    match = _DIRECTED_BY.search(text or "")
    if not match:
        return ""
    pos = match.end()
    words = []
    while True:
        token = _NAME_TOKEN.match(text, pos)
        if not token or not token.group(1)[0].isupper():
            break
        words.append(token.group(1))
        pos = token.end()
        gap = re.compile(r"[ \t]+").match(text, pos)
        if not gap:
            break
        pos = gap.end()
    return " ".join(words)


def _fetch_director(title: str) -> str | None:
    """Director for a title, or None when no lookup succeeded."""
    result = ""
    for page in (urllib.parse.quote(f"{title} (film)"), urllib.parse.quote(title)):
        try:
            payload = json.loads(fetch(f"{WIKI}{page}", timeout=6))
            result = director_from_extract(payload.get("extract") or "")
        except Exception:
            continue
        if result:
            return result
    return None


def _director(title: str) -> str:
    """Director from Wikipedia's free summary API, cached on disk when found."""
    cached = _load_directors()
    if cached.get(title):
        return cached[title]
    result = _fetch_director(title)
    if not result:
        return ""
    cached[title] = result
    _save_directors()
    return result


def _clock(text: str) -> str:
    compact = re.sub(r"\s+", "", text).lower()
    match = re.match(r"(\d{1,2}):(\d{2})(am|pm)", compact)
    if not match:
        return text.strip()
    hour, minute, meridiem = match.groups()
    return f"{int(hour)}:{minute} {meridiem.upper()}"


def _shows_from_block(block: str, now: datetime) -> list[dict]:
    shows = []
    seen = set()
    for tag in re.findall(r"<a\b[^>]*showtime-link[^>]*>.*?</a>", block, re.S):
        format_match = re.search(r'data-print-type-name="([^"]*)"', tag)
        format_name = html.unescape(format_match.group(1)).strip() if format_match else "Standard"
        stamp_match = re.search(r"Showtime=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", html.unescape(tag))
        stamp = stamp_match.group(1) if stamp_match else ""
        label = _clock(re.sub(r"<[^>]+>", "", tag))
        key = (format_name, label)
        if not label or key in seen:
            continue
        seen.add(key)
        when = _when(stamp)
        if when and when + timedelta(minutes=20) < now:
            continue
        shows.append({"label": label, "format": _short_format(format_name), "at": stamp})
    return shows


def _formats(shows: list[dict]) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    for show in shows:
        grouped.setdefault(show["format"], []).append(show["label"])
    return [{"name": name, "times": times} for name, times in grouped.items()]


def parse_showtimes(page: str, now: datetime | None = None) -> dict:
    """Pull movies out of a Cinemark showtimes HTML response."""
    now = now or datetime.now(ET)
    movies = []
    theater_name = ""
    blocks = re.split(r'(?=<div class="showtimeMovieBlock\b)', page)
    for block in blocks:
        raw_match = re.search(r'data-json-model="(\{.*?\})"', block, re.S)
        if not raw_match:
            continue
        try:
            data = json.loads(html.unescape(raw_match.group(1)))
            if not isinstance(data, dict):
                continue
            theater_name = theater_name or data.get("theaterName") or ""
            shows = _shows_from_block(block, now)
        except Exception as exc:
            print(f"movie block skipped: {exc}", flush=True)
            continue
        if not shows:
            continue
        movies.append(
            {
                "id": data.get("cinemarkMovieId"),
                "title": data.get("movieTitle") or "",
                "rating": data.get("movieRating") or "",
                "runtime": data.get("movieRunTime") or "",
                "poster": data.get("posterLargeImageUrl") or data.get("posterMediumImageUrl") or "",
                "trailer": _youtube_id(data.get("trailerUrl") or ""),
                "times": shows,
                "upcoming": shows,
                "formats": _formats(shows),
            }
        )
    return {"theater": theater_name, "movies": [movie for movie in movies if movie["title"]]}


def showtimes(theater_id: int, day, now: datetime | None = None) -> dict:
    url = f"{SHOWTIMES}?theaterId={int(theater_id)}&showDate={day.isoformat()}"
    page = fetch(url).decode("utf-8", "replace")
    parsed = parse_showtimes(page, now)
    parsed["date"] = day.isoformat()
    parsed["id"] = int(theater_id)
    return parsed


def _day_label(day, today) -> str:
    if day == today:
        return "Tonight"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return day.strftime("%A")


def board(theater_id: int, now: datetime | None = None) -> dict:
    """Showtimes for tonight and the next three days, grouped by movie."""
    now = now or datetime.now(ET)
    today = now.astimezone(ET).date()
    by_id: dict = {}
    order: list = []
    theater_name = ""
    for offset in range(4):
        day = today + timedelta(days=offset)
        moment = now if offset == 0 else datetime.combine(day, datetime.min.time(), ET)
        try:
            parsed = showtimes(theater_id, day, moment)
        except Exception as exc:
            print(f"showtimes failed: {exc}", flush=True)
            continue
        theater_name = theater_name or parsed.get("theater") or ""
        for movie in parsed["movies"]:
            if not movie.get("formats") or not movie.get("title"):
                continue
            key = movie.get("id") or movie.get("title")
            entry = by_id.get(key)
            if entry is None:
                entry = dict(movie)
                entry["days"] = []
                by_id[key] = entry
                order.append(key)
            if not entry.get("poster") and movie.get("poster"):
                entry["poster"] = movie["poster"]
            if not entry.get("trailer") and movie.get("trailer"):
                entry["trailer"] = movie["trailer"]
            entry["days"].append({
                "label": _day_label(day, today),
                "date": day.isoformat(),
                "formats": movie["formats"],
            })
    movies: list = []
    fresh = 0
    for key in order:
        entry = by_id[key]
        cached = _load_directors()
        title = entry["title"]
        if cached.get(title):
            entry["director"] = cached[title]
        elif fresh < 3:
            # A few lookups per refresh so the board never stalls.
            fresh += 1
            try:
                entry["director"] = _director(title)
            except Exception as exc:
                print(f"director lookup failed: {exc}", flush=True)
                entry["director"] = ""
        else:
            entry["director"] = ""
        movies.append(entry)
    return {
        "id": int(theater_id),
        "name": theater_name or LAKELAND["name"],
        "label": "Next few days",
        "date": today.isoformat(),
        "movies": movies,
    }


def _theater_id(url: str) -> int | None:
    try:
        payload = json.loads(fetch(url).decode("utf-8", "replace"))
    except Exception as exc:
        print(f"theater search failed: {exc}", flush=True)
        return None
    result = payload.get("Result")
    try:
        return int(result) if result else None
    except (TypeError, ValueError):
        return None


def nearest_text(query: str) -> dict | None:
    query = query.strip()
    if not query:
        return None
    theater_id = _theater_id(f"{THEATERS_TEXT}?contentId=0&searchText={urllib.parse.quote(query)}")
    if not theater_id:
        return None
    return _describe(theater_id)


def nearest_geo(lat: float, lon: float) -> dict | None:
    theater_id = _theater_id(f"{THEATERS_GEO}?contentId=0&geoPosition={lat},{lon}")
    if not theater_id:
        return None
    return _describe(theater_id)


def _describe(theater_id: int) -> dict:
    try:
        parsed = showtimes(theater_id, datetime.now(ET).date())
        name = parsed["theater"]
    except Exception:
        name = ""
    return {"id": theater_id, "name": name}

