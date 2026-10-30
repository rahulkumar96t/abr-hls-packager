SAMPLE := samples/testsrc_1080p.mp4
OUT := out/demo

.PHONY: install sample demo serve test lint clean

install:
	uv sync --locked

# 30 s synthetic 1080p30 source: FFmpeg's testsrc2 pattern plus a 440 Hz tone.
sample: $(SAMPLE)
$(SAMPLE):
	@mkdir -p $(dir $@)
	ffmpeg -v error -y -f lavfi -i testsrc2=size=1920x1080:rate=30 \
		-f lavfi -i "sine=frequency=440:sample_rate=48000,volume=0.5" -t 30 \
		-c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k $@

demo: $(SAMPLE)
	uv run abr-pack package $(SAMPLE) -o $(OUT) --loudnorm --overwrite --validate

# Serve the output for a quick look in Safari or an hls.js page: http://localhost:8000/master.m3u8
serve:
	cd $(OUT) && uv run python -m http.server 8000

test:
	uv run pytest -q

lint:
	uv run ruff format --check .
	uv run ruff check .

clean:
	rm -rf out samples .pytest_cache .ruff_cache
