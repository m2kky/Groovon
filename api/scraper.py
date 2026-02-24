import re, logging, urllib.request, urllib.parse, ssl, gzip, json, base64

log = logging.getLogger(__name__)

import os
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
SEARCH_MODEL = "perplexity/sonar"

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_SKIP_EMAIL = {"sentry.io", "wixpress.com", "example.com", "noreply", "no-reply", "domain.com", "cloudflare"}
BOOKING_KW  = ["booking", "contact", "management", "info", "press", "agent"]

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

SOCIAL_DOMAINS = {"facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
                  "soundcloud.com", "bandcamp.com", "spotify.com", "tiktok.com",
                  "myspace.com", "discogs.com", "last.fm", "lastfm.com"}

SKIP_DOMAINS = {"google.com", "bing.com", "duckduckgo.com", "wikipedia.org",
                "wikidata.org", "musicbrainz.org", "genius.com", "azlyrics.com",
                "allmusic.com", "amazon.com", "apple.com", "shazam.com"}

PLATFORM_RE = {
    "instagram":  re.compile(r"instagram\.com/([A-Za-z0-9_.]+)"),
    "facebook":   re.compile(r"facebook\.com/([A-Za-z0-9_.]+)"),
    "twitter":    re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]+)"),
    "tiktok":     re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)"),
    "soundcloud": re.compile(r"soundcloud\.com/([A-Za-z0-9_-]+)"),
    "bandcamp":   re.compile(r"([A-Za-z0-9_-]+)\.bandcamp\.com"),
    "youtube":    re.compile(r"youtube\.com/(?:c/|channel/|@)([A-Za-z0-9_-]+)"),
    "spotify":    re.compile(r"open\.spotify\.com/artist/([A-Za-z0-9]+)"),
    "linktree":   re.compile(r"linktr\.ee/([A-Za-z0-9_.-]+)"),
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _valid_email(e):
    return not any(s in e.lower() for s in _SKIP_EMAIL) and len(e) < 80


def _score_email(e):
    for i, kw in enumerate(BOOKING_KW):
        if kw in e.lower():
            return len(BOOKING_KW) - i
    return 0


def _domain(url):
    try:
        return url.split("/")[2].replace("www.", "")
    except Exception:
        return ""


def _fetch(url: str, timeout: int = 12) -> str:
    """Fetch URL with plain urllib, handle gzip."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
        raw = r.read()
        if r.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        html = raw.decode("utf-8", errors="ignore")
    # Strip scripts/styles
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return html


def _extract_social_links(text: str) -> dict:
    found = {}
    for platform, rx in PLATFORM_RE.items():
        m = rx.search(text)
        if m:
            full = m.group(0)
            found[platform] = ("https://" + full) if not full.startswith("http") else full
    return found


def _ai_web_search(artist_name: str) -> dict:
    """
    Use Perplexity Sonar (web search model) to find artist info:
    email, official website, social links, press URLs.
    """
    prompt = f"""Find information about the music artist "{artist_name}".
Return a JSON object with these fields (null if not found):
{{
  "official_website": "URL of their official website",
  "email": "booking or contact email address",
  "linktree": "linktr.ee URL if exists",
  "instagram": "instagram.com URL",
  "facebook": "facebook.com URL",
  "twitter": "twitter.com or x.com URL",
  "tiktok": "tiktok.com URL",
  "soundcloud": "soundcloud.com URL",
  "bandcamp": "bandcamp URL",
  "press_urls": ["list of up to 5 press/review article URLs about this artist"]
}}"""
    try:
        data = json.dumps({
            "model": SEARCH_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as r:
            content = json.loads(r.read())["choices"][0]["message"]["content"]
            result = json.loads(content)
            log.info(f"AI web search for '{artist_name}': {result}")
            return result
    except Exception as e:
        log.warning(f"AI web search failed for '{artist_name}': {e}")
        return {}


def _ddg_search(query: str, max_results: int = 8) -> list[str]:
    """DuckDuckGo HTML search → list of result URLs."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={**_HEADERS, "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=12) as r:
            html = r.read().decode("utf-8", errors="ignore")
        urls = re.findall(r'uddg=(https?[^&"<>\s]+)', html)
        urls = [urllib.parse.unquote(u) for u in urls]
        urls = [u for u in urls if not any(s in _domain(u) for s in SKIP_DOMAINS)]
        log.info(f"DDG raw results for '{query}': {urls[:max_results]}")
        return urls[:max_results]
    except Exception as e:
        log.warning(f"DDG search failed: {e}")
        return []


def scrape_website(urls: list[str], artist_name: str = None) -> dict:
    result = {"email": None, "official_website": None, "social_links": {}, "press_urls": []}
    all_emails, all_text = [], []

    # 1. AI web search — gets structured data directly
    if artist_name:
        ai = _ai_web_search(artist_name)
        if ai.get("email") and _valid_email(ai["email"]):
            all_emails.append(ai["email"])
        if ai.get("official_website"):
            result["official_website"] = ai["official_website"]
        if ai.get("press_urls"):
            result["press_urls"] = [u for u in ai["press_urls"] if isinstance(u, str)][:5]
        # Collect social links from AI result
        for platform in ("instagram", "facebook", "twitter", "tiktok", "soundcloud", "bandcamp", "linktree"):
            if ai.get(platform):
                result["social_links"][platform] = ai[platform]

        # 2. Scrape press URLs for emails + more social links
        for url in result["press_urls"]:
            try:
                text = _fetch(url)
                all_text.append(text)
                emails = [e for e in dict.fromkeys(_EMAIL_RE.findall(text)) if _valid_email(e)]
                all_emails.extend(emails)
                result["social_links"].update(_extract_social_links(text))
            except Exception as e:
                log.warning(f"Scrape failed {url}: {e}")

        # 3. Scrape Linktree if found
        lt = ai.get("linktree") or result["social_links"].get("linktree")
        if lt:
            try:
                text = _fetch(lt)
                all_text.append(text)
                result["social_links"].update(_extract_social_links(text))
                emails = [e for e in dict.fromkeys(_EMAIL_RE.findall(text)) if _valid_email(e)]
                all_emails.extend(emails)
                log.info(f"Scraped Linktree {lt}")
            except Exception as e:
                log.warning(f"Linktree scrape failed {lt}: {e}")

    # 4. Known URLs (Discogs/Wikidata)
    for url in (urls or []):
        if not url or not url.startswith("http"):
            continue
        dom = _domain(url)
        is_social = any(s in dom for s in SOCIAL_DOMAINS)
        if not is_social and not result["official_website"]:
            result["official_website"] = url
        try:
            text = _fetch(url)
            all_text.append(text)
            emails = [e for e in dict.fromkeys(_EMAIL_RE.findall(text)) if _valid_email(e)]
            all_emails.extend(emails)
            result["social_links"].update(_extract_social_links(text))
        except Exception as e:
            log.warning(f"Scrape failed {url}: {e}")

    combined = " ".join(all_text)
    result["social_links"].update(_extract_social_links(combined))
    if all_emails:
        result["email"] = max(dict.fromkeys(all_emails), key=_score_email)
    return result
