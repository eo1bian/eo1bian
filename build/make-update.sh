#!/bin/bash
# Build a signed EO1bian update for a GitHub release: out/release/eo1bian-update.tar
#   bash debian/build/make-update.sh v1.0.1           runtime files only (patches/10-eo1.tar.gz)
#   bash debian/build/make-update.sh v1.0.1 --boot    plus uInitrd, eo1.dtb, zImage and 6x_bootscript from out/
# The tar holds manifest.json (version, sha256 of every file), manifest.sig (ed25519 by the update key) and the
# files. EO1s verify it with /usr/share/eo1/update-key.pem (eo1-update). The private key never goes into git:
# KEY=~/eo1bian-update-key.pem by default; keep a backup, without it no further update can be signed.
# Attach eo1bian-update.tar to a release tagged with the same version (e.g. v1.0.1 -> version "v1.0.1").
set -euo pipefail
VERSION="${1:?version, e.g. v1.0.1}"
BOOT="${2:-}"
KEY="${KEY:-$HOME/eo1bian-update-key.pem}"
[ -f "$KEY" ] || { echo "no update key at $KEY"; exit 1; }
DEB="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$DEB/out/release"
rm -rf "$OUT"; mkdir -p "$OUT/patches"
cd "$DEB/overlay"
. ../build/patch-files.sh
for f in $FILES; do [ -f "$f" ] || { echo "missing: $f"; exit 1; }; done
# the runtime patch, assembled exactly like push-patch.sh does on the EO1 (owners, modes, systemd links)
# shellcheck disable=SC2086
tar -cf - $FILES -C ../build patch-assemble.sh | MSYS_NO_PATHCONV=1 docker run --rm -i -v "$OUT:/o" debian:bookworm bash -c "
	set -e; mkdir /p; tar -xf - -C /p; cd /p
	echo '$VERSION' > etc/eo1bian-version
	bash /p/patch-assemble.sh /o/patches/10-eo1.tar.gz"
NAMES="patches/10-eo1.tar.gz"
if [ "$BOOT" = "--boot" ]; then
	for f in uInitrd eo1.dtb zImage 6x_bootscript; do
		[ -f "$DEB/out/$f" ] || { echo "missing: out/$f"; exit 1; }
		cp -f "$DEB/out/$f" "$OUT/$f"; NAMES="$NAMES $f"
	done
fi
cd "$OUT"
{
	printf '{\n  "version": "%s",\n  "files": {\n' "$VERSION"
	first=1
	for n in $NAMES; do
		[ $first = 1 ] || printf ',\n'
		printf '    "%s": "%s"' "$n" "$(sha256sum "$n" | cut -d" " -f1)"; first=0
	done
	printf '\n  }\n}\n'
} > manifest.json
openssl pkeyutl -sign -inkey "$KEY" -rawin -in manifest.json -out manifest.sig
openssl pkeyutl -verify -pubin -inkey "$DEB/overlay/usr/share/eo1/update-key.pem" -rawin -in manifest.json -sigfile manifest.sig
# shellcheck disable=SC2086
tar -cf eo1bian-update.tar manifest.json manifest.sig $NAMES
ls -la eo1bian-update.tar
cat manifest.json
