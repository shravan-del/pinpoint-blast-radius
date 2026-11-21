# The scorer builds this on linux/amd64 and runs it with no network, read-only,
# as user `nobody`.  The digest is the image the commits were labeled in: its
# Python decides which sqlglot tests run, so it is kept.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Nothing is downloaded and nothing is installed: the maps were built offline
# (tools/build_maps.py, run in tools/lab/Dockerfile) and are committed, and the
# selector needs only the standard library.
COPY selector.py /app/selector.py
COPY pinpoint /app/pinpoint
COPY maps /app/maps

# The run is as `nobody`, so everything has to be world readable.  A file the
# build leaves at mode 600 would be unreadable and reject the submission.
# compileall first, then chmod: the disk is read-only at run time, so Python
# cannot write a .pyc then, and the __pycache__ this creates has to be readable
# by `nobody` too.
RUN python -m compileall -q /app/pinpoint /app/selector.py && chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
