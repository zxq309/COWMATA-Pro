"""Device credentials are DPAPI protected for the current Windows user and machine."""
import ctypes
import json
import os
import tempfile
from pathlib import Path
from ctypes import wintypes
from .client import AccessDenied

class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]

def protect(raw,*,decrypt=False):
    if os.name!='nt':raise AccessDenied('自动登录凭据目前仅支持 Windows DPAPI')
    source_buffer=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(source_buffer,ctypes.POINTER(ctypes.c_ubyte)));target=Blob()
    api=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if decrypt:
        fn=api.CryptUnprotectData;fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        okay=fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    else:
        fn=api.CryptProtectData;fn.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        okay=fn(ctypes.byref(source),'COWMATA device activation',None,None,None,1,ctypes.byref(target))
    if not okay:raise AccessDenied('此 Windows 用户无法解密设备授权，请重新激活')
    try:return ctypes.string_at(target.data,target.size)
    finally:kernel.LocalFree(target.data)

class DeviceStore:
    def __init__(self,product,path=None):
        local=Path.home()/'AppData/Local' if os.name=='nt' else Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))
        self.path=Path(path) if path else local/'COWMATA-Security'/product/'device.bin'
    def load(self):
        if not self.path.is_file():return None
        if self.path.stat().st_size>16384:raise AccessDenied('设备授权文件无效')
        return json.loads(protect(self.path.read_bytes(),decrypt=True))
    def save(self,value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        raw=protect(json.dumps(value).encode())
        descriptor,name=tempfile.mkstemp(dir=self.path.parent,prefix='.device-',suffix='.tmp')
        try:
            with os.fdopen(descriptor,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)
    def delete(self):self.path.unlink(missing_ok=True)
