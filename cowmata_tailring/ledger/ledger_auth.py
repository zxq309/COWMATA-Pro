"""Upload authorization is kept outside public packages and survives upgrades."""
import json,os,shutil,subprocess,tempfile
from pathlib import Path
from .ledger_core import DEFAULTS,atomic_write
def auth_directory(root):
    return Path(os.environ.get("COWMATA_LEDGER_AUTH",str(root)))
def key_path(root):
    original=auth_directory(root)/'upload_key'
    from cowmata_security.profile import profile_path
    return original if original.is_file() else profile_path()
def authorized(root):return key_path(root).is_file()
def import_authorization(path,root):
    path=Path(path)
    if path.stat().st_size>65536:raise ValueError("授权文件过大")
    data=json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("product")!="cowmata-ledger-authorization" or data.get("server")!=DEFAULTS["host"] or data.get("port")!=DEFAULTS["port"]:raise ValueError("授权文件不属于此上传服务器")
    private=data.get("private_key","")
    if not isinstance(private,str) or not private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):raise ValueError("授权文件格式无效")
    destination=key_path(root);destination.parent.mkdir(parents=True,exist_ok=True)
    import base64,struct
    try:
        raw=base64.b64decode("".join(private.splitlines()[1:-1]),validate=True)
        magic=b"openssh-key-v1\0"
        if not raw.startswith(magic):raise ValueError()
        position=len(magic)
        def read_string():
            nonlocal position
            length=struct.unpack_from(">I",raw,position)[0];position+=4
            if position+length>len(raw):raise ValueError()
            value=raw[position:position+length];position+=length;return value
        cipher=read_string();kdf=read_string();read_string()
        count=struct.unpack_from(">I",raw,position)[0];position+=4
        if cipher!=b"none" or kdf!=b"none" or count!=1:raise ValueError()
        public_blob=read_string()
        expected=base64.b64decode((Path(root)/"upload_key.pub").read_text(encoding="ascii").split()[1],validate=True)
        if public_blob!=expected:raise ValueError()
        private_blob=read_string()
        if len(private_blob)<16 or private_blob[:4]!=private_blob[4:8]:raise ValueError()
    except (ValueError,IndexError,struct.error):
        raise ValueError("授权私钥格式无效，或未在指定服务器登记") from None
    atomic_write(destination,private.encode("ascii"))
    if os.name=="nt":
        import ctypes
        # The enclosing installation is per-user; remove inherited broad ACLs on the key.
        name=os.environ.get("USERNAME","")
        if name:
            subprocess.run(["icacls",str(destination),"/inheritance:r","/grant:r",name+":F","SYSTEM:F"],capture_output=True,timeout=10,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return destination
