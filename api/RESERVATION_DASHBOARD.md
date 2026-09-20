# Reservation dashboard frontend guide

## Production integration

| Item | Value |
| --- | --- |
| Railway API | `https://fto-be-production.up.railway.app` |
| Dashboard app | `https://fto-fe-new.vercel.app` |
| Local dashboard | `http://localhost:5173` |
| Reservations endpoint | `GET /dashboard/reservations` |

Railway is deployed and CORS is enabled for the two dashboard origins above.
The reservations API contains patient phone numbers, so the browser must not
hold its API key. Route dashboard requests through a Vercel server-side route.

## What another frontend can use today

This service is an appointment API for the voice flow and dashboard. Use
`GET /dashboard/reservations` for the clinic-wide, paginated reservation list;
do not infer bookings from `/find_slots` or read `clinic.db` directly.

## Run the API locally

From `fto-be/api`:

```bash
python3 seed.py --reset
python3 -m uvicorn main:app --host 127.0.0.1 --port 8787
```

The local API base URL is `http://127.0.0.1:8787`. FastAPI also exposes
interactive API documentation at `http://127.0.0.1:8787/docs` and its
machine-readable schema at `http://127.0.0.1:8787/openapi.json`.

## Available calls

| Need | Request | Result |
| --- | --- | --- |
| Service status | `GET /health` | Current time plus total and free-slot counts. |
| All reservations | `GET /dashboard/reservations` | Joined, paginated booking history. |
| A patient's upcoming visits | `POST /lookup_patient` with `{ "phone": "..." }` | Patient identity, if known, and their future `BOOKED` appointments. |
| Availability | `POST /find_slots` with optional filters | Free future slots only. |
| Create a reservation | `POST /book` | Creates or returns a booking idempotently by `call_id`. |
| Move a reservation | `POST /reschedule` | Moves one appointment to a free slot. |
| Cancel a reservation | `POST /cancel` | Marks an appointment `CANCELLED`; it is safe to repeat. |

All write requests use JSON and return JSON. Error bodies always have this
shape:

```json
{ "error": { "code": "SLOT_TAKEN", "message": "That time was just taken." } }
```

Use the HTTP status and `error.code`, rather than matching error text. In
particular, `409 SLOT_TAKEN` and `409 SLOT_IN_PAST` mean the UI should refresh
availability and let the user choose again.

## Example: patient appointment lookup

```ts
const apiBase = import.meta.env.VITE_RESERVATION_API_BASE_URL;

const response = await fetch(`${apiBase}/lookup_patient`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ phone: "0912345678" }),
});

const result = await response.json();
if (!response.ok) throw new Error(result.error?.code ?? "REQUEST_FAILED");

// result.upcoming is an array of { appointment_id, starts_at, doctor_name }
```

Times are ISO 8601/RFC 3339 strings with an offset, for example
`2026-09-21T09:00:00+08:00`. Parse and display them as timezone-aware values;
do not strip or reinterpret the offset.

## Dashboard reservations

`GET /dashboard/reservations` accepts optional `from`, `to`, and `status`
(`BOOKED` or `CANCELLED`) filters, a `limit` from 1 to 100 (default 50), and a
stable `cursor` returned as `next_cursor`. Results are ordered by appointment
start time, then appointment ID.

```http
GET /dashboard/reservations?from=2026-09-21T00:00:00%2B08:00&to=2026-09-28T00:00:00%2B08:00&status=BOOKED&limit=50&cursor=...
```

```json
{
  "items": [
    {
      "appointment_id": "uuid",
      "status": "BOOKED",
      "patient": { "id": "uuid", "name": "王小明", "phone": "0912345678" },
      "doctor": { "id": "uuid", "name": "陳柏宏", "department": "皮膚科" },
      "starts_at": "2026-09-21T09:00:00+08:00",
      "ends_at": "2026-09-21T09:30:00+08:00",
      "created_at": "2026-09-20T14:12:00+08:00",
      "updated_at": "2026-09-20T14:12:00+08:00"
    }
  ],
  "next_cursor": null
}
```

### Required environment variables

Set the same random value in both deployments, without exposing it to the
browser:

```bash
# Railway service variable
DASHBOARD_API_KEY=<long-random-secret>

# Vercel server environment variables (not VITE_* / not NEXT_PUBLIC_*)
FTO_BE_API_URL=https://fto-be-production.up.railway.app
DASHBOARD_API_KEY=<same-long-random-secret>
```

`CORS_ORIGINS` is already configured in Railway as
`http://localhost:5173,https://fto-fe-new.vercel.app`.

Until `DASHBOARD_API_KEY` exists in Railway, the endpoint deliberately returns
`503 DASHBOARD_DISABLED`. CORS only permits a browser origin; it does not
authorize access to patient data.

### Recommended Vercel proxy

Create a server route such as `/api/dashboard/reservations`. It should forward
only the supported query parameters and attach the secret server-side:

```ts
export async function GET(request: Request) {
  const url = new URL(request.url);
  const query = new URLSearchParams();
  for (const key of ["from", "to", "status", "limit", "cursor"]) {
    const value = url.searchParams.get(key);
    if (value) query.set(key, value);
  }

  const upstream = await fetch(
    `${process.env.FTO_BE_API_URL}/dashboard/reservations?${query}`,
    {
      headers: { "X-Dashboard-Key": process.env.DASHBOARD_API_KEY! },
      cache: "no-store",
    },
  );
  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: { "content-type": "application/json" },
  });
}
```

The browser then calls `/api/dashboard/reservations?...`, not Railway directly.
Add staff login/authorization to this Vercel route before making the dashboard
available to multiple users.

For an exact, machine-oriented integration contract, see
[`reservation-dashboard.agent.md`](reservation-dashboard.agent.md).
