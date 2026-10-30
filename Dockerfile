# python:3.14-slim, pinned by digest (Dependabot docker updates it)
FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

WORKDIR /app

# Dependencies are installed from hash-locked files (requirements/relock.sh regenerates them). The shared blocks (agentpg-kit)
# are in the lock too, pinned to a commit-addressed archive by sha256. Nothing is resolved at build time.
COPY requirements/build.txt requirements/runtime.txt ./requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/build.txt \
 && pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r requirements/runtime.txt

# Then the app itself (its dependencies are already installed).

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1
# psycopg-binary bundles its own OpenSSL: point it at the system CA bundle so sslrootcert=system works.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN useradd --system --no-create-home app
USER app
EXPOSE 8000
HEALTHCHECK --start-period=30s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:'+os.environ['PORT']+'/healthz')"
CMD ["paybroker", "start"]
