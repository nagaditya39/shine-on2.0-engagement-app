# Event engagement app

Phones scan one QR code. The big screen shows live results. An AI worker on your laptop writes the summaries.

```
phones -> Render (FastAPI + Postgres) <- laptop worker (Ollama), outbound polling only
```

If the laptop or the model is down, ratings and comments keep working, and you can type a summary by hand in /admin.

## Pages
- `/` phones (QR target)
- `/display` projector, add as a browser window on the big screen
- `/admin` run the show (password = `ADMIN_PASSWORD`)

## Run locally (SQLite, no account)
```
pip install -r requirements.txt
uvicorn app:app --port 8000
# new terminal: python seed.py http://localhost:8000 admin 200
```
Admin password locally is `admin`.

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render dashboard: New > Blueprint > select the repo. It creates the web service and Postgres from `render.yaml`.
3. Open the service > Environment and copy `ADMIN_PASSWORD` and `WORKER_TOKEN`.
4. Open `https://<service>.onrender.com/admin`.

Free web services sleep after 15 minutes idle (about a minute to wake). Fine for the POC; for the event week change `plan` in
`render.yaml` to a paid instance (`standard`) and the database to a paid plan, then load-test on that tier.

## Laptop worker
```
ollama pull qwen2.5:7b          # or a 3-4B model on CPU-only machines
pip install requests
APP_URL=https://<service>.onrender.com WORKER_TOKEN=<token> python worker.py
```
Set `MODEL=...` to try other models. The worker only makes outbound HTTPS calls.

## Event flow
- Care: create a session with the division name and a few notes about the story. Optionally tick "AI writes the statements". Review them, press Open. Close when the speaker finishes. Phones show 1-5 ratings; the display shows averages and distributions.
- Courage: create a pitch session, Open it, audience comments from their phones. Close, then press "Summarise comments". The worker returns a positive, faithful recap that appears on the display. Hide any comment from "Moderate comments" before summarising.
- "Highlights" on the big screen shows every care score and every pitch recap so far.

## Load test and cleanup
`locust -f locustfile.py --host https://<service>.onrender.com -u 200 -r 20 -t 3m --headless`

After the event: Admin > Download all data, then Delete all data, then delete the Render services.

## Notes
- Phones poll every ~3s with jitter; the server caches state for 1.5s, so 200 phones cost a few DB queries per second.
- One device can submit one set of ratings per care session (resubmitting replaces it) and 3 comments per pitch.
- Data is anonymous: only a random per-phone ID is stored.
