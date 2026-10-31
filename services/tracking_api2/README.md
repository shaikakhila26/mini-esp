# Mini ESP Tracking API

Implements the agreed tracking routes against the shared PostgreSQL schema.

## Routes

- `GET /o/{token}.gif`: record an `open` observation when the token is valid and campaign tracking is enabled; return a 1x1 GIF.
- `HEAD /o/{token}.gif`: return the GIF without recording an event.
- `GET /c/{token}/{link_no}`: resolve the stored link for the message's campaign, record a `click` when tracking is enabled, and redirect with HTTP 302.
- `HEAD /c/{token}/{link_no}`: validate and resolve the link without recording an event.
- `GET /health`: check application and database availability.

Interactive API docs are disabled.

## Environment

- `DATABASE_URL`: PostgreSQL connection string.
- `TRACKING_SOURCE`: optional event source label; defaults to `tracking_api`.

Example for local development when PostgreSQL is published on the host loopback:
`DATABASE_URL=postgresql://esp_app:<password>@127.0.0.1:5432/mini_esp`

Inside Docker Compose, `127.0.0.1` points to the API container. Use the PostgreSQL service DNS name instead, e.g.
`postgresql://esp_app:<password>@postgres:5432/mini_esp`.

## Build

Run from the repository root (the Docker build context must be `.`):

```sh
docker build -f services/tracking_api/Dockerfile -t mini-esp-tracking .
```

For Compose, configure the service with:

```yaml
tracking_api:
  build:
    context: .
    dockerfile: services/tracking_api/Dockerfile
  environment:
    DATABASE_URL: postgresql://esp_app:${POSTGRES_PASSWORD}@postgres:5432/mini_esp
    TRACKING_SOURCE: tracking_api
  depends_on:
    postgres:
      condition: service_healthy
  ports:
    - "127.0.0.1:8002:8000"
  restart: unless-stopped
```

Do not commit real credentials. Keep the service bound to loopback until the reverse proxy is configured.

## Event semantics

Each eligible GET observation gets a fresh `idempotency_key`. Repeated opens and clicks are separate observations. The API does not store recipient email addresses or IP addresses. Database errors produce HTTP 503; click redirects are not issued when the required lookup or event insert fails.

## Important deployment note

This implementation expects the `001_init` schema shown in the project plan. The `campaign_links.destination_url` database constraint checks the HTTPS prefix; the application also parses the stored URL before redirecting. Only campaign-approved destinations should be inserted by the Campaign API.
