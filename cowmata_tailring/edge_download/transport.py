"""Host-scoped CA supplements from the desktop downloader; strict TLS remains on."""
import hashlib
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener

PINS = {
    'letsencrypt-yr2.pem': '238b85a0099c65b970477d5724f1a1d475ce5058cffe4efa8733899bdb863c47',
    'letsencrypt-root-yr-by-x1.pem': '072639d0b140d5bffae16ad9c3f6cc6086040621f51ee61a6d46a8915c07cf76',
    'isrg-root-x1.pem': '96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6',
}


def tls_context(url):
    context = ssl.create_default_context()
    if urlsplit(url).hostname == 'data.cowmata.com':
        certificates = []
        for name, expected in PINS.items():
            path = Path(__file__).with_name('certificates') / name
            pem = path.read_text(encoding='ascii')
            if hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() != expected:
                raise ValueError('Supplemental TLS certificate fingerprint mismatch')
            info = ssl._ssl._test_decode_cert(str(path))
            if not ssl.cert_time_to_seconds(info['notBefore']) <= time.time() <= ssl.cert_time_to_seconds(info['notAfter']):
                raise ValueError('Supplemental TLS certificate is outside its validity period')
            certificates.append(pem)
        context.load_verify_locations(cadata='\n'.join(certificates))
    return context


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme not in {'http', 'https'} or new.username or new.password:
            raise ValueError('Unsafe download redirect')
        if old.scheme == 'https' and new.scheme != 'https':
            raise ValueError('HTTPS downgrade redirect rejected')
        if old.hostname == 'data.cowmata.com' and (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise ValueError('External data redirect must keep the same origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout=30):
    handlers = [CheckedRedirect()]
    if urlsplit(request.full_url).scheme == 'https':
        handlers.append(HTTPSHandler(context=tls_context(request.full_url)))
    return build_opener(*handlers).open(request, timeout=timeout)
