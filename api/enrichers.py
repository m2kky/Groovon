import urllib.request, json, urllib.parse, base64, time, unicodedata, ssl, logging, re
from threading import Lock
from classifier import classify_title, verify_match
from scraper import scrape_website

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

import os
SPOTIFY_CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
LASTFM_API_KEY        = os.environ["LASTFM_API_KEY"]
YOUTUBE_API_KEY       = os.environ["YOUTUBE_API_KEY"]
OPENROUTER_API_KEY    = os.environ["OPENROUTER_API_KEY"]
DISCOGS_TOKEN         = os.environ["DISCOGS_TOKEN"]
MODEL = "google/gemini-2.5-flash"

_spotify_token = None
_token_expiry = 0
_token_lock = Lock()


def get_spotify_token():
    global _spotify_token, _token_expiry
    with _token_lock:
        if _spotify_token and time.time() < _token_expiry:
            return _spotify_token
        creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            _spotify_token = data["access_token"]
            _token_expiry = time.time() + data["expires_in"] - 60
            return _spotify_token


def normalize(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def spotify_search(name):
    token = get_spotify_token()
    for query in [name, normalize(name), normalize(name).replace("i", "i")]:
        query = query.strip()
        if not query:
            continue
        url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=artist&limit=1"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                items = json.loads(r.read()).get("artists", {}).get("items", [])
                if items:
                    a = items[0]
                    return {"id": a["id"], "name": a["name"], "genres": a.get("genres", []),
                            "popularity": a.get("popularity", 0),
                            "photo": a["images"][0]["url"] if a.get("images") else None,
                            "spotify_url": a["external_urls"]["spotify"],
                            "followers": a.get("followers", {}).get("total")}
        except Exception as e:
            log.warning(f"Spotify error for '{query}': {e}")
    return None


def deezer_search(name):
    url = f"https://api.deezer.com/search/artist?q={urllib.parse.quote(name)}&limit=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read()).get("data", [])
            if data:
                a = data[0]
                return {"name": a["name"], "photo": a.get("picture_xl"), "fans": a.get("nb_fan"), "deezer_id": a["id"]}
    except Exception as e:
        log.warning(f"Deezer error for '{name}': {e}")
    return None


def itunes_search(name):
    url = f"https://itunes.apple.com/search?term={urllib.parse.quote(name)}&entity=musicArtist&limit=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            results = json.loads(r.read()).get("results", [])
            if results:
                a = results[0]
                return {"genre": a.get("primaryGenreName"), "itunes_url": a.get("artistLinkUrl")}
    except Exception as e:
        log.warning(f"iTunes error for '{name}': {e}")
    return None


def lastfm_search(name):
    url = f"https://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist={urllib.parse.quote(name)}&api_key={LASTFM_API_KEY}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            a = json.loads(r.read()).get("artist", {})
            if not a or not a.get("name"):
                return None
            bio = a.get("bio", {}).get("summary", "")
            bio = bio.split("<a href")[0].strip() if bio else None
            return {
                "tags": [t["name"] for t in a.get("tags", {}).get("tag", [])],
                "bio": bio,
                "lastfm_url": a.get("url"),
                "listeners": a.get("stats", {}).get("listeners")
            }
    except Exception as e:
        log.warning(f"LastFM error for '{name}': {e}")
    return None


def wikipedia_search(name):
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(name.replace(' ', '_'))}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Groovon/1.0"})
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=10) as r:
            d = json.loads(r.read())
            if d.get("type") == "disambiguation":
                return None
            return {
                "bio": d.get("extract"),
                "wiki_url": d.get("content_urls", {}).get("desktop", {}).get("page"),
                "thumbnail": d.get("thumbnail", {}).get("source")
            }
    except Exception as e:
        log.warning(f"Wikipedia error for '{name}': {e}")
    return None


def wikidata_search(name):
    """Get structured infobox data from Wikidata via Wikipedia sitelinks."""
    # Step 1: get Wikidata entity ID from Wikipedia page
    search_url = f"https://en.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(name.replace(' ','_'))}&prop=pageprops&ppprop=wikibase_item&format=json"
    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": "Groovon/1.0"})
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=10) as r:
            pages = json.loads(r.read()).get("query", {}).get("pages", {})
            qid = next(iter(pages.values()), {}).get("pageprops", {}).get("wikibase_item")
        if not qid:
            return None

        # Step 2: fetch Wikidata entity
        wd_url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
        req2 = urllib.request.Request(wd_url, headers={"User-Agent": "Groovon/1.0"})
        with urllib.request.urlopen(req2, context=SSL_CTX, timeout=10) as r:
            entity = json.loads(r.read()).get("entities", {}).get(qid, {})

        claims = entity.get("claims", {})

        def label(qid_val):
            """Resolve a Wikidata QID to English label."""
            try:
                lr = urllib.request.Request(
                    f"https://www.wikidata.org/wiki/Special:EntityData/{qid_val}.json",
                    headers={"User-Agent": "Groovon/1.0"}
                )
                with urllib.request.urlopen(lr, context=SSL_CTX, timeout=8) as r2:
                    ent = json.loads(r2.read()).get("entities", {}).get(qid_val, {})
                    return ent.get("labels", {}).get("en", {}).get("value")
            except Exception:
                return None

        def get_str(prop):
            vals = claims.get(prop, [])
            return [v["mainsnak"]["datavalue"]["value"] for v in vals
                    if v.get("mainsnak", {}).get("datavalue")] if vals else []

        def get_qids(prop):
            vals = claims.get(prop, [])
            return [v["mainsnak"]["datavalue"]["value"]["id"] for v in vals
                    if v.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")] if vals else []

        def get_time(prop):
            vals = claims.get(prop, [])
            if vals:
                v = vals[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
                t = v.get("time", "").lstrip("+")
                precision = v.get("precision", 9)
                if not t:
                    return None
                parts = t.split("T")[0].split("-")
                try:
                    if precision >= 11:  # day
                        import datetime
                        d = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
                        return d.strftime("%B %-d, %Y")
                    elif precision == 10:  # month
                        import datetime
                        d = datetime.date(int(parts[0]), int(parts[1]), 1)
                        return d.strftime("%B %Y")
                    else:  # year only
                        return parts[0]
                except Exception:
                    return parts[0]
            return None

        # Resolve QID lists to labels (limit to avoid too many requests)
        genre_ids    = get_qids("P136")[:4]
        occ_ids      = get_qids("P106")[:4]
        instr_ids    = get_qids("P1303")[:4]
        label_ids    = get_qids("P264")[:3]

        genres      = [l for l in (label(q) for q in genre_ids) if l]
        occupations = [l for l in (label(q) for q in occ_ids) if l]
        instruments = [l for l in (label(q) for q in instr_ids) if l]
        rec_labels  = [l for l in (label(q) for q in label_ids) if l]

        # Website: P856
        websites = get_str("P856")[:2]

        # Born: P569, birthplace: P19
        born_date  = get_time("P569")
        # If Wikidata only has year precision, try Wikipedia infobox via parse API
        if born_date and len(born_date) == 4:  # year only like "1989"
            try:
                parse_url = f"https://en.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(name.replace(' ','_'))}&prop=revisions&rvprop=content&rvsection=0&format=json"
                preq = urllib.request.Request(parse_url, headers={"User-Agent": "Groovon/1.0"})
                with urllib.request.urlopen(preq, context=SSL_CTX, timeout=10) as r:
                    content = json.dumps(json.loads(r.read()))
                # Look for birth_date pattern in wikitext
                m = re.search(r'birth_date\s*=\s*\{\{[Bb]irth date[^}]*?(\d{4})[^}]*?(\d{1,2})[^}]*?(\d{1,2})', content)
                if m:
                    import datetime
                    d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    born_date = d.strftime("%B %-d, %Y")
            except Exception:
                pass
        birthplace_ids = get_qids("P19")[:1]
        birthplace = label(birthplace_ids[0]) if birthplace_ids else None

        # Years active: P2031 start, P2032 end
        active_start = get_time("P2031")
        active_end   = get_time("P2032")
        years_active = None
        if active_start:
            years_active = active_start[:4] + ("–" + active_end[:4] if active_end else "–present")

        return {
            "born": born_date,
            "birthplace": birthplace,
            "years_active": years_active,
            "genres": genres,
            "occupations": occupations,
            "instruments": instruments,
            "labels": rec_labels,
            "websites": websites,
        }
    except Exception as e:
        log.warning(f"Wikidata error for '{name}': {e}")
    return None


def musicbrainz_search(name):
    url = f"https://musicbrainz.org/ws/2/artist/?query=artist:{urllib.parse.quote(name)}&limit=1&fmt=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Groovon/1.0 (groovon@example.com)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            artists = json.loads(r.read()).get("artists", [])
            if not artists:
                return None
            a = artists[0]
            tags = [t["name"] for t in a.get("tags", [])[:5]]
            return {
                "country": a.get("country"),
                "begin_area": a.get("begin-area", {}).get("name") if a.get("begin-area") else None,
                "type": a.get("type"),  # Person / Group / DJ
                "tags": tags,
                "disambiguation": a.get("disambiguation"),
                "mb_id": a.get("id"),
            }
    except Exception as e:
        log.warning(f"MusicBrainz error for '{name}': {e}")
    return None


def discogs_search(name):
    url = f"https://api.discogs.com/database/search?q={urllib.parse.quote(name)}&type=artist&per_page=1"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Discogs token={DISCOGS_TOKEN}",
            "User-Agent": "Groovon/1.0"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.loads(r.read()).get("results", [])
            if not results:
                return None
            artist_id = results[0]["id"]
        req2 = urllib.request.Request(
            f"https://api.discogs.com/artists/{artist_id}",
            headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}", "User-Agent": "Groovon/1.0"}
        )
        with urllib.request.urlopen(req2, timeout=10) as r:
            a = json.loads(r.read())
            return {
                "real_name": a.get("realname"),
                "country": a.get("profile", "").split("\n")[0][:100] if a.get("profile") else None,
                "discogs_url": a.get("uri"),
                "profile": a.get("profile", "").split("\n")[0][:300] if a.get("profile") else None,
                "urls": a.get("urls", [])[:5],
            }
    except Exception as e:
        log.warning(f"Discogs error for '{name}': {e}")
    return None


def youtube_search(name):
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={urllib.parse.quote(name + ' official')}&type=channel&maxResults=1&key={YOUTUBE_API_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            items = json.loads(r.read()).get("items", [])
            if items:
                ch = items[0]
                channel_id = ch["snippet"]["channelId"]
                return {
                    "channel_id": channel_id,
                    "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                    "channel_title": ch["snippet"]["title"]
                }
    except Exception as e:
        log.warning(f"YouTube error for '{name}': {e}")
    return None


def ai_call(prompt):
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])



def enrich_artist(title: str, venue: str = None, city: str = None) -> dict:
    log.info(f"Enriching: '{title}' | venue={venue} | city={city}")
    classified = classify_title(title, venue, city)
    artist_name = (classified.get("artist_name") or "").strip() or title
    is_artist = classified.get("is_artist", False)
    log.info(f"Classified '{title}' → is_artist={is_artist}, name='{artist_name}'")

    if not is_artist and not classified.get("artist_name"):
        return {"original_title": title, "artist_name": None, "confidence": 0,
                "confidence_reasons": "Not a music artist event", "venue": venue, "city": city}

    # Spotify
    log.info(f"Spotify search: '{artist_name}'")
    spotify = spotify_search(artist_name)
    if spotify and not verify_match(title, spotify["name"]):
        spotify = None
    if spotify:
        artist_name = spotify["name"]

    # Deezer
    log.info(f"Deezer search: '{artist_name}'")
    deezer = deezer_search(artist_name)
    if deezer and not spotify and not verify_match(title, deezer["name"]):
        deezer = None
    if deezer and not spotify:
        artist_name = deezer["name"]

    # Sequential enrichment
    log.info(f"Enrichment: '{artist_name}'")
    itunes  = itunes_search(artist_name)
    lastfm  = lastfm_search(artist_name)
    wiki    = wikipedia_search(artist_name)
    wikidata = wikidata_search(artist_name)
    youtube = youtube_search(artist_name)
    discogs = discogs_search(artist_name)
    mb      = musicbrainz_search(artist_name)
    log.info(f"Enrichment done: '{artist_name}' | lastfm={bool(lastfm)} discogs={bool(discogs)} mb={bool(mb)} wikidata={bool(wikidata)}")

    # Scrapling: email + official_website from Discogs URLs + Wikidata websites
    scraped = {"email": None, "official_website": None, "social_links": {}}
    scrape_urls = list(discogs["urls"] if discogs and discogs.get("urls") else [])
    if wikidata and wikidata.get("websites"):
        scrape_urls = wikidata["websites"] + scrape_urls
    if scrape_urls or artist_name:
        log.info(f"Scraping {len(scrape_urls)} URLs for '{artist_name}'")
        scraped = scrape_website(scrape_urls, artist_name=artist_name)

    # Confidence
    score = 0
    reasons = []
    if spotify:
        score += 40
        reasons.append("Spotify")
        if spotify["popularity"] > 30:
            score += 10
    if lastfm:
        score += 20
        reasons.append("Last.fm")
    if wiki:
        score += 15
        reasons.append("Wikipedia")
    if youtube:
        score += 10
        reasons.append("YouTube")
    if discogs:
        score += 10
        reasons.append("Discogs")
    if mb:
        score += 5
        reasons.append("MusicBrainz")
    if scraped.get("email"):
        score += 5
        reasons.append("Email")
    if artist_name.lower() in title.lower():
        score += 5

    # Bio priority: wiki > lastfm
    bio = (wiki["bio"] if wiki and wiki.get("bio") else None) or (lastfm["bio"] if lastfm else None)
    photo = (spotify["photo"] if spotify else None) or (deezer["photo"] if deezer else None) or \
            (wiki["thumbnail"] if wiki else None)
    genres = (spotify["genres"] if spotify else []) or (lastfm["tags"] if lastfm else []) or \
             ([itunes["genre"]] if itunes and itunes.get("genre") else [])

    # Merge genres: spotify > wikidata > lastfm > itunes > musicbrainz
    if not genres and wikidata and wikidata.get("genres"):
        genres = wikidata["genres"]
    if not genres and mb and mb.get("tags"):
        genres = mb["tags"]

    # Country: musicbrainz > discogs profile
    country = (mb["country"] if mb else None) or (mb["begin_area"] if mb else None)
    artist_type = mb["type"] if mb else None  # Person / Group / DJ

    # official_website: scraped > wikidata direct
    official_website = scraped.get("official_website") or (wikidata["websites"][0] if wikidata and wikidata.get("websites") else None)

    return {
        "original_title": title,
        "artist_name": artist_name if (spotify or deezer or lastfm or wiki) else None,
        "spotify_id": spotify["id"] if spotify else None,
        "genres": genres,
        "locale_city": city,
        "official_website": official_website,
        "youtube_channel": youtube["channel_url"] if youtube else None,
        "youtube_channel_id": youtube["channel_id"] if youtube else None,
        "photo": photo,
        "spotify_url": spotify["spotify_url"] if spotify else None,
        "itunes_url": itunes["itunes_url"] if itunes else None,
        "fans_deezer": deezer["fans"] if deezer else None,
        "followers_spotify": spotify["followers"] if spotify else None,
        "popularity": spotify["popularity"] if spotify else None,
        "bio": bio,
        "email": scraped.get("email"),
        "real_name": discogs["real_name"] if discogs else None,
        "discogs_url": discogs["discogs_url"] if discogs else None,
        "official_links": discogs["urls"] if discogs else [],
        "social_links": scraped.get("social_links", {}),
        "press_urls": scraped.get("press_urls", []),
        "wiki_url": wiki["wiki_url"] if wiki else None,
        "lastfm_url": lastfm["lastfm_url"] if lastfm else None,
        "country": country,
        "artist_type": artist_type,
        "mb_id": mb["mb_id"] if mb else None,
        "born": wikidata["born"] if wikidata else None,
        "birthplace": wikidata["birthplace"] if wikidata else None,
        "years_active": wikidata["years_active"] if wikidata else None,
        "occupations": wikidata["occupations"] if wikidata else [],
        "instruments": wikidata["instruments"] if wikidata else [],
        "record_labels": wikidata["labels"] if wikidata else [],
        "confidence": min(score, 100),
        "confidence_reasons": ", ".join(reasons),
        "venue": venue,
        "city": city
    }
