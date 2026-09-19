# Phone demo

The adapter uses Twilio `<Gather>` for speech-to-text and `<Say>` for playback.
It deliberately keeps call state in memory: one local demo process, no restart
recovery or horizontal scaling.

Set these in the gitignored root `.env`:

```env
OPENAI_API_KEY=sk-...
TWILIO_AUTH_TOKEN=...
PUBLIC_BASE_URL=https://your-public-tunnel.example
HANDOFF_NUMBER=+886...
```

Start the appointment API, export the variables, then start the adapter:

```zsh
cd fto-be
set -a; source .env; set +a
(cd api && python3 -m uvicorn main:app --host 127.0.0.1 --port 8787)
(cd voice && python3 -m uvicorn telephony:app --host 0.0.0.0 --port 8000)
```

Expose port 8000 with an HTTPS tunnel. In Twilio, set the number's incoming
Voice webhook to `https://your-public-tunnel.example/twilio/voice` using POST.
`PUBLIC_BASE_URL` must exactly match that public HTTPS origin so webhook
signature validation succeeds.

Run the adapter unit test from `voice/` with `python3 -m unittest -v test_telephony`.

## Railway

Railway starts the root `app:app`, which serves both the appointment API and
Twilio routes on its assigned `$PORT`. Attach a Railway Volume at `/data` and
set `CLINIC_DB_PATH=/data/clinic.db` before storing real appointments.
