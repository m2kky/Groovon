from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uuid, json, asyncio
from parsers import parse_file
from enrichers import enrich_artist
from db import init_db, upsert_artist, save_event, find_artist

app = FastAPI(title="Groovon API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# job_id → {status, total, done, results}
jobs: dict = {}
# job_id → list of connected WebSockets
ws_clients: dict[str, list[WebSocket]] = {}


@app.on_event("startup")
def startup():
    try:
        init_db()
        # Migrate: add new columns if they don't exist
        from db import db as get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                for col, typ in [
                    ("country", "TEXT"), ("artist_type", "TEXT"), ("mb_id", "TEXT"),
                    ("born", "TEXT"), ("birthplace", "TEXT"), ("years_active", "TEXT"),
                    ("occupations", "TEXT[]"), ("instruments", "TEXT[]"), ("record_labels", "TEXT[]"),
                    ("social_links", "JSONB"), ("press_urls", "TEXT[]"),
                ]:
                    cur.execute(f"""
                        ALTER TABLE artists ADD COLUMN IF NOT EXISTS {col} {typ};
                    """)
    except Exception as e:
        print(f"DB init warning: {e}")


class SingleRequest(BaseModel):
    title: str
    venue: Optional[str] = None
    city: Optional[str] = None


class BulkRequest(BaseModel):
    items: List[SingleRequest]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/enrich/single")
def enrich_single(req: SingleRequest):
    result = enrich_artist(req.title, req.venue, req.city)
    if result.get("artist_name"):
        artist_id = upsert_artist(result)
        save_event(req.title, req.venue, req.city, artist_id)
        result["artist_id"] = artist_id
    return result


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    content = await file.read()
    try:
        items = parse_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    items = items[:10]  # Demo limit
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "total": len(items), "done": 0, "results": []}
    ws_clients[job_id] = []
    background_tasks.add_task(run_job, job_id, items)
    return {"job_id": job_id, "total": len(items)}


@app.get("/artists")
def list_artists(limit: int = 100, offset: int = 0):
    from db import db as get_db
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, real_name, photo, genres, bio, confidence, sources,
                       spotify_url, youtube_channel, wiki_url, discogs_url, lastfm_url,
                       locale_city, official_links, official_website, email,
                       country, artist_type, spotify_followers, popularity, deezer_fans,
                       spotify_id, itunes_url, mb_id,
                       born, birthplace, years_active, occupations, instruments, record_labels,
                       social_links, press_urls,
                       last_enriched_at
                FROM artists
                ORDER BY confidence DESC, last_enriched_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) as total FROM artists")
            total = cur.fetchone()["total"]
    return {"total": total, "artists": [dict(r) for r in rows]}


@app.get("/artists/{artist_id}")
def get_artist(artist_id: int):
    from db import db as get_db
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM artists WHERE id = %s", (artist_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Artist not found")
            return dict(row)


class ArtistPatch(BaseModel):
    name: Optional[str] = None
    real_name: Optional[str] = None
    email: Optional[str] = None
    official_website: Optional[str] = None
    bio: Optional[str] = None
    genres: Optional[List[str]] = None
    country: Optional[str] = None
    locale_city: Optional[str] = None


@app.patch("/artists/{artist_id}")
def patch_artist(artist_id: int, patch: ArtistPatch):
    from db import db as get_db
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE artists SET {set_clause}, last_enriched_at = NOW() WHERE id = %s RETURNING id",
                        (*fields.values(), artist_id))
            if not cur.fetchone():
                raise HTTPException(404, "Artist not found")
    return {"ok": True}


@app.post("/artists/{artist_id}/re-enrich")
def re_enrich_artist(artist_id: int, background_tasks: BackgroundTasks):
    from db import db as get_db
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name, locale_city FROM artists WHERE id = %s", (artist_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Artist not found")
    background_tasks.add_task(_do_re_enrich, artist_id, row["name"], row["locale_city"])
    return {"ok": True, "message": "Re-enrichment started"}


def _do_re_enrich(artist_id: int, name: str, city: str):
    from db import db as get_db
    result = enrich_artist(name, city=city)
    result["artist_name"] = result.get("artist_name") or name
    if result.get("confidence", 0) > 0:
        upsert_artist(result)
        # Also update the specific artist id in case upsert matched a different row
        from db import _build_sources
        sources = _build_sources(result)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE artists SET
                        photo = COALESCE(%s, photo), bio = COALESCE(%s, bio),
                        genres = CASE WHEN %s::text[] IS NOT NULL AND array_length(%s::text[],1)>0 THEN %s ELSE genres END,
                        email = COALESCE(%s, email), official_website = COALESCE(%s, official_website),
                        country = COALESCE(%s, country), confidence = GREATEST(confidence, %s),
                        sources = %s, last_enriched_at = NOW()
                    WHERE id = %s
                """, (
                    result.get("photo"), result.get("bio"),
                    result.get("genres") or None, result.get("genres") or None, result.get("genres") or None,
                    result.get("email"), result.get("official_website"),
                    result.get("country"), result.get("confidence", 0),
                    sources, artist_id
                ))


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@app.websocket("/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()
    if job_id not in ws_clients:
        ws_clients[job_id] = []
    ws_clients[job_id].append(websocket)

    # Send already-completed results immediately
    if job_id in jobs:
        for r in jobs[job_id]["results"]:
            await websocket.send_text(json.dumps(r))

    try:
        while True:
            await asyncio.sleep(30)  # keep alive
    except WebSocketDisconnect:
        ws_clients[job_id].remove(websocket)


def run_job(job_id: str, items: list):
    import asyncio

    async def _broadcast(result: dict):
        for ws in list(ws_clients.get(job_id, [])):
            try:
                await ws.send_text(json.dumps(result))
            except Exception:
                pass

    loop = asyncio.new_event_loop()

    for item in items:
        result = enrich_artist(item["title"], item.get("venue"), item.get("city"))
        artist_id = None
        if result.get("artist_name"):
            artist_id = upsert_artist(result)
            result["artist_id"] = artist_id
        save_event(item["title"], item.get("venue"), item.get("city"), artist_id)
        jobs[job_id]["results"].append(result)
        jobs[job_id]["done"] += 1
        loop.run_until_complete(_broadcast(result))

    jobs[job_id]["status"] = "done"
    loop.run_until_complete(_broadcast({"__status__": "done", "total": jobs[job_id]["total"]}))
    loop.close()
