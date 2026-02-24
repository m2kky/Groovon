import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://groovon:groovon@db:5432/groovon")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS artists (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_normalized TEXT,
                    spotify_id TEXT UNIQUE,
                    spotify_url TEXT,
                    spotify_followers INTEGER,
                    popularity INTEGER,
                    deezer_fans INTEGER,
                    photo TEXT,
                    youtube_channel TEXT,
                    genres TEXT[],
                    bio TEXT,
                    email TEXT,
                    official_website TEXT,
                    locale_city TEXT,
                    itunes_url TEXT,
                    wiki_url TEXT,
                    lastfm_url TEXT,
                    discogs_url TEXT,
                    real_name TEXT,
                    official_links TEXT[],
                    country TEXT,
                    artist_type TEXT,
                    mb_id TEXT,
                    born TEXT,
                    birthplace TEXT,
                    years_active TEXT,
                    occupations TEXT[],
                    instruments TEXT[],
                    record_labels TEXT[],
                    social_links JSONB,
                    press_urls TEXT[],
                    confidence INTEGER DEFAULT 0,
                    sources TEXT[],
                    last_enriched_at TIMESTAMPTZ DEFAULT NOW(),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    raw_title TEXT NOT NULL,
                    venue TEXT,
                    city TEXT,
                    artist_id INTEGER REFERENCES artists(id),
                    processed_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS enrichment_log (
                    id SERIAL PRIMARY KEY,
                    artist_id INTEGER REFERENCES artists(id),
                    source_api TEXT,
                    field_name TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)


def normalize_name(name: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip()


def find_artist(name: str) -> dict | None:
    """Find artist by spotify_id or normalized name."""
    norm = normalize_name(name)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM artists WHERE name_normalized = %s LIMIT 1",
                (norm,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_artist(data: dict) -> int:
    """Insert or update artist, returns artist id."""
    norm = normalize_name(data.get("artist_name") or "")
    existing = None

    with db() as conn:
        with conn.cursor() as cur:
            # Try find by spotify_id first
            if data.get("spotify_id"):
                cur.execute("SELECT id FROM artists WHERE spotify_id = %s", (data["spotify_id"],))
                row = cur.fetchone()
                if row:
                    existing = row["id"]

            # Fallback: normalized name
            if not existing and norm:
                cur.execute("SELECT id FROM artists WHERE name_normalized = %s", (norm,))
                row = cur.fetchone()
                if row:
                    existing = row["id"]

            genres = data.get("genres") or []
            sources = _build_sources(data)

            if existing:
                cur.execute("""
                    UPDATE artists SET
                        photo = COALESCE(%s, photo),
                        bio = COALESCE(%s, bio),
                        genres = CASE WHEN %s != '{}' THEN %s ELSE genres END,
                        spotify_id = COALESCE(%s, spotify_id),
                        spotify_url = COALESCE(%s, spotify_url),
                        spotify_followers = COALESCE(%s, spotify_followers),
                        popularity = COALESCE(%s, popularity),
                        deezer_fans = COALESCE(%s, deezer_fans),
                        youtube_channel = COALESCE(%s, youtube_channel),
                        itunes_url = COALESCE(%s, itunes_url),
                        wiki_url = COALESCE(%s, wiki_url),
                        lastfm_url = COALESCE(%s, lastfm_url),
                        discogs_url = COALESCE(%s, discogs_url),
                        real_name = COALESCE(%s, real_name),
                        official_links = COALESCE(%s, official_links),
                        official_website = COALESCE(%s, official_website),
                        email = COALESCE(%s, email),
                        country = COALESCE(%s, country),
                        artist_type = COALESCE(%s, artist_type),
                        mb_id = COALESCE(%s, mb_id),
                        born = COALESCE(%s, born),
                        birthplace = COALESCE(%s, birthplace),
                        years_active = COALESCE(%s, years_active),
                        occupations = COALESCE(%s, occupations),
                        instruments = COALESCE(%s, instruments),
                        record_labels = COALESCE(%s, record_labels),
                        social_links = COALESCE(%s, social_links),
                        press_urls = COALESCE(%s, press_urls),
                        confidence = GREATEST(confidence, %s),
                        sources = %s,
                        last_enriched_at = NOW()
                    WHERE id = %s
                """, (
                    data.get("photo"), data.get("bio"),
                    genres, genres,
                    data.get("spotify_id"), data.get("spotify_url"),
                    data.get("followers_spotify"), data.get("popularity"),
                    data.get("fans_deezer"), data.get("youtube_channel"),
                    data.get("itunes_url"), data.get("wiki_url"), data.get("lastfm_url"),
                    data.get("discogs_url"), data.get("real_name"), data.get("official_links") or None,
                    data.get("official_website"), data.get("email"),
                    data.get("country"), data.get("artist_type"), data.get("mb_id"),
                    data.get("born"), data.get("birthplace"), data.get("years_active"),
                    data.get("occupations") or None, data.get("instruments") or None, data.get("record_labels") or None,
                    json.dumps(data["social_links"]) if data.get("social_links") else None,
                    data.get("press_urls") or None,
                    data.get("confidence", 0), sources, existing
                ))
                return existing
            else:
                cur.execute("""
                    INSERT INTO artists (
                        name, name_normalized, spotify_id, spotify_url, spotify_followers,
                        popularity, deezer_fans, photo, youtube_channel, genres, bio,
                        itunes_url, wiki_url, lastfm_url, discogs_url, real_name, official_links,
                        official_website, email, country, artist_type, mb_id,
                        born, birthplace, years_active, occupations, instruments, record_labels,
                        social_links,
                        press_urls,
                        locale_city, confidence, sources
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (
                    data.get("artist_name"), norm,
                    data.get("spotify_id"), data.get("spotify_url"), data.get("followers_spotify"),
                    data.get("popularity"), data.get("fans_deezer"), data.get("photo"),
                    data.get("youtube_channel"), genres, data.get("bio"),
                    data.get("itunes_url"), data.get("wiki_url"), data.get("lastfm_url"),
                    data.get("discogs_url"), data.get("real_name"), data.get("official_links") or None,
                    data.get("official_website"), data.get("email"),
                    data.get("country"), data.get("artist_type"), data.get("mb_id"),
                    data.get("born"), data.get("birthplace"), data.get("years_active"),
                    data.get("occupations") or None, data.get("instruments") or None, data.get("record_labels") or None,
                    json.dumps(data["social_links"]) if data.get("social_links") else None,
                    data.get("press_urls") or None,
                    data.get("locale_city"), data.get("confidence", 0), sources
                ))
                return cur.fetchone()["id"]


def save_event(raw_title: str, venue: str, city: str, artist_id: int | None):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (raw_title, venue, city, artist_id) VALUES (%s,%s,%s,%s)",
                (raw_title, venue, city, artist_id)
            )


def _build_sources(data: dict) -> list:
    sources = []
    if data.get("spotify_id"):
        sources.append("spotify")
    if data.get("fans_deezer") is not None:
        sources.append("deezer")
    if data.get("lastfm_url"):
        sources.append("lastfm")
    if data.get("wiki_url"):
        sources.append("wikipedia")
    if data.get("youtube_channel"):
        sources.append("youtube")
    if data.get("itunes_url"):
        sources.append("itunes")
    if data.get("discogs_url"):
        sources.append("discogs")
    if data.get("mb_id"):
        sources.append("musicbrainz")
    if data.get("email"):
        sources.append("scrapling")
    return sources
