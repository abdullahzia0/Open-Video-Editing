FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY deploy/docker/requirements.lock /build/requirements.lock
RUN pip wheel --no-cache-dir --wheel-dir /wheels --require-hashes -r requirements.lock \
    && pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home ove \
    && mkdir /data && chown ove:ove /data
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels open-video-editing \
    && rm -rf /wheels
USER 10001:10001
WORKDIR /home/ove
ENV OVE_DATA_DIR=/data \
    OVE_ALLOWED_LOCAL_ROOTS='["/media"]' \
    OVE_OFFLINE=true \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["ove"]
CMD ["serve"]
