# Cutover accelerator image. Built remotely by Cloud Build (see infra/terraform/modules/accelerator).
# checkov:skip=CKV_DOCKER_2:run-to-completion batch job (Cloud Run job); Cloud Run ignores Docker HEALTHCHECK and job success is the exit code
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "psycopg[binary]==3.2.*" \
    && useradd --create-home --uid 10001 cutover

WORKDIR /app
COPY cutover/ ./cutover/

USER cutover
ENTRYPOINT ["python", "-m", "cutover.simulate"]
CMD ["--target", "postgres", "--out", "/reports/latest"]
