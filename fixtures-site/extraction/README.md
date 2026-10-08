# Published extraction evaluation pages

Each `<name>/index.html` here is a byte-for-byte published copy of
`/evals/extraction/pages/<name>/page.html`. They are served over HTTP by the
fixtures site so integration and E2E tests can load the same labeled pages the
extraction evaluation suite scores.

These are generated artifacts: if an eval page under `/evals/extraction/pages/`
changes, regenerate the copy here, e.g. from the repo root:

    for d in evals/extraction/pages/*/; do
      name=$(basename "$d")
      mkdir -p "fixtures-site/extraction/$name"
      cp "$d/page.html" "fixtures-site/extraction/$name/index.html"
    done

The labels for these pages live in `/evals/extraction/labels.yaml`.
