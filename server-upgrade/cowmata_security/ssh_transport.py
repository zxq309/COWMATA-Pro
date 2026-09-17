"""Reuse the restricted upload SSH account; never bundle its private key."""
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path
from .client import AccessDenied

class SshTransport:
    def __init__(self, config, root):
        self.root=Path(root).resolve()
        self.host=str(config['host']);self.port=int(config['port']);self.user=str(config['user'])
        ipaddress.ip_address(self.host)
        if not 1<=self.port<=65535 or not re.fullmatch(r'[A-Za-z0-9_.-]+',self.user):
            raise ValueError('Invalid SSH endpoint')
        self.exe=self.root/config['ssh']
        self.hosts=self.root/config['known_hosts']
        key=config.get('key')
        if key:
            self.key=Path(os.path.expandvars(key))
        else:
            local=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))
            from .profile import profile_path
            candidates=[profile_path(),Path(os.environ.get('COWMATA_LEDGER_AUTH',str(self.root)))/'upload_key',self.root/'auth/upload_key',local/'Programs/COWMATA 现场台账/auth/upload_key']
            self.key=next((p for p in candidates if p.is_file()),candidates[-1])
        for p in (self.exe,self.hosts,self.key):
            if not p.is_file():raise AccessDenied('缺少服务器连接组件或设备授权：'+str(p))
    def __call__(self, request):
        # All account/code/session fields travel over stdin, never argv or a job file.
        data=json.dumps({'version':2,'action':'security','request':request},ensure_ascii=False).encode()
        # Resolve the pinned file relative to its directory, avoiding OpenSSH -o path splitting.
        args=[str(self.exe),'-F','none','-T','-o','ProxyCommand=none','-o','ProxyJump=none',
              '-o','ClearAllForwardings=yes','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
              '-o','StrictHostKeyChecking=yes','-o','GlobalKnownHostsFile=none',
              '-o','UserKnownHostsFile='+self.hosts.name,'-o','HostKeyAlias=['+self.host+']:'+str(self.port),
              '-o','ConnectTimeout=8','-o','ConnectionAttempts=1','-i',str(self.key.resolve()),'-p',str(self.port),
              self.user+'@'+self.host,'cowmata-ledger-upload-v2']
        env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH','LOCALAPPDATA','USERPROFILE')}
        try:
            result=subprocess.run(args,input=data+b'\n',capture_output=True,timeout=15,env=env,cwd=self.hosts.parent,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (OSError,subprocess.TimeoutExpired):raise OSError('授权服务器连接超时') from None
        if len(result.stdout)>4*1024*1024:raise AccessDenied('Invalid response size')
        try:reply=json.loads(result.stdout)
        except (ValueError,UnicodeError):
            error=result.stderr.decode('utf-8','replace')
            if any(s in error.lower() for s in ('host key verification failed','identification has changed','permission denied')):
                raise AccessDenied('服务器指纹或设备授权验证失败') from None
            raise OSError('授权服务器不可达或协议未启用') from None
        if not reply.get('ok'):
            raise AccessDenied(reply.get('error','授权失败'))
        return reply['result']
