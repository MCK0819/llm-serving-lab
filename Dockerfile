FROM python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LLM_LAB_UPLOAD_ROOT=/var/lib/llm-lab/uploads
WORKDIR /srv/llm-serving-lab
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && useradd --create-home --uid 10001 appuser \
    && install -d -o appuser -g appuser /var/lib/llm-lab/uploads
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser migrations ./migrations
USER appuser
EXPOSE 8000
CMD ["python", "-m", "app.server"]
