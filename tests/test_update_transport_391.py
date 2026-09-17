import io
import ssl
import urllib.error

import pytest

from cowmata_tailring.app import update_core as core


class Response(io.BytesIO):
    status = 200
    headers = {}


def test_metadata_retries_tls_eof_before_returning_release_list():
    attempts = []
    def open_response(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.URLError(ssl.SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING'))
        return Response(b'[]')
    assert core.read_bytes(core.API+'/releases',100,open_response) == b'[]'
    assert len(attempts) == 2


def test_metadata_retries_eof_while_reading():
    class Broken(Response):
        def read(self,*args):
            raise ssl.SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING')
    replies = iter([Broken(),Response(b'[]')])
    assert core.read_bytes(core.API+'/releases',100,lambda _:next(replies)) == b'[]'


def test_certificate_errors_are_not_retried():
    calls=[]
    def invalid(url):
        calls.append(url)
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1,'certificate mismatch'))
    with pytest.raises(urllib.error.URLError):
        core.read_bytes(core.API+'/releases',100,invalid)
    assert len(calls)==1


def test_tls_eof_switches_to_system_transport(monkeypatch):
    class Broken:
        def open(self,*args,**kwargs):
            raise urllib.error.URLError(ssl.SSLEOFError(8,'unexpected EOF'))
    monkeypatch.setattr(core.urllib.request,'build_opener',lambda *_:Broken())
    monkeypatch.setattr(core.os,'name','nt')
    monkeypatch.setattr(core,'_native_open',lambda url,headers:Response(b'official metadata'))
    with core.open_url(core.API+'/releases') as response:
        assert response.read() == b'official metadata'


def test_native_transport_refuses_untrusted_redirect(monkeypatch):
    from cowmata_tailring.app import update_windows
    class Redirect(Response):
        status=302
        headers={'Location':'https://example.com/untrusted.zip'}
    monkeypatch.setattr(update_windows,'Response',lambda *_:Redirect())
    with pytest.raises(ValueError,match='outside'):
        update_windows.open_windows(core.PAGE+'/download/v3.9.0/package.zip',{},core.valid_url)


def test_native_transport_preserves_range_and_response_bytes(monkeypatch):
    from cowmata_tailring.app import update_windows
    class Data(Response):
        status=206
        headers={'Content-Range':'bytes 2-4/5'}
    def response(url,headers):
        assert headers['Range']=='bytes=2-'
        return Data(b'345')
    monkeypatch.setattr(update_windows,'Response',response)
    with update_windows.open_windows(core.PAGE+'/download/v3.9.0/package.zip',{'Range':'bytes=2-'},core.valid_url) as reply:
        assert reply.status == 206
        assert reply.headers['Content-Range'] == 'bytes 2-4/5'
        assert reply.read() == b'345'
