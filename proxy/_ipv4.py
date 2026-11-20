"""Force IPv4 for all socket connections in this process.

The laptop's IPv6 path to some providers (OpenRouter/Cloudflare) completes the
TCP handshake but then stalls — the connection establishes (SYN-SENT -> EST)
but no data flows, so `requests` hangs until its read timeout (60s). IPv4 is
fast and reliable for every provider in use. This monkeypatch is run by the
`mneme` launcher before the proxy starts, so every provider call goes over IPv4.
"""
import socket as _socket

_orig = _socket.getaddrinfo


def getaddrinfo_v4(host, *args, **kwargs):
    try:
        return [a for a in _orig(host, *args, **kwargs) if a[0] == _socket.AF_INET]
    except Exception:
        return _orig(host, *args, **kwargs)


_socket.getaddrinfo = getaddrinfo_v4
