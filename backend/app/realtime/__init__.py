"""Real-time push plumbing (dataset-library spec, Requirement 6).

This package holds the glue for the API Gateway WebSocket API:

- :mod:`app.realtime.connections` – the ``ws-connections`` DynamoDB store that
  records and reaps active connection IDs. Shared by the connect/disconnect
  Lambda handlers and the push consumer so there is one place that touches the
  table.
- :mod:`app.realtime.connect` / :mod:`app.realtime.disconnect` – the tiny
  ``$connect`` and ``$disconnect`` Lambda handlers. They are the only real-time
  code that inspects Lambda WebSocket event shapes; the application compute
  (the push consumer) stays runtime-agnostic.
"""
