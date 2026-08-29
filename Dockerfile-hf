FROM ghcr.io/realbestia1/easyproxy:latest

ENV PORT=7860
ENV ENABLE_WARP=true
ENV WARP_MODE=wireproxy

EXPOSE 7860

RUN sed -i '\|cd /app/flaresolverr|s|^|#|' /app/entrypoint.sh && \
    sed -i '\|Starting FlareSolverr|s|^|#|' /app/entrypoint.sh

CMD ["/bin/bash", "/app/entrypoint.sh"]
