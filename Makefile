IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# --- Pinpoint's own targets -------------------------------------------------

.PHONY: unit lint local cpu

# Our unit tests.  They need nothing but the standard library.
unit:
	python3 -m unittest discover -s tests -t . -q

lint:
	ruff check . --exclude vendor

# The selector run straight on the host against the sample batch: the same
# numbers as `make sample` without a Docker build, for fast iteration.
local:
	BLAST_RADIUS_DATA=$(SAMPLE) python3 selector.py > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# What the board will add to our simulated seconds: user+sys of every process
# in the container, measured under the scorer's own limits.
cpu: build
	docker run $(RUN_FLAGS) --entrypoint sh $(IMAGE) -c \
		'for i in 1 2 3; do python /app/selector.py > /dev/null; done; times' 2>&1 | tail -4
