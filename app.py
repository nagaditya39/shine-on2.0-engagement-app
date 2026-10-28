"""Event engagement app: Care ratings + Courage pitch comments, AI summaries via a laptop worker.

Runs on Render (Postgres via DATABASE_URL) or locally (SQLite fallback).
Phones only talk to this app. The laptop worker polls /api/worker/next for jobs.
"""
import io
import json
import os
import re
import secrets
import time
from collections import Counter
from pathlib import Path
from typing import Literal

import qrcode
import qrcode.image.svg
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "dev-worker-token")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # optional override for QR target

# ---------------------------------------------------------------- database
DB_URL = os.getenv("DATABASE_URL", "sqlite:///event.db")
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgres://"):]
elif DB_URL.startswith("postgresql://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgresql://"):]

if DB_URL.startswith("sqlite"):
    engine = sa.create_engine(DB_URL, connect_args={"check_same_thread": False})

    @sa.event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
else:
    engine = sa.create_engine(DB_URL, pool_size=10, max_overflow=5, pool_timeout=10, pool_pre_ping=True)

md = sa.MetaData()
sessions = sa.Table(
    "sessions", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(16), nullable=False),            # care | courage
    sa.Column("title", sa.String(120), nullable=False),
    sa.Column("blurb", sa.Text, nullable=False, default=""),     # admin notes, fed to the LLM, never shown to phones
    sa.Column("status", sa.String(16), nullable=False, default="draft"),  # draft | open | closed
    sa.Column("questions", sa.Text, nullable=False, default="[]"),
    sa.Column("summary", sa.Text),                                # JSON {headline, points}
    sa.Column("summary_state", sa.String(16), nullable=False, default="none"),  # none|pending|ready|failed
    sa.Column("created_at", sa.Float, default=time.time),
)
responses = sa.Table(
    "responses", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.Integer, nullable=False, index=True),
    sa.Column("device_id", sa.String(64), nullable=False),
    sa.Column("answers", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, default=time.time),
    sa.UniqueConstraint("session_id", "device_id", name="uq_resp_dev"),
)
comments = sa.Table(
    "comments", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.Integer, nullable=False, index=True),
    sa.Column("device_id", sa.String(64), nullable=False),
    sa.Column("text", sa.String(300), nullable=False),
    sa.Column("hidden", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", sa.Float, default=time.time),
)
jobs = sa.Table(
    "jobs", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.Integer, nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),            # summarize | questions
    sa.Column("payload", sa.Text, nullable=False),
    sa.Column("status", sa.String(16), nullable=False, default="pending"),  # pending|running|done|failed
    sa.Column("claimed_at", sa.Float),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.Float, default=time.time),
)
kv = sa.Table(
    "kv", md,
    sa.Column("key", sa.String(32), primary_key=True),
    sa.Column("value", sa.Text),
)
md.create_all(engine)


def kv_get(c, key, default=None):
    r = c.execute(sa.select(kv.c.value).where(kv.c.key == key)).first()
    return r[0] if r else default


def kv_set(c, key, value):
    r = c.execute(sa.update(kv).where(kv.c.key == key).values(value=str(value)))
    if r.rowcount == 0:
        c.execute(sa.insert(kv).values(key=key, value=str(value)))


# ---------------------------------------------------------------- tiny cache
_cache: dict = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def bust():
    _cache.clear()


# ---------------------------------------------------------------- helpers
DEFAULT_Q = [
    "How clearly was the hazard or risk explained?",
    "How effective was the team's response?",
    "How likely are you to apply this lesson in your own area?",
]
STOP = set(
    "the a an and or but to of in on for with is are was were be been it this that these those i you we they he she "
    "at as by from so very really just have has had not no yes can will would should could our your their my me us "
    "all more most also too than then there here what which who how why when its it's into about out up do does".split()
)


def mkq(texts):
    return [{"id": f"q{i + 1}", "text": t.strip()[:140]} for i, t in enumerate([t for t in texts if t.strip()][:5])]


def pub_session(r):
    if not r:
        return None
    return {
        "id": r.id, "kind": r.kind, "title": r.title, "status": r.status,
        "questions": json.loads(r.questions or "[]"),
        "summary": json.loads(r.summary) if r.summary else None,
        "summary_state": r.summary_state,
    }


def care_stats(c, s):
    qs = json.loads(s.questions or "[]")
    rows = c.execute(sa.select(responses.c.answers).where(responses.c.session_id == s.id)).all()
    agg = {q["id"]: [0] * 5 for q in qs}
    for (a,) in rows:
        for qid, v in json.loads(a).items():
            if qid in agg and isinstance(v, int) and 1 <= v <= 5:
                agg[qid][v - 1] += 1
    out, tot, cnt = [], 0, 0
    for q in qs:
        counts = agg[q["id"]]
        n = sum(counts)
        avg = sum((i + 1) * k for i, k in enumerate(counts)) / n if n else None
        tot += sum((i + 1) * k for i, k in enumerate(counts))
        cnt += n
        out.append({"id": q["id"], "text": q["text"], "counts": counts, "avg": avg})
    return {"n": len(rows), "questions": out, "overall": (tot / cnt) if cnt else None}


def top_words(texts, k=12):
    cnt = Counter()
    for t in texts:
        for w in re.findall(r"[a-zA-Z']{4,}", t.lower()):
            if w not in STOP:
                cnt[w] += 1
    return [{"word": w, "n": n} for w, n in cnt.most_common(k)]


def build_display():
    with engine.connect() as c:
        mode = kv_get(c, "display_mode", "live")
        if mode == "overview":
            items = []
            for s in c.execute(sa.select(sessions).where(sessions.c.status != "draft").order_by(sessions.c.id)).all():
                if s.kind == "care":
                    st = care_stats(c, s)
                    items.append({"kind": "care", "title": s.title, "n": st["n"], "overall": st["overall"]})
                else:
                    n = c.execute(sa.select(sa.func.count()).select_from(comments).where(
                        comments.c.session_id == s.id, comments.c.hidden.is_(False))).scalar()
                    items.append({"kind": "courage", "title": s.title, "n": n,
                                  "summary": json.loads(s.summary) if s.summary else None})
            return {"mode": "overview", "items": items}
        aid = kv_get(c, "active_id")
        s = c.execute(sa.select(sessions).where(sessions.c.id == int(aid))).first() if aid else None
        if not s:
            return {"mode": "live", "session": None}
        out = {"mode": "live", "session": pub_session(s)}
        if s.kind == "care":
            out["stats"] = care_stats(c, s)
        else:
            rows = c.execute(sa.select(comments.c.text).where(
                comments.c.session_id == s.id, comments.c.hidden.is_(False)).order_by(comments.c.id.desc()).limit(200)).all()
            texts = [r[0] for r in rows]
            out["stats"] = {"n": len(texts), "latest": texts[:8], "words": top_words(texts)}
        return out


# ---------------------------------------------------------------- app
app = FastAPI(title="Event Engagement")
app.add_middleware(GZipMiddleware, minimum_size=500)
PAGES = {n: (BASE / "static" / f"{n}.html").read_text("utf-8") for n in ("phone", "display", "admin")}
NOCACHE = {"Cache-Control": "no-cache"}


def admin(x_admin_key: str = Header(default="")):
    if not secrets.compare_digest(x_admin_key.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "bad admin key")


def worker(authorization: str = Header(default="")):
    if not secrets.compare_digest(authorization.encode(), f"Bearer {WORKER_TOKEN}".encode()):
        raise HTTPException(401, "bad worker token")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def phone_page():
    return HTMLResponse(PAGES["phone"], headers=NOCACHE)


@app.get("/display", response_class=HTMLResponse)
def display_page():
    return HTMLResponse(PAGES["display"], headers=NOCACHE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(PAGES["admin"], headers=NOCACHE)


@app.get("/qr.svg")
def qr(request: Request):
    url = PUBLIC_URL or str(request.base_url)
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


# ---- phones
@app.get("/api/state")
def api_state():
    def build():
        with engine.connect() as c:
            aid = kv_get(c, "active_id")
            s = c.execute(sa.select(sessions).where(sessions.c.id == int(aid))).first() if aid else None
            p = pub_session(s)
            if p:
                p["summary"] = None  # phones don't need it
            return {"active": p}
    return cached("state", 1.5, build)


class Rate(BaseModel):
    session_id: int
    device_id: str = Field(min_length=8, max_length=64)
    answers: dict[str, int]


@app.post("/api/rate")
def api_rate(b: Rate):
    with engine.connect() as c:
        s = c.execute(sa.select(sessions).where(sessions.c.id == b.session_id)).first()
    if not s or s.kind != "care" or s.status != "open":
        raise HTTPException(409, "This session is closed")
    qids = {q["id"] for q in json.loads(s.questions)}
    ans = {k: v for k, v in b.answers.items() if k in qids and 1 <= v <= 5}
    if not ans:
        raise HTTPException(400, "No valid answers")
    row = dict(session_id=b.session_id, device_id=b.device_id, answers=json.dumps(ans), created_at=time.time())
    try:
        with engine.begin() as c:
            c.execute(sa.insert(responses).values(**row))
    except sa.exc.IntegrityError:  # same phone again: replace its answers
        with engine.begin() as c:
            c.execute(sa.update(responses).where(
                responses.c.session_id == b.session_id, responses.c.device_id == b.device_id
            ).values(answers=row["answers"], created_at=row["created_at"]))
    return {"ok": True}


class Comment(BaseModel):
    session_id: int
    device_id: str = Field(min_length=8, max_length=64)
    text: str


@app.post("/api/comment")
def api_comment(b: Comment):
    text = " ".join(b.text.split())[:280]
    if len(text) < 3:
        raise HTTPException(400, "Comment too short")
    with engine.connect() as c:
        s = c.execute(sa.select(sessions).where(sessions.c.id == b.session_id)).first()
        if not s or s.kind != "courage" or s.status != "open":
            raise HTTPException(409, "This session is closed")
        mine = c.execute(sa.select(sa.func.count()).select_from(comments).where(
            comments.c.session_id == b.session_id, comments.c.device_id == b.device_id)).scalar()
    if mine >= 3:
        raise HTTPException(429, "Comment limit reached")
    with engine.begin() as c:
        c.execute(sa.insert(comments).values(session_id=b.session_id, device_id=b.device_id, text=text,
                                             hidden=False, created_at=time.time()))
    return {"ok": True}


# ---- big screen
@app.get("/api/display")
def api_display():
    return cached("display", 1.5, build_display)


# ---- admin
class NewSession(BaseModel):
    kind: Literal["care", "courage"]
    title: str = Field(min_length=1, max_length=120)
    blurb: str = Field(default="", max_length=1500)
    questions: list[str] = []
    ai_questions: bool = False


@app.post("/api/admin/sessions", dependencies=[Depends(admin)])
def admin_create(b: NewSession):
    qs = mkq(b.questions) or mkq(DEFAULT_Q) if b.kind == "care" else []
    with engine.begin() as c:
        sid = c.execute(sa.insert(sessions).values(
            kind=b.kind, title=b.title.strip(), blurb=b.blurb, status="draft",
            questions=json.dumps(qs), summary_state="none", created_at=time.time())).inserted_primary_key[0]
        if b.kind == "care" and b.ai_questions and b.blurb.strip():
            c.execute(sa.insert(jobs).values(session_id=sid, kind="questions", status="pending", created_at=time.time(),
                                             payload=json.dumps({"title": b.title, "blurb": b.blurb})))
    bust()
    return {"id": sid}


class EditSession(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    blurb: str | None = Field(default=None, max_length=1500)
    questions: list[str] | None = None


@app.put("/api/admin/sessions/{sid}", dependencies=[Depends(admin)])
def admin_edit(sid: int, b: EditSession):
    vals = {}
    if b.title is not None:
        vals["title"] = b.title.strip()
    if b.blurb is not None:
        vals["blurb"] = b.blurb
    if b.questions is not None:
        vals["questions"] = json.dumps(mkq(b.questions))
    if vals:
        with engine.begin() as c:
            c.execute(sa.update(sessions).where(sessions.c.id == sid).values(**vals))
    bust()
    return {"ok": True}


class Status(BaseModel):
    status: Literal["draft", "open", "closed"]


@app.post("/api/admin/sessions/{sid}/status", dependencies=[Depends(admin)])
def admin_status(sid: int, b: Status):
    with engine.begin() as c:
        if b.status == "open":
            c.execute(sa.update(sessions).where(sessions.c.status == "open").values(status="closed"))
            kv_set(c, "active_id", sid)
            kv_set(c, "display_mode", "live")
        c.execute(sa.update(sessions).where(sessions.c.id == sid).values(status=b.status))
    bust()
    return {"ok": True}


@app.post("/api/admin/sessions/{sid}/activate", dependencies=[Depends(admin)])
def admin_activate(sid: int):
    with engine.begin() as c:
        kv_set(c, "active_id", sid)
        kv_set(c, "display_mode", "live")
    bust()
    return {"ok": True}


@app.post("/api/admin/sessions/{sid}/summarize", dependencies=[Depends(admin)])
def admin_summarize(sid: int):
    with engine.begin() as c:
        s = c.execute(sa.select(sessions).where(sessions.c.id == sid)).first()
        if not s or s.kind != "courage":
            raise HTTPException(400, "Only pitch sessions can be summarised")
        rows = c.execute(sa.select(comments.c.text).where(
            comments.c.session_id == sid, comments.c.hidden.is_(False)).order_by(comments.c.id.desc()).limit(150)).all()
        texts, total = [], 0
        for (t,) in rows:
            total += len(t)
            if total > 12000:
                break
            texts.append(t)
        if not texts:
            raise HTTPException(400, "No comments yet")
        c.execute(sa.delete(jobs).where(jobs.c.session_id == sid, jobs.c.kind == "summarize", jobs.c.status == "pending"))
        c.execute(sa.insert(jobs).values(session_id=sid, kind="summarize", status="pending", created_at=time.time(),
                                         payload=json.dumps({"title": s.title, "blurb": s.blurb, "comments": texts})))
        c.execute(sa.update(sessions).where(sessions.c.id == sid).values(summary_state="pending"))
    bust()
    return {"ok": True, "comments": len(texts)}


class ManualSummary(BaseModel):
    headline: str = Field(max_length=200)
    points: list[str] = Field(max_length=4)


@app.put("/api/admin/sessions/{sid}/summary", dependencies=[Depends(admin)])
def admin_manual_summary(sid: int, b: ManualSummary):
    data = {"headline": b.headline.strip(), "points": [p.strip()[:220] for p in b.points if p.strip()]}
    with engine.begin() as c:
        c.execute(sa.update(sessions).where(sessions.c.id == sid).values(summary=json.dumps(data), summary_state="ready"))
    bust()
    return {"ok": True}


@app.get("/api/admin/sessions", dependencies=[Depends(admin)])
def admin_list():
    with engine.connect() as c:
        rows = c.execute(sa.select(sessions).order_by(sessions.c.id)).all()
        rc = dict(c.execute(sa.select(responses.c.session_id, sa.func.count()).group_by(responses.c.session_id)).all())
        cc = dict(c.execute(sa.select(comments.c.session_id, sa.func.count()).where(
            comments.c.hidden.is_(False)).group_by(comments.c.session_id)).all())
        seen = kv_get(c, "worker_seen")
        mode = kv_get(c, "display_mode", "live")
        aid = kv_get(c, "active_id")
        pending = c.execute(sa.select(sa.func.count()).select_from(jobs).where(jobs.c.status.in_(["pending", "running"]))).scalar()
    out = []
    for r in rows:
        p = pub_session(r)
        p.update(blurb=r.blurb, n_responses=rc.get(r.id, 0), n_comments=cc.get(r.id, 0))
        out.append(p)
    return {"sessions": out, "display_mode": mode, "active_id": int(aid) if aid else None,
            "worker_age": (time.time() - float(seen)) if seen else None, "pending_jobs": pending}


@app.get("/api/admin/sessions/{sid}/comments", dependencies=[Depends(admin)])
def admin_comments(sid: int):
    with engine.connect() as c:
        rows = c.execute(sa.select(comments).where(comments.c.session_id == sid).order_by(comments.c.id.desc()).limit(100)).all()
    return [{"id": r.id, "text": r.text, "hidden": r.hidden} for r in rows]


@app.post("/api/admin/comments/{cid}/hide", dependencies=[Depends(admin)])
def admin_hide(cid: int, hidden: bool = True):
    with engine.begin() as c:
        c.execute(sa.update(comments).where(comments.c.id == cid).values(hidden=hidden))
    bust()
    return {"ok": True}


class Mode(BaseModel):
    mode: Literal["live", "overview"]


@app.post("/api/admin/display", dependencies=[Depends(admin)])
def admin_display(b: Mode):
    with engine.begin() as c:
        kv_set(c, "display_mode", b.mode)
    bust()
    return {"ok": True}


@app.get("/api/admin/export", dependencies=[Depends(admin)])
def admin_export():
    with engine.connect() as c:
        def rows(t):
            return [dict(r._mapping) for r in c.execute(sa.select(t)).all()]
        return {"sessions": rows(sessions), "responses": rows(responses), "comments": rows(comments)}


class Purge(BaseModel):
    confirm: str


@app.post("/api/admin/purge", dependencies=[Depends(admin)])
def admin_purge(b: Purge):
    if b.confirm != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    with engine.begin() as c:
        for t in (responses, comments, jobs, sessions, kv):
            c.execute(sa.delete(t))
    bust()
    return {"ok": True}


# ---- laptop worker
@app.get("/api/worker/next", dependencies=[Depends(worker)])
def worker_next():
    now = time.time()
    with engine.begin() as c:
        kv_set(c, "worker_seen", now)
        row = c.execute(sa.select(jobs).where(sa.or_(
            jobs.c.status == "pending",
            sa.and_(jobs.c.status == "running", jobs.c.claimed_at < now - 90),
        )).order_by(jobs.c.id).limit(1)).first()
        if not row:
            return {"job": None}
        r = c.execute(sa.update(jobs).where(jobs.c.id == row.id, jobs.c.status == row.status).values(
            status="running", claimed_at=now))
        if r.rowcount != 1:
            return {"job": None}
    return {"job": {"id": row.id, "kind": row.kind, "payload": json.loads(row.payload)}}


class JobResult(BaseModel):
    job_id: int
    result: dict


@app.post("/api/worker/result", dependencies=[Depends(worker)])
def worker_result(b: JobResult):
    with engine.begin() as c:
        job = c.execute(sa.select(jobs).where(jobs.c.id == b.job_id)).first()
        if not job:
            raise HTTPException(404, "no such job")
        if job.kind == "summarize":
            pts = [str(p).strip()[:220] for p in (b.result.get("points") or []) if str(p).strip()][:4]
            data = {"headline": str(b.result.get("headline", "")).strip()[:200], "points": pts}
            c.execute(sa.update(sessions).where(sessions.c.id == job.session_id).values(
                summary=json.dumps(data), summary_state="ready"))
        elif job.kind == "questions":
            qs = mkq([str(q) for q in (b.result.get("questions") or [])])
            s = c.execute(sa.select(sessions).where(sessions.c.id == job.session_id)).first()
            if qs and s and s.status == "draft":  # never change questions once phones may have answered
                c.execute(sa.update(sessions).where(sessions.c.id == job.session_id).values(questions=json.dumps(qs)))
        c.execute(sa.update(jobs).where(jobs.c.id == job.id).values(status="done"))
    bust()
    return {"ok": True}


class JobFail(BaseModel):
    job_id: int
    error: str = ""


@app.post("/api/worker/fail", dependencies=[Depends(worker)])
def worker_fail(b: JobFail):
    with engine.begin() as c:
        job = c.execute(sa.select(jobs).where(jobs.c.id == b.job_id)).first()
        if job:
            c.execute(sa.update(jobs).where(jobs.c.id == job.id).values(status="failed", error=b.error[:500]))
            if job.kind == "summarize":
                c.execute(sa.update(sessions).where(sessions.c.id == job.session_id).values(summary_state="failed"))
    bust()
    return {"ok": True}
