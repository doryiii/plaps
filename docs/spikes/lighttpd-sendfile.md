# Spike: X-Sendfile and Range behavior through lighttpd

* **Date:** 2026-10-30
* **Status:** Verified — negative result on X-Sendfile; design revised accordingly
* **Feeds:** `plaps/api/media.py`, `plaps/app.py`, `lighttpd/plaps-lighttpd.conf`

## Environment

* lighttpd **1.4.85** (ssl), Arch build; runtime modules in `/usr/lib/lighttpd/` (26 `.so` files — **no `mod_fastcgi.so` or `mod_scgi.so` on disk**).
* The system binary has `mod_fastcgi` **statically compiled in** (the loaded config lists it for php-fpm and parses cleanly — verified with `lighttpd -tt`).

## Result 1 — mod_proxy silently ignores X-Sendfile (blocker for the original design)

A stub HTTP backend behind `mod_proxy` responded `200` with an `X-Sendfile` header pointing at a real 2 MB file. The client received:

```
HTTP/1.1 200 OK
Server: BaseHTTP/0.6 Python/3.14.8      <- the backend's own header, passed through
Content-Length: 0                        <- the file was NOT sent
```

Zero bytes, status 200, no error anywhere. An app built assuming X-Sendfile would "work" and serve empty files on every download.

`strings` on `mod_proxy.so` shows no X-Sendfile support. The main `/usr/sbin/lighttpd` binary contains X-Sendfile/X-Sendfile2 (including `x-sendfile-docroot`) via the compiled-in `mod_fastcgi` — but reaching it would require the app to speak FastCGI, which no maintained Python WSGI server does (flup is unmaintained; rejected on simplicity grounds regardless).

**Design consequence:** media and originals stream through waitress via Werkzeug conditional `send_file`. At single-user LAN scale this costs milliseconds per request (wire time dominates); a 16-thread waitress pool keeps timeline requests responsive during large downloads.

## Result 2 — Range/206 passthrough works through mod_proxy

Backend returned `206 Partial Content` + `Content-Range: bytes 100-199/2097152` for a `Range: bytes=100-199` request; mod_proxy forwarded status, headers, and body untouched. Werkzeug's range handling therefore works end-to-end behind the proxy — this is now the load-bearing mechanism for the original-download viewer.

## Result 3 — target server restart is safe

`lighttpd -tt -f /etc/lighttpd/lighttpd.conf` parses cleanly (only failure: LetsEncrypt cert permission when run as an unprivileged user — not an issue for the real service). The `mod_fastcgi` module line resolves via the compiled-in implementation. No config repair needed before restart.

## Target lighttpd configuration for plaps

The existing server uses htdigest auth (`/etc/lighttpd/digestpasswd`, realm `General`) and subdomain-per-app vhosts. The plaps block follows that pattern; the app (waitress) listens on `127.0.0.1:8096`, static frontend files live in `/srv/http/plaps/`:

```lighttpd
# plaps — photo gallery
$HTTP["host"] == "photos.dory.moe" {
    server.document-root = "/srv/http/plaps"
    $HTTP["url"] =~ "^/(api|media|original)/" {
        proxy.server = ( "" => ( ( "host" => "127.0.0.1", "port" => 8096 ) ) )
    }
    auth.require = ( "" => (
        "method"  => "digest",
        "realm"   => "General",
        "require" => "valid-user"
    ) )
}
```

Notes:

* Auth terminates entirely at the proxy (digest, reusing the existing userfile and realm); the app ships no auth code, per the design. Basic and digest are interchangeable here — digest matches the server's existing setup.
* The `auth.require` with path `""` covers static files and proxied paths alike.
* Port 8096 is free (it appears only in a commented-out jellyfin block).
* The existing `:443` socket carries the `home.dory.moe` certificate; serving `photos.dory.moe` over TLS needs a certificate covering that name (or a SAN). LAN-only use works over plain HTTP immediately; TLS is required before any exposure past the LAN, per the design's security section.
