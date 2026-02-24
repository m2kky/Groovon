"""
Title classifier - separates artist names from event titles.
Replaces the classify_title / verify_match functions in enrichers.py
"""
import unicodedata
import json
import urllib.request

OPENROUTER_API_KEY = "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"
MODEL = "google/gemini-2.5-flash"

# Patterns that almost always mean it's NOT an artist
_EVENT_KEYWORDS = {
    "jam session", "open mic", "comedy night", "brunch series", "trivia night",
    "karaoke", "open bar", "happy hour", "showcase", "festival", "fest ",
    "spotlight", "underground comedy", "drag show", "dance party", "dj night"
}

# Suffixes to strip before treating remainder as artist name
_STRIP_SUFFIXES = [
    " (ep release)", " (album release)", " (single release)", " (live)", " (tour)",
    " (residency)", " (debut)", " (farewell)", " (reunion)"
]


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().strip()


def _quick_classify(title: str) -> dict | None:
    """Rule-based fast path. Returns result dict or None to fall through to AI."""
    t = title.lower().strip()

    # Year at end → likely festival/event
    import re
    if re.search(r'\b20\d{2}\b', t):
        return {"is_artist": False, "artist_name": None, "confidence": 90}

    # Contains "w/" or " with " → extract part after it
    for sep in [" w/ ", " with ", " feat. ", " feat ", " ft. "]:
        if sep in t:
            parts = title.split(sep, 1)
            if len(parts) < 2:
                continue
            artist = parts[1].strip()
            # Strip any trailing event suffix
            for suf in _STRIP_SUFFIXES:
                artist = artist.replace(suf, "").strip()
            return {"is_artist": True, "artist_name": artist, "confidence": 85}

    # Generic event keywords
    for kw in _EVENT_KEYWORDS:
        if kw in t:
            return {"is_artist": False, "artist_name": None, "confidence": 85}

    # Strip known suffixes → treat remainder as artist
    clean = title
    for suf in _STRIP_SUFFIXES:
        if t.endswith(suf):
            clean = title[:len(title) - len(suf)].strip()
            return {"is_artist": True, "artist_name": clean, "confidence": 80}

    return None  # needs AI


def _ai_classify(title: str, venue: str = None, city: str = None) -> dict:
    context = f'Title: "{title}"'
    if venue:
        context += f'\nVenue: "{venue}"'
    if city:
        context += f'\nCity: "{city}"'
    prompt = f"""You are classifying concert/event listing titles.

{context}"

Examples:
- "MONOLINK" → artist
- "SNOWCUFFS (EP RELEASE)" → artist = "SNOWCUFFS"
- "FANK! W/ DINOSAUR GALAXY" → artist = "DINOSAUR GALAXY"
- "EXTRAORDINARY POPULAR DELUSIONS" → artist (band name that sounds like a phrase)
- "MACHÏN" → artist (foreign/unusual spelling is fine)
- "JAZZ LINKS JAM SESSION" → NOT artist (generic recurring event)
- "CHICAGO UNDERGROUND COMEDY" → NOT artist
- "SUNDAY SPOTLIGHT BRUNCH" → NOT artist
- "SNÜZFEST 2026" → NOT artist (festival with year)
- "BLUEGRASS BRUNCH" → NOT artist (genre + meal = recurring event)

Rules:
1. If it could be a band/artist name (even unusual ones) → is_artist: true
2. Only mark false if it's CLEARLY a generic recurring event or festival
3. Strip suffixes like (EP RELEASE), (LIVE), (TOUR) and return clean artist name
4. "W/" or "WITH" → extract the act AFTER it

Return JSON only: {{"is_artist": true/false, "artist_name": "name or null", "confidence": 0-100}}"""

    data = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])


def classify_title(title: str, venue: str = None, city: str = None) -> dict:
    """Returns {is_artist, artist_name, confidence}"""
    result = _quick_classify(title)
    if result is not None:
        return result
    try:
        return _ai_classify(title, venue, city)
    except Exception:
        return {"is_artist": True, "artist_name": title, "confidence": 40}


def verify_match(title: str, found_name: str) -> bool:
    """Check if found_name is a reasonable match for the event title."""
    t_norm = _normalize(title)
    n_norm = _normalize(found_name)

    # Exact substring match
    if n_norm in t_norm:
        return True

    # Token overlap ≥ 50%
    t_tokens = set(t_norm.split())
    n_tokens = set(n_norm.split())
    if n_tokens and len(t_tokens & n_tokens) / len(n_tokens) >= 0.5:
        return True

    return False
