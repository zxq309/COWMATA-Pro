"""Import the owner's restricted SSH profile on a clean Windows device."""
import json,os,subprocess,tempfile
from pathlib import Path

def profile_path():
    legacy=Path.home()/'AppData/Local/COWMATA-Security/transport/upload_key'
    return legacy if legacy.is_file() else provision_connection()

def provision_connection():
    # This newly generated, deliberately distributable identity only opens the
    # forced JSON receiver. The server still requires account/password/code or
    # an active device session for every business request. It grants no shell,
    # file-system access, forwarding or account-administration privileges.
    bundled=Path(__file__).with_name('connection_key')
    if not bundled.is_file():raise ValueError('连接组件不完整，请重新安装最新版')
    raw=bundled.read_bytes()
    destination=Path.home()/'AppData/Local/COWMATA-Security/transport/connection_key'
    if destination.is_file() and destination.read_bytes()==raw:return destination
    destination.parent.mkdir(parents=True,exist_ok=True);atomic_write(destination,raw)
    if os.name=='nt':
        import csv,io,re
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        who=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,check=True,creationflags=flags,timeout=10)
        sid=next(csv.reader(io.StringIO(who.stdout.decode('utf-8','replace'))))[1]
        if not re.fullmatch(r'S-1-[0-9-]+',sid):raise ValueError('无法保护本机连接组件')
        result=subprocess.run(['icacls',str(destination),'/inheritance:r','/grant:r','*'+sid+':F','*S-1-5-18:F'],capture_output=True,creationflags=flags,timeout=10)
        if result.returncode:raise ValueError('无法保存本机连接组件，请检查用户目录权限')
    return destination

def atomic_write(destination,raw):
    descriptor,name=tempfile.mkstemp(dir=destination.parent,prefix='.profile-')
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,destination)
    finally:
        if os.path.exists(name):os.unlink(name)

def import_authorization(path,root,config):
    path=Path(path)
    if path.stat().st_size>65536:raise ValueError("授权文件过大")
    data=json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("product")!="cowmata-ledger-authorization" or data.get("server")!=config['host'] or data.get("port")!=int(config['port']):raise ValueError("授权文件不属于此上传服务器")
    private=data.get("private_key","")
    if not isinstance(private,str) or not private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):raise ValueError("授权文件格式无效")
    destination=Path.home()/'AppData/Local/COWMATA-Security/transport/upload_key';destination.parent.mkdir(parents=True,exist_ok=True)
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
