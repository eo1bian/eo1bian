# Security

EO1bian runs on a device in your home. This is how it is protected, in plain words, and what an attacker could
and could not do.

## At home

- The **control page** answers only on your home network and needs the EO1's access code (in the QR codes and
  the link you saved). Without it, nothing can be changed.
- **SSH** is off until you connect a computer (Connect a computer, or a key on the USB stick), and then accepts
  only those keys, never passwords.
- The system is **read-only** while it runs: a power cut cannot damage it, and nothing written by a program
  survives a restart unless EO1bian saves it on purpose (settings, pictures).

## Live from anywhere (ntfy)

The live remote lets a phone see what the EO1 shows and send a few commands from anywhere, through an ntfy
server: by default ntfy.zap.pm, hosted free for EO1bian frames by speville (see below); the public ntfy.sh or
your own ntfy server work too, chosen on the control page.

- **Pairing:** the control page makes a random topic name and a random 256-bit key and gives them to your phone in
  a link (the key is after `#`, a part browsers never send to any server) or a QR code. Only your EO1 and the
  phones you paired have the key. **New key** makes a fresh one; phones with the old key stop working.
- **Encryption:** every message is encrypted (AES-256-CTR with a random IV) and then authenticated
  (HMAC-SHA256 over its direction, IV and cipher text), with keys derived from the shared key. The ntfy server
  only ever sees scrambled bytes: it cannot read the status or the pictures, and it cannot make or change a
  message the EO1 would accept.
- **A fixed list of commands:** `watch`, `next`, `prev`, `screenOn`, `screenOff`, `brightness` (1-100 %),
  `auto` (automatic brightness on or off), `albums` (a list of album names to play), `interval` (seconds per
  picture, or keep), `shuffle`, `titles`, `edges` (how pictures that do not fill the panel are framed),
  `show` (one picture from the picture browser), `sheet` (an album's small pictures) and `thumb` (send the
  small picture again). They go through the same
  local API as the control page. There is no command that runs code, downloads anything or
  changes the system. Anything else is ignored.
- **No replays:** each command carries a time stamp and a random nonce. The EO1 refuses commands older or newer
  than two minutes and any nonce it has already seen, so a recorded message cannot be sent again.
- **Software updates never come this way.** New EO1bian versions are installed only from releases signed with
  the project's update key (ed25519); see `eo1-update`.

**If the ntfy server were taken over,** the attacker could stop messages from arriving (the live remote stops
working until the server is back or you choose another) and could see metadata: IP addresses, times and message
sizes. They could not see what your EO1 shows, could not control it, and could not install anything.

### ntfy.zap.pm, the default server

ntfy.zap.pm is plain, unmodified open-source [ntfy](https://github.com/binwiederhier/ntfy) (the official
`binwiederhier/ntfy` Docker image) behind nginx with https. It is set up only to carry EO1 remotes, with exactly
the files in [ntfy/](ntfy/):

- Only topics that start with `eo1-` (the random topic names the control page makes) are open; every other topic
  is closed (`auth-default-access: deny-all`). No accounts, no sign-up, no web app, a blank front page.
- Every message goes through a small filter first (`ntfy/eo1-ntfy-filter.py`, Python standard library only): it
  gets through only when it is an encrypted EO1 message (`{"v":1,"iv","ct","mac"}`, nothing else, no extra
  ntfy headers such as e-mail, calls, actions or links). The server cannot read the messages, but it can tell
  them from anything else, so it is useless as a general push service.
- nginx lets through only what an EO1 and the remote page use: publishing to `/eo1-...`, the `/json` and `/sse`
  streams and `/file/` (an album's small pictures, encrypted like everything else). Everything else is a 404.
- Limits per network (IP address), far above what a frame does: a burst of 60 requests then one per 3 seconds,
  3000 messages a day, 10 open streams, 20 connections. A frame sends a status message only when the picture or
  a setting changes while a phone watches, and an album's pictures as one message (80 at most) however big the
  album is.
- fail2ban bans a network for 6 hours (longer when it comes back) after 30 refused requests in 10 minutes
  (not an EO1 message, closed path, over a limit). An EO1 and the remote page never get those answers.
- Messages are kept at most 1 hour and attachments at most 1 hour, then deleted (an EO1 refuses commands older
  than 2 minutes anyway).
- As with any server, it sees IP addresses, times, topic names and message sizes (in the nginx log), never what
  is inside a message.

You can run exactly the same on your own server ([ntfy/README.md](ntfy/README.md)) and pick it on the control page.

The checks are tested in `build/test_ntfyremote.py` (forged, altered, reflected, old and repeated messages are
refused; unknown commands do nothing).

## GitHub gallery and EO1 Remote

- The EO1 reads your gallery with a **read-only** deploy key or token; it cannot change your repository.
- The EO1 Remote page keeps your GitHub token **only in your phone's browser** and sends it only to GitHub. Give
  it a fine-grained token limited to your gallery repository.
- Pictures from GitHub are decoded as pictures only; nothing in the gallery is ever run.

## Tailscale (optional)

Off until you turn it on. Its state is kept on the gallery partition and it uploads no logs. Access is decided
by your own Tailscale account.

## Reporting a problem

Please report security problems privately to the maintainer through GitHub (Security > Report a vulnerability)
rather than in a public issue.
