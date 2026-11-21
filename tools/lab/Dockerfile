# Local lab image: reproduces the labeling environment described in the kit README
# (same base image digest, requirements.lock, sqlglot checkout on PYTHONPATH,
# pure-Python sqlglot - no sqlglotc build). Used offline to build maps and to
# label locally generated mutants. This image is NOT the submission.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# coverage is not in requirements.lock because the scorer's environment does not
# need it.  It is a lab-only tool: the maps it produces are committed, so the
# submission image never installs or runs it.
RUN pip install --no-cache-dir 'coverage==7.12.0'

RUN git init /src/sqlglot \
    && cd /src/sqlglot \
    && git remote add origin https://github.com/tobymao/sqlglot.git \
    && git fetch --depth 1 origin ${SQLGLOT_COMMIT} \
    && git checkout FETCH_HEAD \
    && rm -rf .git

ENV PYTHONPATH=/src/sqlglot
ENV SKIP_INTEGRATION=0
WORKDIR /src/sqlglot
CMD ["bash"]
