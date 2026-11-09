# Your own ntfy server for EO1 live remotes

The live remote works through any ntfy server. The free public ntfy.sh has a daily message limit per home
network; your own server has none. This folder is a complete setup for a small Linux server that already runs
nginx with Let's Encrypt (certbot) and Docker.

| File | What it is |
| --- | --- |
| `server.yml` | ntfy's config: no web app, no sign-up, nothing open until you allow it |
| `eo1-ntfy-filter.py` | a small gate (Python standard library only): a publish gets through only when it is an EO1 message |
| `nginx-ntfy.conf` | nginx: only the paths an EO1 and the remote page use; every publish goes through the filter |

Messages are encrypted end to end between your EO1 and your phones, so the server cannot read them. The filter
can still tell them from anything else (an exact `{"v":1,"iv","ct","mac"}` shape), so plain notifications,
ntfy's e-mail, call and action headers and links never get in: the server is useless as a general push service.

## Set up

Replace `ntfy.example.com` with your own name in all three files, and point that name at the server (an A record).

```sh
sudo mkdir -p /opt/ntfy/etc /opt/ntfy/cache /opt/ntfy/lib /opt/ntfy/filter
sudo cp server.yml /opt/ntfy/etc/server.yml
sudo cp eo1-ntfy-filter.py /opt/ntfy/filter/
sudo docker run -d --name ntfy --restart unless-stopped -p 127.0.0.1:8097:80 \
  -v /opt/ntfy/etc:/etc/ntfy -v /opt/ntfy/cache:/var/cache/ntfy -v /opt/ntfy/lib:/var/lib/ntfy \
  binwiederhier/ntfy serve
sudo docker run -d --name ntfy-filter --restart unless-stopped --network host -u 65534:65534 --read-only \
  -v /opt/ntfy/filter:/app:ro python:3.12-alpine python3 /app/eo1-ntfy-filter.py 8098 http://127.0.0.1:8097
sudo cp nginx-ntfy.conf /etc/nginx/conf.d/ntfy.conf
sudo certbot --nginx -d ntfy.example.com
```

(certbot adds the certificate lines to the 443 block.) Then choose who gets in.

**Only your own EO1s and phones** (recommended for a private server):

```sh
sudo docker exec -e NTFY_PASSWORD="$(head -c 24 /dev/urandom | base64)" ntfy ntfy user add my-eo1
sudo docker exec ntfy ntfy access my-eo1 'eo1-*' rw
sudo docker exec ntfy ntfy token add my-eo1          # prints tk_...
```

On the EO1's control page at home: **Live from anywhere -> Your own ntfy server**, the address
(`https://ntfy.example.com`) and the token, **Use**, then open the new link on your phones once (it carries the token).
Several EO1s can share the token, or give each its own user.

**Every EO1** (a server for friends or the community; the filter still lets nothing else through):

```sh
sudo docker exec ntfy ntfy access everyone 'eo1-*' rw
```

## Check

```sh
curl -s -o /dev/null -w "%{http_code}\n" -d hello https://ntfy.example.com/eo1-test   # 400 or 403: plain text never gets in
curl -s http://127.0.0.1:8097/v1/health                                               # on the server: {"healthy":true}
```
