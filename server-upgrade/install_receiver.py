"""Install/update the v2 receiver for the existing cowmata_upload public key."""
import argparse,ctypes,json,os,re,shutil,subprocess,sys
from datetime import datetime
from pathlib import Path
SOURCE=Path(__file__).resolve().parent
def install(target,service,authfile,public_key,check=False):
    if os.name!="nt" or not ctypes.windll.shell32.IsUserAnAdmin():raise RuntimeError("请在服务器上以管理员身份运行")
    target=Path(target);service=Path(service);authfile=Path(authfile)
    if not target.is_absolute() or target.suffix.lower()!=".csv" or not Path(target.anchor).exists():raise ValueError("服务器CSV绝对路径或磁盘无效")
    if not service.is_absolute() or service==Path(service.anchor):raise ValueError("接收程序目录无效")
    key=Path(public_key).read_text("utf8").strip().split()
    if len(key)<2:raise ValueError("公钥格式无效")
    original=authfile.read_text("utf8")
    lines=original.splitlines()
    indices=[i for i,line in enumerate(lines) if key[1] in line]
    if len(indices)!=1:raise ValueError("authorized_keys中没有唯一匹配的上传公钥")
    i=indices[0];match=re.search(r'command="([^"\r\n]+)"',lines[i])
    if not match or not lines[i].startswith("restrict,"):raise ValueError("公钥必须已设置restrict和command限制，未改变配置")
    legacy=match[1]
    configfile=service/"receiver-config.json"
    if "CowmataLedgerUploadV2/" in legacy:
        if not configfile.exists():raise ValueError("接收器配置缺失，无法恢复旧版转发命令")
        legacy=json.loads(configfile.read_text("utf-8-sig"))["legacy_command"]
    print("固定CSV：",target);print("接收程序：",service)
    if check:print("预检查通过；尚未安装");return
    service.mkdir(parents=True,exist_ok=True)
    for name in ["runtime","ledger_core.py","ledger_merge.py","ledger_sheets.py","ledger_accounts.py","bootstrap_accounts.py","receiver.py"]:
        src=SOURCE/name;dst=service/name
        if src.resolve()==dst.resolve():continue
        if src.is_dir():shutil.copytree(src,dst,dirs_exist_ok=True)
        else:shutil.copy2(src,dst)
    config=json.loads(configfile.read_text("utf-8-sig")) if configfile.exists() else {}
    config.update(target=str(target),legacy_command=legacy)
    configfile.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf8")
    python=service/"runtime"/"python.exe"
    subprocess.run([str(python),"-I","-B","-c","from receiver import handle,DEFAULT_TARGET;print(handle({'version':2,'action':'probe'},DEFAULT_TARGET))"],cwd=service,check=True)
    target.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(["icacls",str(service),"/grant","cowmata_upload:(OI)(CI)RX"],check=True)
    subprocess.run(["icacls",str(target.parent),"/grant","cowmata_upload:(OI)(CI)M"],check=True)
    timestamp=datetime.now().strftime("%Y%m%d-%H%M%S")
    backup=authfile.with_name(authfile.name+".before-v2-"+timestamp)
    shutil.copy2(authfile,backup)
    command=f"{python.as_posix()} -I -B {(service/'receiver.py').as_posix()}"
    if " " in str(service):raise ValueError("接收程序目录请使用不含空格的路径")
    lines[i]=re.sub(r'command="[^"\r\n]+"','command="'+command+'"',lines[i],count=1)
    try:authfile.write_text("\n".join(lines)+"\n",encoding="utf8")
    except Exception:
        shutil.copy2(backup,authfile);raise
    print("安装完成，不需要重启SSH。旧公钥和restrict限制保留；v1入口已关闭，所有读取和同步都要求登录。")
    print("回退备份：",backup)
    print("需配置受限账号库auth_db并用bootstrap_accounts.py初始化账号；未配置时服务器拒绝访问。")
    print("回到现场软件登录后同步，核对成功后才算完成。")
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--target",default=r"F:\牛舍_现场记录\样本试验台账.csv")
    p.add_argument("--service",default=r"E:\Services\CowmataLedgerUploadV2")
    p.add_argument("--authfile",default=r"C:\Users\cowmata_upload\.ssh\authorized_keys")
    p.add_argument("--public-key",default=str(SOURCE/"upload_key.pub"))
    p.add_argument("--check",action="store_true");a=p.parse_args()
    try:install(a.target,a.service,a.authfile,a.public_key,a.check);return 0
    except Exception as e:print("未完成：",e,file=sys.stderr);return 1
if __name__=="__main__":sys.exit(main())
