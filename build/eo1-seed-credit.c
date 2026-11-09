/* eo1-seed-credit: feed a saved random seed to the kernel RNG *with credit* (RNDADDENTROPY), so the entropy
 * pool is initialised at once in the initramfs instead of ~2 s later (systemd-random-seed waited for it).
 * The seed (512 bytes, state/random-seed on the boot partition) is refreshed at every boot by eo1-seed.service.
 * Static binary for the initramfs; built in the armhf build container by finalize.sh.
 *   eo1-seed-credit /path/to/seed
 */
#include <fcntl.h>
#include <linux/random.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	unsigned char buf[sizeof(struct rand_pool_info) + 512];
	struct rand_pool_info *rpi = (struct rand_pool_info *)buf;
	int fd, rfd, n;

	if (argc != 2) {
		fprintf(stderr, "usage: %s seedfile\n", argv[0]);
		return 2;
	}
	fd = open(argv[1], O_RDONLY);
	if (fd < 0) {
		perror(argv[1]);
		return 1;
	}
	n = read(fd, rpi->buf, 512);
	close(fd);
	if (n < 64) {
		fprintf(stderr, "seed too short (%d bytes)\n", n);
		return 1;
	}
	rpi->entropy_count = n * 8;
	rpi->buf_size = n;
	rfd = open("/dev/urandom", O_WRONLY);
	if (rfd < 0) {
		perror("/dev/urandom");
		return 1;
	}
	if (ioctl(rfd, RNDADDENTROPY, rpi) < 0) {
		perror("RNDADDENTROPY");
		return 1;
	}
	close(rfd);
	printf("credited %d bytes of entropy\n", n);
	return 0;
}
