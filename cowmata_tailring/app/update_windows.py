"""Windows TLS transport with system proxy discovery and verified redirects.

Uses WinHTTP/Schannel when Python's OpenSSL connection is interrupted.
Certificate checks remain enabled; only the updater's allowlisted URLs are used.
"""
from __future__ import annotations

import ctypes
import email.parser
import urllib.error
import urllib.parse
from ctypes import wintypes as w


def _api():
    api = ctypes.WinDLL('winhttp', use_last_error=True)
    handle = w.HANDLE
    signatures = {
        'WinHttpOpen': (handle, [w.LPCWSTR,w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD]),
        'WinHttpConnect': (handle, [handle,w.LPCWSTR,w.WORD,w.DWORD]),
        'WinHttpOpenRequest': (handle,[handle,w.LPCWSTR,w.LPCWSTR,w.LPCWSTR,w.LPCWSTR,ctypes.c_void_p,w.DWORD]),
        'WinHttpSetOption': (w.BOOL,[handle,w.DWORD,ctypes.c_void_p,w.DWORD]),
        'WinHttpSetTimeouts': (w.BOOL,[handle,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int]),
        'WinHttpSendRequest': (w.BOOL,[handle,w.LPCWSTR,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.c_size_t]),
        'WinHttpReceiveResponse': (w.BOOL,[handle,ctypes.c_void_p]),
        'WinHttpQueryHeaders': (w.BOOL,[handle,w.DWORD,w.LPCWSTR,ctypes.c_void_p,ctypes.POINTER(w.DWORD),ctypes.c_void_p]),
        'WinHttpReadData': (w.BOOL,[handle,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]),
        'WinHttpCloseHandle': (w.BOOL,[handle]),
    }
    for name,(restype,args) in signatures.items():
        func=getattr(api,name)
        func.restype,func.argtypes=restype,args
    return api


def _checked(result):
    if not result:
        raise urllib.error.URLError(ctypes.WinError(ctypes.get_last_error()))
    return result


class Response:
    def __init__(self,url,headers):
        self.api=_api()
        self.handles=[]
        self.request=None
        try:
            session=_checked(self.api.WinHttpOpen('COWMATA-Updater',4,None,None,0))
            self.handles.append(session)
            _checked(self.api.WinHttpSetTimeouts(session,10000,15000,15000,30000))
            # TLS 1.2 and 1.3 only. Windows 10 builds without TLS 1.3 use 1.2.
            protocols=w.DWORD(0x800 | 0x2000)
            if not self.api.WinHttpSetOption(session,84,ctypes.byref(protocols),4):
                protocols=w.DWORD(0x800)
                _checked(self.api.WinHttpSetOption(session,84,ctypes.byref(protocols),4))
            split=urllib.parse.urlsplit(url)
            connection=_checked(self.api.WinHttpConnect(session,split.hostname,443,0))
            self.handles.append(connection)
            target=urllib.parse.urlunsplit(('', '', split.path or '/',split.query,''))
            request=_checked(self.api.WinHttpOpenRequest(connection,'GET',target,None,None,None,0x800000))
            self.handles.append(request)
            self.request=request
            # Disable automatic redirects and cookies. Validate each redirect ourselves.
            disabled=w.DWORD(2 | 1)
            _checked(self.api.WinHttpSetOption(request,63,ctypes.byref(disabled),4))
            auth=w.DWORD(2)  # Never send the user's Windows login to an update endpoint.
            _checked(self.api.WinHttpSetOption(request,77,ctypes.byref(auth),4))
            raw=''.join(str(k)+': '+str(v)+'\r\n' for k,v in headers.items())
            _checked(self.api.WinHttpSendRequest(request,raw,len(raw),None,0,0,0))
            _checked(self.api.WinHttpReceiveResponse(request,None))
            status=w.DWORD()
            size=w.DWORD(4)
            _checked(self.api.WinHttpQueryHeaders(request,19 | 0x20000000,None,ctypes.byref(status),ctypes.byref(size),None))
            self.status=status.value
            size=w.DWORD()
            self.api.WinHttpQueryHeaders(request,22,None,None,ctypes.byref(size),None)
            if not 0 < size.value <= 128*1024:
                raise ValueError('Invalid Windows update response headers')
            buffer=ctypes.create_unicode_buffer(size.value//2+1)
            _checked(self.api.WinHttpQueryHeaders(request,22,None,buffer,ctypes.byref(size),None))
            self.headers=email.parser.Parser().parsestr(buffer.value.partition('\r\n')[2])
        except BaseException:
            self.close()
            raise

    def read(self,size=-1):
        blocks=[]
        remaining=size
        while remaining != 0:
            count=min(65536,remaining) if remaining>0 else 65536
            buffer=ctypes.create_string_buffer(count)
            used=w.DWORD()
            _checked(self.api.WinHttpReadData(self.request,buffer,count,ctypes.byref(used)))
            if not used.value:
                break
            blocks.append(buffer.raw[:used.value])
            if remaining>0:
                remaining-=used.value
        return b''.join(blocks)

    def close(self):
        for handle in reversed(self.handles):
            self.api.WinHttpCloseHandle(handle)
        self.handles=[]

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()


def open_windows(url,headers,validate):
    validate(url)
    for _attempt in range(6):
        response=Response(url,headers)
        if response.status in {301,302,303,307,308}:
            target=urllib.parse.urljoin(url,response.headers.get('Location',''))
            response.close()
            validate(target,redirect=True)
            url=target
            continue
        if response.status>=400:
            status,values=response.status,response.headers
            response.close()
            raise urllib.error.HTTPError(url,status,'Windows update transport',values,None)
        return response
    raise ValueError('Too many update redirects')
