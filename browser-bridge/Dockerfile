ARG FLARESOLVERR_IMAGE=flaresolverr-custom:3.5.0-chrome146
FROM ${FLARESOLVERR_IMAGE}

USER root
COPY browser-bridge/app.py /bridge/app.py
RUN chmod 0755 /bridge/app.py

ENV PORT=8192 \
    LOG_LEVEL=warning \
    HEADLESS=true

EXPOSE 8192
ENTRYPOINT ["python3", "/bridge/app.py"]

