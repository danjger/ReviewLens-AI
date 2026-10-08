# Capture integration-test fixtures

These pages are served over HTTP by the Compose `fixtures` nginx container and
are loaded by the headless-browser capture integration tests
(`backend/tests/integration/capture/test_capture_int.py`, dataset-ingestion
task 2.2). They exercise `app.capture.engine.render()`:

| Path | What it exercises |
|---|---|
| `capture/normal/` | A normal review page: title, main status 200, HTML + screenshot stored, final URL unchanged. |
| `capture/localhost_image/` | A page whose only sub-resource is an image on `127.0.0.1`. The capture SSRF route guard must abort that sub-request while the page itself still renders. |
| `capture/meta_refresh/` | A client-side (`<meta http-equiv="refresh">`) redirect to `capture/normal/`. The browser ends on the target, so `render()` reports `redirected=True` and a `final_url` on the target page (Req 2.8). |
| `capture/forbidden/` | A directory with no `index.html`; static nginx returns **403** for it, so the main document status is 403 (Req 2.7). It contains only `note.txt` so the directory exists in git. |
| `capture/lazy/` | Reviews injected into the DOM only after the page is scrolled, proving the single post-render scroll surfaces lazy-loaded reviews (Req 3.1/4.1). |

The tests run against `make up`. The browser reaches these pages at
`http://localhost:9090/capture/...`; `localhost` is allowed through the SSRF
guard for the test (`SSRF_TEST_ALLOW_HOSTS=localhost`) while `127.0.0.1` is
deliberately **not** allowed, which is what makes the `localhost_image` block
observable.
