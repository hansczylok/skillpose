FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913
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
