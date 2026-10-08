# backend image – API service, chat service, push consumer, sweeper
#
# Runs unchanged on AWS Lambda (via the Lambda Web Adapter) and as a plain
# uvicorn container.  The CMD below is the container-mode default; the Lambda
# Web Adapter exec wrapper takes over automatically in Lambda mode.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# ── System dependencies ──────────────────────────────────────────────────────
RUN dnf install -y \
        python3.12 \
        python3.12-pip \
        python3.12-devel \
        gcc \
    && dnf clean all

# ── AWS Lambda Web Adapter ───────────────────────────────────────────────────
# Copies the LWA binary from its public ECR image.
# Active only when the Lambda runtime invokes the exec wrapper; harmless when
# running as a plain container.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 \
     /lambda-adapter /opt/extensions/lambda-adapter

# ── uv (fast package installer) ──────────────────────────────────────────────
RUN pip3.12 install --no-cache-dir "uv>=0.5"

# Amazon Linux ships no bare `python`; several service commands invoke
# `python -m app.consumer`, so symlink it to the 3.12 interpreter.
RUN ln -sf /usr/bin/python3.12 /usr/local/bin/python

WORKDIR /var/task

# ── Python dependencies (cached layer) ──────────────────────────────────────
# Install the exact locked dependency set from `uv.lock` (NOT the open-ended
# `pyproject.toml` floors) so a rebuilt image never floats above the
# known-good versions. `--frozen` fails rather than re-resolving if the lock
# is stale, and `--no-dev` keeps test/lint tooling out of the runtime image.
#
# Copy only the manifest + lockfile first; this layer is invalidated only when
# the dependencies change. A minimal package stub lets `uv sync` build the
# project without the full source tree, keeping this layer cacheable.
COPY pyproject.toml uv.lock README.md ./
RUN mkdir -p app && touch app/__init__.py \
    && uv sync --frozen --no-dev --python python3.12 \
    && rm -rf app

# Put the project virtualenv created by `uv sync` on PATH so `python`,
# `uvicorn`, and `playwright` resolve to the locked interpreter/entry points
# in both compute modes.
ENV VIRTUAL_ENV=/var/task/.venv
ENV PATH=/var/task/.venv/bin:$PATH

# ── Application code ─────────────────────────────────────────────────────────
COPY app/ ./app/
COPY prompts/ ./prompts/

# ── Runtime environment ──────────────────────────────────────────────────────
ENV PYTHONPATH=/var/task
# LWA listens on PORT and reverse-proxies to the app process; container mode
# also binds to this port directly.
ENV PORT=8080
# Exec wrapper path – Lambda runtime picks this up automatically.
ENV AWS_LAMBDA_EXEC_WRAPPER=/opt/extensions/lambda-adapter

EXPOSE 8080

# Default container-mode command; overridden per service in docker-compose /
# ECS task definition / Lambda function configuration.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
