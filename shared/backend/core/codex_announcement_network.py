"""Safe transport diagnostics. Never retain URLs, credentials or response bodies."""
from email.utils import parsedate_to_datetime
import socket
import ssl
import urllib.error


def failure_reason(exc):
    cause=exc.reason if isinstance(exc,urllib.error.URLError) else exc
    if isinstance(cause,ssl.SSLCertVerificationError):return 'tls_certificate'
    if isinstance(cause,ssl.SSLError):return 'tls_handshake'
    if isinstance(cause,socket.gaierror):return 'dns'
    if isinstance(cause,(TimeoutError,socket.timeout)):return 'timeout'
    if isinstance(cause,(ConnectionError,OSError)):return 'connection'
    if isinstance(cause,UnicodeError):return 'decode'
    return 'unknown_network'


def retry_seconds(raw,now,cap=7*86400):
    try:wait=float(raw)
    except (TypeError,ValueError):
        try:wait=parsedate_to_datetime(raw).timestamp()-now
        except (TypeError,ValueError,OverflowError):return 0
    import math
    return min(cap,max(0,wait)) if math.isfinite(wait) else 0


def summary(exc):
    # Error class + errno is sufficient to distinguish unknown failures without
    # leaking proxy credentials or arbitrary exception response text.
    cause=getattr(exc,'reason',exc)
    name=type(cause).__name__
    code=getattr(cause,'errno',None)
    return (name+(f' errno={code}' if isinstance(code,int) else ''))[:160]
