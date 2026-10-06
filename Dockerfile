# Production image: build the real wheel, install it, run it.
# No editable install, no dev dependencies, no silent pip fallback.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
COPY api ./api
COPY clinic_adapter ./clinic_adapter
COPY config ./config
COPY dialogue ./dialogue
COPY domain ./domain
COPY identity ./identity
COPY nlu ./nlu
COPY observability ./observability
COPY resilience ./resilience
COPY sessions ./sessions
COPY voice ./voice
COPY demo ./demo
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl uvicorn[standard] \
  && rm -rf /tmp/*.whl
EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
