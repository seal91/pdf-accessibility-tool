# Container for running the browser version on a server (this is also what an internal server / Azure would run).
#   docker build -t pdfa11y .
#   docker run -p 8765:8765 -e PDFA11Y_PASSWORD=choose-something -v pdfa11y-data:/data pdfa11y
# It refuses to listen on the network without a password. Put it behind HTTPS (a reverse proxy or the platform's ingress).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PDFA11Y_TOOLS=/opt/pdfa11y/tools

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/install_tools.sh deploy/install_tools.sh
RUN bash deploy/install_tools.sh

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
ENV PDFA11Y_DATA=/data
EXPOSE 8765
CMD ["sh", "-c", "python -m pdfa11y web --host 0.0.0.0 --port 8765 --data-dir /data --retention-days ${RETENTION_DAYS:-30}"]
