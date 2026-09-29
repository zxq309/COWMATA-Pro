"""Signed GitHub channel for the ledger product; no upload credentials leave the app."""
import hashlib,json,os,re,subprocess,tempfile,urllib.parse,urllib.request
from pathlib import Path
REPO="zxq309/COWMATA-Pro"
CHANNEL="https://github.com/"+REPO+"/releases/download/ledger-latest/cowmata-ledger-update.json"
def version_key(value):
    if not isinstance(value,str) or not re.fullmatch(r"\d+\.\d+\.\d+",value):raise ValueError("更新版本格式无效")
    return tuple(map(int,value.split(".")))
def valid_url(url,redirect=False):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!="https" or parsed.username or parsed.password or parsed.port not in (None,443):raise ValueError("更新地址必须使用受信任HTTPS")
    valid=parsed.hostname=="github.com" and parsed.path.startswith("/"+REPO+"/releases/download/")
    if not valid and not (redirect and parsed.hostname in ("release-assets.githubusercontent.com","objects.githubusercontent.com","github-releases.githubusercontent.com")):raise ValueError("更新地址不属于指定GitHub仓库")
    return url
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        valid_url(newurl,True);return super().redirect_request(req,fp,code,msg,headers,newurl)
def open_url(url):
    valid_url(url)
    return urllib.request.build_opener(Redirect()).open(urllib.request.Request(url,headers={"User-Agent":"COWMATA-Ledger-Updater","Cache-Control":"no-cache"}),timeout=15)
def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):h.update(block)
    return h.hexdigest()
def validate_manifest(document):
    if document.get("schema")!=1 or document.get("product")!="cowmata-ledger":raise ValueError("更新包产品不匹配")
    version=document.get("version");version_key(version)
    name="COWMATA-Ledger-"+version+"-Setup.exe"
    if document.get("name")!=name or document.get("url")!="https://github.com/"+REPO+"/releases/download/ledger-v"+version+"/"+name:raise ValueError("更新包名称或发布地址不匹配")
    if not isinstance(document.get("size"),int) or not 0<document["size"]<=1024**3:raise ValueError("更新包大小无效")
    if not re.fullmatch("[0-9a-f]{64}",document.get("sha256","")):raise ValueError("更新校验码无效")
    return document
def check_update(root,current,cache,opener=open_url):
    root=Path(root);cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    with opener(CHANNEL) as response:content=response.read(65537)
    if len(content)>65536:raise ValueError("更新清单过大")
    with tempfile.NamedTemporaryFile(dir=cache,suffix=".json",delete=False) as f:f.write(content);signed=Path(f.name)
    try:
        result=subprocess.run([str(root/"LedgerVerify.exe"),str(signed)],capture_output=True,timeout=10,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode:raise ValueError("更新清单签名校验未通过")
        manifest=validate_manifest(json.loads(result.stdout))
    finally:signed.unlink(missing_ok=True)
    return manifest if version_key(manifest["version"])>version_key(current) else None
def download(update,cache,progress=lambda *_:None,cancelled=lambda:False,opener=open_url):
    validate_manifest(update);cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    destination=cache/update["name"];partial=cache/(update["name"]+".part")
    if destination.is_symlink() or partial.is_symlink():raise ValueError("更新缓存路径异常")
    if destination.exists() and destination.stat().st_size==update["size"] and digest(destination)==update["sha256"]:return destination
    total=0
    try:
        with opener(update["url"]) as response,partial.open("wb") as stream:
            while True:
                if cancelled():raise InterruptedError("已暂停下载")
                block=response.read(1024*1024)
                if not block:break
                total+=len(block)
                if total>update["size"]:raise ValueError("更新包超出声明大小")
                stream.write(block);progress(total,update["size"])
            stream.flush();os.fsync(stream.fileno())
        if total!=update["size"] or digest(partial)!=update["sha256"]:raise ValueError("更新包完整性校验失败")
        partial.replace(destination);return destination
    except Exception:
        partial.unlink(missing_ok=True);raise
