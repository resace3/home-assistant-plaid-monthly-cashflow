ARG BUILD_ARCH=aarch64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-python:3.13-alpine3.24

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apk add --no-cache ca-certificates libffi openssl \
    && apk add --no-cache --virtual .build-deps build-base cargo libffi-dev openssl-dev \
    && pip3 install --no-cache-dir --prefer-binary -r /app/requirements.txt \
    && apk del .build-deps

COPY app /app/app
RUN cp /app/app/static/app.js /app/app/static/app-0.1.8.js \
    && cp /app/app/static/styles.css /app/app/static/styles-0.1.8.css
COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8099
CMD ["/run.sh"]
