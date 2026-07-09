ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apk add --no-cache ca-certificates libffi openssl \
    && apk add --no-cache --virtual .build-deps build-base cargo libffi-dev openssl-dev \
    && pip3 install --no-cache-dir --prefer-binary -r /app/requirements.txt \
    && apk del .build-deps

COPY app /app/app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8099
CMD ["/run.sh"]
