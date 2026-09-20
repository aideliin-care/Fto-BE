# Reservation dashboard integration contract for agents

```yaml
contract_version: 2
status: current-api-with-dashboard-read-model
service: appointment-service
production_base_url: https://fto-be-production.up.railway.app
local_base_url: http://127.0.0.1:8787
dashboard_origins:
  - https://fto-fe-new.vercel.app
  - http://localhost:5173
content_type: application/json
time_format: RFC3339 with numeric offset
timezone_in_seeded_data: Asia/Taipei (+08:00)
error_shape:
  error:
    code: string
    message: string
openapi: /openapi.json
interactive_docs: /docs
```

## Integration rules

1. Treat only the endpoints marked **implemented** below as callable now.
2. Do not read `clinic.db`, query SQLite, scrape the FastAPI docs, or infer
   booked reservations from free slots.
3. `/find_slots` returns only free, future slots. It is not a reservation list.
4. `/lookup_patient` returns only future `BOOKED` appointments for one exact
   phone value. It is not a search endpoint and cannot populate a clinic-wide
   dashboard.
5. Parse timestamps as offset-aware instants; preserve their offset in data
   exchange.
6. For non-2xx responses, parse `error.code` and branch on it. Do not branch
   on `error.message`.
7. CORS is already enabled for `https://fto-fe-new.vercel.app` and
   `http://localhost:5173`. Send the required `X-Dashboard-Key` from a
   server-side Vercel proxy, never browser JavaScript.
8. Browser code calls the Vercel proxy. The proxy calls Railway with
   `X-Dashboard-Key`; do not put the API key in `VITE_*` or `NEXT_PUBLIC_*`.

## Implemented endpoints

### `GET /health`

```yaml
request: null
response_200:
  ok: boolean
  now: rfc3339_datetime
  slots: integer
  free: integer
```

### `POST /lookup_patient`

```yaml
request:
  phone: non_empty_string
response_200:
  found: boolean
  patient_id: string | omitted
  name: string | omitted
  upcoming:
    - appointment_id: string
      starts_at: rfc3339_datetime
      doctor_name: string
errors:
  400: [BAD_JSON, MISSING_FIELD, BAD_REQUEST]
  500: [INTERNAL]
```

When `found` is `false`, `patient_id` and `name` are omitted and `upcoming` is
an empty array.

### `POST /find_slots`

```yaml
request:
  department: string | omitted
  doctor_name: string | omitted # substring match
  earliest: rfc3339_datetime | omitted # inclusive
  latest: rfc3339_datetime | omitted # inclusive
  limit: integer >= 1 | omitted # default 5; service clamps values to 50
response_200:
  slots:
    - slot_id: string
      doctor_name: string
      department: string
      starts_at: rfc3339_datetime
      ends_at: rfc3339_datetime
errors:
  400: [BAD_JSON, MISSING_FIELD, BAD_REQUEST]
  500: [INTERNAL]
```

### `POST /book`

```yaml
request:
  call_id: non_empty_string # idempotency key
  slot_id: non_empty_string
  patient_name: non_empty_string
  phone: non_empty_string
response_200:
  appointment_id: string
  starts_at: rfc3339_datetime
  doctor_name: string
  status: BOOKED
errors:
  400: [BAD_JSON, MISSING_FIELD, BAD_REQUEST]
  404: [SLOT_NOT_FOUND]
  409: [SLOT_TAKEN, SLOT_IN_PAST]
  500: [INTERNAL]
```

Retry an uncertain write with the same `call_id`; do not generate a new key
for that retry. If `SLOT_TAKEN` or `SLOT_IN_PAST`, refetch slots before a new
booking attempt.

### `POST /reschedule`

```yaml
request:
  call_id: non_empty_string
  appointment_id: non_empty_string
  new_slot_id: non_empty_string
response_200:
  appointment_id: string
  starts_at: rfc3339_datetime
  doctor_name: string
  status: BOOKED
errors:
  400: [BAD_JSON, MISSING_FIELD, BAD_REQUEST]
  404: [APPOINTMENT_NOT_FOUND, SLOT_NOT_FOUND]
  409: [SLOT_TAKEN, SLOT_IN_PAST]
  500: [INTERNAL]
```

### `POST /cancel`

```yaml
request:
  call_id: non_empty_string
  appointment_id: non_empty_string
response_200:
  appointment_id: string
  status: CANCELLED
errors:
  400: [BAD_JSON, MISSING_FIELD, BAD_REQUEST]
  404: [APPOINTMENT_NOT_FOUND]
  500: [INTERNAL]
```

Cancellation is idempotent: cancelling an already cancelled appointment is a
successful `200` response with `status: CANCELLED`.

### `GET /dashboard/reservations`

```yaml
request:
  headers:
    X-Dashboard-Key: required
  query:
    from: rfc3339_datetime | omitted # inclusive
    to: rfc3339_datetime | omitted # inclusive
    status: BOOKED | CANCELLED | omitted
    limit: integer 1..100 | omitted # default 50
    cursor: opaque_string | omitted
response_200:
  items:
    - appointment_id: string
      status: BOOKED | CANCELLED
      patient: { id: string, name: string, phone: string }
      doctor: { id: string, name: string, department: string }
      starts_at: rfc3339_datetime
      ends_at: rfc3339_datetime
      created_at: rfc3339_datetime
      updated_at: rfc3339_datetime
  next_cursor: opaque_string | null
errors:
  400: [BAD_REQUEST]
  403: [DASHBOARD_FORBIDDEN]
  503: [DASHBOARD_DISABLED]
```

Production URL:

```text
https://fto-be-production.up.railway.app/dashboard/reservations
```

Integration algorithm:

```yaml
browser:
  request: GET /api/dashboard/reservations?status=BOOKED&limit=50
vercel_server_route:
  allowed_query_keys: [from, to, status, limit, cursor]
  upstream: ${FTO_BE_API_URL}/dashboard/reservations
  upstream_headers:
    X-Dashboard-Key: ${DASHBOARD_API_KEY}
  cache: no-store
railway:
  required_variable: DASHBOARD_API_KEY
  required_value_relationship: must_equal_vercel_DASHBOARD_API_KEY
```

Pagination algorithm:

```yaml
initial_request: GET ?limit=50
next_page: GET ?limit=50&cursor=<previous.next_cursor>
end_condition: next_cursor == null
sort: starts_at ascending, then appointment_id ascending
reset_cursor_when_any_filter_changes: true
```

## Dashboard capability status

```yaml
all_reservations_list: implemented
reservation_history: implemented
appointment_search: not_implemented
pagination: implemented_cursor
dashboard_authentication: api_key_required
cross_origin_browser_access: configurable_with_CORS_ORIGINS
direct_database_access_from_frontend: forbidden
```

The endpoint is read-only. A dashboard needs a server-side session/role model
before it can safely expose data to multiple staff members.
