/* eo1-fade: cross-fade two raw frames (same size, 4 bytes per pixel, e.g. BGRx 1920x1080) and write the
 * intermediate frames to stdout at a fixed rate, for eo1-player's `fdsrc ! rawvideoparse ! kmssink` pipeline.
 *   eo1-fade from.raw to.raw frames fps
 * Writes `frames` blended frames (the last one equals to.raw). Pillow needed ~0.3 s per 1080p frame on the
 * EO1's Cortex-A9; this loop is auto-vectorised (NEON) by gcc -O3 and runs in ~20 ms.
 * Built static in the armhf build container: gcc -O3 -mfpu=neon -static -o eo1-fade eo1-fade.c
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static uint8_t *load(const char *path, size_t *n)
{
	FILE *f = fopen(path, "rb");
	if (!f) {
		perror(path);
		exit(1);
	}
	fseek(f, 0, SEEK_END);
	*n = (size_t)ftell(f);
	fseek(f, 0, SEEK_SET);
	uint8_t *b = malloc(*n);
	if (!b || fread(b, 1, *n, f) != *n) {
		fprintf(stderr, "read %s failed\n", path);
		exit(1);
	}
	fclose(f);
	return b;
}

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

static void put(const uint8_t *b, size_t n)
{
	while (n) {
		ssize_t w = write(1, b, n);
		if (w <= 0) {
			exit(0);	/* the pipeline went away: nothing to do */
		}
		b += w;
		n -= (size_t)w;
	}
}

int main(int argc, char **argv)
{
	if (argc != 5) {
		fprintf(stderr, "usage: %s from.raw to.raw frames fps\n", argv[0]);
		return 2;
	}
	size_t na, nb;
	uint8_t *a = load(argv[1], &na), *b = load(argv[2], &nb);
	if (na != nb) {
		fprintf(stderr, "size mismatch %zu vs %zu\n", na, nb);
		return 1;
	}
	int frames = atoi(argv[3]), fps = atoi(argv[4]);
	if (frames < 1) frames = 1;
	if (fps < 1) fps = 15;
	uint8_t *o = malloc(na);
	double t0 = now();
	for (int f = 1; f <= frames; f++) {
		uint32_t k = (uint32_t)(256 * f / frames);	/* 0..256 */
		uint32_t ik = 256 - k;
		for (size_t i = 0; i < na; i++) {
			o[i] = (uint8_t)((a[i] * ik + b[i] * k) >> 8);
		}
		put(o, na);
		double wait = t0 + (double)f / fps - now();
		if (wait > 0) {
			usleep((useconds_t)(wait * 1e6));
		}
	}
	return 0;
}
