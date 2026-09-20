#!/usr/bin/env bash
# Smoke test for the appointment service. Proves the seam works on its own,
# with no voice layer. Start the service first:
#
#   python3 seed.py --reset
#   python3 -m uvicorn main:app --host 127.0.0.1 --port 8787
#   ./test_api.sh
#
# Exits non-zero if any assertion failed.

set -uo pipefail
API=${API:-http://127.0.0.1:8787}
PHONE=${PHONE:-0912$RANDOM$RANDOM}
CALL=${CALL:-call-$RANDOM}
DB_PATH=${CLINIC_DB_PATH:-clinic.db}
FAIL=0

post() { curl -s --max-time 10 -X POST "$API/$1" \
           -H 'content-type: application/json' -d "$2"; }
code() { curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$API/$1" \
           -H 'content-type: application/json' -d "$2"; }

# get <json> <key> [key...]   — walks the document; "#" yields a length
get() {
  local doc=$1; shift
  printf '%s' "$doc" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1:]:
    if k == "#":
        d = len(d); break
    d = d[int(k)] if k.lstrip("-").isdigit() else d[k]
print(str(d).lower() if isinstance(d, bool) else d)
' "$@" 2>/dev/null
}

ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { printf '  \033[31mFAIL\033[0m %s\n       expected: %s\n       got:      %s\n' \
          "$1" "$2" "$3"; FAIL=1; }
is()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "$3" "${2:-<empty>}"; fi; }

echo "== health =="
curl -s --max-time 5 "$API/health"; echo

echo
echo "== 1. lookup_patient: unknown caller is a miss, not an error =="
R=$(post lookup_patient "{\"phone\":\"$PHONE\"}")
is "found is false" "$(get "$R" found)" "false"

echo
echo "== 2. find_slots: free future slots, limit respected =="
R=$(post find_slots '{"limit":3}')
is "returned 3 slots" "$(get "$R" slots '#')" "3"
SLOT1=$(get "$R" slots 0 slot_id)
SLOT2=$(get "$R" slots 1 slot_id)
echo "  slot1 = $SLOT1  ($(get "$R" slots 0 doctor_name), $(get "$R" slots 0 starts_at))"
echo "  slot2 = $SLOT2  ($(get "$R" slots 1 doctor_name), $(get "$R" slots 1 starts_at))"

echo
echo "== 3. find_slots: department filter =="
R=$(post find_slots '{"department":"皮膚科","limit":2}')
is "department is 皮膚科" "$(get "$R" slots 0 department)" "皮膚科"

echo
echo "== 4. find_slots: doctor_name matches a partial spoken name =="
R=$(post find_slots '{"doctor_name":"陳","limit":1}')
is "matched 陳柏宏" "$(get "$R" slots 0 doctor_name)" "陳柏宏"

echo
echo "== 5. book: the patient is created as part of booking =="
R=$(post book "{\"call_id\":\"$CALL\",\"slot_id\":\"$SLOT1\",\"patient_name\":\"王小明\",\"phone\":\"$PHONE\"}")
APPT=$(get "$R" appointment_id)
is "status is BOOKED" "$(get "$R" status)" "BOOKED"
if [ -n "$APPT" ]; then ok "appointment_id: $APPT"; else bad "appointment_id returned" "a uuid" "$R"; fi

echo
echo "== 6. book: the same call_id twice returns the SAME appointment (redial) =="
R=$(post book "{\"call_id\":\"$CALL\",\"slot_id\":\"$SLOT2\",\"patient_name\":\"王小明\",\"phone\":\"$PHONE\"}")
is "idempotent on call_id" "$(get "$R" appointment_id)" "$APPT"

echo
echo "== 7. book: a different caller on the taken slot gets 409 SLOT_TAKEN =="
is "http status 409" \
   "$(code book "{\"call_id\":\"race-1\",\"slot_id\":\"$SLOT1\",\"patient_name\":\"李大華\",\"phone\":\"0988111222\"}")" \
   "409"
R=$(post book "{\"call_id\":\"race-2\",\"slot_id\":\"$SLOT1\",\"patient_name\":\"李大華\",\"phone\":\"0988111222\"}")
is "error code SLOT_TAKEN" "$(get "$R" error code)" "SLOT_TAKEN"

echo
echo "== 8. the booked slot disappears from find_slots =="
R=$(post find_slots '{"limit":50}')
if printf '%s' "$R" | grep -q "$SLOT1"
  then bad "booked slot withdrawn" "not offered" "still offered"
  else ok "booked slot withdrawn"; fi

echo
echo "== 9. lookup_patient: now a hit, with the upcoming appointment =="
R=$(post lookup_patient "{\"phone\":\"$PHONE\"}")
is "found is true" "$(get "$R" found)" "true"
is "name"          "$(get "$R" name)" "王小明"
is "1 upcoming"    "$(get "$R" upcoming '#')" "1"

echo
echo "== 10. reschedule onto a free slot =="
R=$(post reschedule "{\"call_id\":\"$CALL\",\"appointment_id\":\"$APPT\",\"new_slot_id\":\"$SLOT2\"}")
is "same appointment" "$(get "$R" appointment_id)" "$APPT"
is "status is BOOKED" "$(get "$R" status)" "BOOKED"

echo
echo "== 11. reschedule to the slot it already holds is a no-op =="
R=$(post reschedule "{\"call_id\":\"$CALL\",\"appointment_id\":\"$APPT\",\"new_slot_id\":\"$SLOT2\"}")
is "still BOOKED" "$(get "$R" status)" "BOOKED"

echo
echo "== 12. the original slot is released by the move =="
R=$(post find_slots '{"limit":50}')
if printf '%s' "$R" | grep -q "$SLOT1"
  then ok "old slot released"
  else bad "old slot released" "offered again" "still withheld"; fi

echo
echo "== 13. cancel, then cancel again =="
R=$(post cancel "{\"call_id\":\"$CALL\",\"appointment_id\":\"$APPT\"}")
is "status is CANCELLED" "$(get "$R" status)" "CANCELLED"
R=$(post cancel "{\"call_id\":\"$CALL\",\"appointment_id\":\"$APPT\"}")
is "cancel is idempotent" "$(get "$R" status)" "CANCELLED"

echo
echo "== 14. cancelling frees the slot for another caller =="
R=$(post book "{\"call_id\":\"later-1\",\"slot_id\":\"$SLOT2\",\"patient_name\":\"李大華\",\"phone\":\"0977333444\"}")
is "rebooked by someone else" "$(get "$R" status)" "BOOKED"

echo
echo "== 15. errors =="
is "unknown appointment is 404" "$(code cancel '{"call_id":"x","appointment_id":"nope"}')" "404"
is "unknown slot is 404" \
   "$(code book '{"call_id":"y","slot_id":"nope","patient_name":"A","phone":"0900"}')" "404"
R=$(post book '{"slot_id":"x","patient_name":"A","phone":"0900"}')
is "missing call_id is MISSING_FIELD" "$(get "$R" error code)" "MISSING_FIELD"
R=$(curl -s --max-time 10 -X POST "$API/book" -H 'content-type: application/json' -d 'not json')
is "bad body is BAD_JSON" "$(get "$R" error code)" "BAD_JSON"

echo
echo "== 16. notification rows, written by the database trigger =="
python3 - "$APPT" "$DB_PATH" <<'PY'
import sqlite3, sys
appt = sys.argv[1]
conn = sqlite3.connect(sys.argv[2])
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT n.event, ps.starts_at AS moved_from, s.starts_at AS at "
    "FROM notification n "
    "JOIN slot s ON s.id = n.slot_id "
    "LEFT JOIN slot ps ON ps.id = n.prev_slot_id "
    "WHERE n.appointment_id = ? ORDER BY n.id", (appt,)).fetchall()
for r in rows:
    extra = f"   moved_from={r['moved_from']}" if r["moved_from"] else ""
    print(f"  {r['event']:<10} at={r['at']}{extra}")
events = [r["event"] for r in rows]
expected = ["CREATED", "UPDATED", "CANCELLED"]
if events == expected:
    print(f"  \033[32mPASS\033[0m event sequence {events}")
else:
    print(f"  \033[31mFAIL\033[0m event sequence\n       expected: {expected}\n       got:      {events}")
    raise SystemExit(1)
PY
[ $? -ne 0 ] && FAIL=1

echo
if [ $FAIL -eq 0 ]
  then printf '\033[32mall checks passed\033[0m\n'
  else printf '\033[31msome checks failed\033[0m\n'; fi
exit $FAIL
