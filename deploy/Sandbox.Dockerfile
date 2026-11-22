FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
COPY sandbox-entrypoint.sh /usr/local/bin/sandbox-entrypoint
RUN chmod 0755 /usr/local/bin/sandbox-entrypoint
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint"]
