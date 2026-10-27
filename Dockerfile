FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt ".[web]" \
 && useradd --create-home --uid 10001 skillpose && mkdir /data && chown skillpose /data
USER skillpose
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["skillpose", "web", "--host", "0.0.0.0", "--port", "8000", "--db", "/data/skillpose.sqlite"]
