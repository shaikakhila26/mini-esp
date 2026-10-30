# Event Ingestion API

Receives KumoMTA event JSON, correlates it to `messages.id` and `campaign_id`,
then appends an event row. This first version deliberately does not guess where
the correlation values live: the currently observed records have empty
`headers` and `meta` objects.

## Event mapping

| KumoMTA type | `events.event_type` |
|---|---|
| `Reception` | `mta_accepted` |
| `Delivery` | `remote_accepted` |
| `TransientFailure` | `deferred` |
| `Bounce` | `failed` |
| `Expiration` | `failed` |

Unknown types are logged and acknowledged. Missing/unknown correlation is
logged and acknowledged as skipped. Database failures return 503. A `Delivery`
event means remote SMTP acceptance, not inbox placement.

## Webhook contract

- `POST /webhooks/kumomta`
- Body: one JSON object or an array of event objects
- Authentication: `X-Webhook-Secret: <KUMO_WEBHOOK_SECRET>`
- Default max body: 1 MiB; default max batch: 500 records
- 2xx is returned only after the database transaction commits
- 401 auth failure, 400 invalid JSON/record timestamp, 413 too large, 503 DB failure

This is the contract expected by this service, not a claim that KumoMTA is
already configured with this URL or header. Coordinate the matching sender
configuration with the teammate responsible for KumoMTA.

## Correlation configuration

After inspecting a real webhook payload, configure exact dotted paths:
`KUMO_MESSAGE_ID_PATH` and `KUMO_CAMPAIGN_ID_PATH`. For example,
`meta.x_esp_message_id` and `meta.x_esp_campaign_id` are illustrative only;
do not use them unless verified in the actual payload. Values must be UUIDs.
The service validates that the message UUID belongs to the campaign UUID and
never correlates by recipient email.

## Idempotency

The key hashes the KumoMTA record ID, type, timestamp, attempt number, SMTP
response code/content, and recipient. It does not use KumoMTA ID alone because
Reception and Delivery may share that ID. `ON CONFLICT (idempotency_key)
DO NOTHING` makes exact replays idempotent. Confirm fingerprint behavior with
complete retry records before production use.

## Local run (from repository root)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r services/ingest/requirements.txt
export DATABASE_URL='postgresql://esp_app:<password>@127.0.0.1:5432/mini_esp'
export KUMO_WEBHOOK_SECRET='<local-long-random-secret>'
uvicorn app.main:app --app-dir services/ingest --host 127.0.0.1 --port 8003
```

Run unit tests:

```bash
PYTHONPATH=services/ingest python -m unittest discover -s services/ingest/tests -v
```

Build the image from repository root:

```bash
docker build -f services/ingest/Dockerfile -t mini-esp-ingest .
```

Compose must use repository root as build context, connect this service to the
PostgreSQL Compose network, and use `postgres` as the DB hostname. If native
KumoMTA runs on the same host, publish the ingestion port on loopback only,
for example `127.0.0.1:8003:8000`.