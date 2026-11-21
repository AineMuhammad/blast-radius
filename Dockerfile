# Blast Radius entry: coverage-map test selection for sqlglot.
# Build: installs the labeling environment, runs sqlglot's suite once under a
# per-test tracer, and compiles a line -> tests map. Run: stdlib-only selector.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.lock /build/requirements.lock
RUN pip install -r /build/requirements.lock

# sqlglot at the pinned commit (GitHub archive of the commit)
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,sys,tarfile,urllib.request as u; \
d=u.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot

# one traced run of the whole suite (same flags we labeled with: SKIP_INTEGRATION=0)
COPY tools/br_trace.py /build/br_trace.py
RUN cd /opt/sqlglot \
 && (SKIP_INTEGRATION=0 BR_TRACE_OUT=/build/trace PYTHONPATH=/opt/sqlglot:/build \
     python -m pytest -p br_trace -p no:cacheprovider -n 4 -q tests > /build/trace.log 2>&1 || true) \
 && tail -n 3 /build/trace.log

COPY tools/build_map.py /build/build_map.py
RUN PYTHONPATH=/opt/sqlglot python /build/build_map.py /opt/sqlglot /build/trace /app/map \
 && chmod -R a+rX /app

COPY selector.py /app/selector.py
CMD ["python", "-S", "/app/selector.py"]
