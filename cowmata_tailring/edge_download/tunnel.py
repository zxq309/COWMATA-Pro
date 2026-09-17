"""Optional authenticated SSH transport from the supplied downloader launcher."""
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .core import Cancelled, Client, DownloadError


def validate_config(config, base_url):
    address = urlsplit(base_url)
    if address.scheme != 'http' or address.hostname not in ('127.0.0.1', 'localhost') or not address.port:
        raise DownloadError('SSH 隧道模式的下载服务器应为 http://127.0.0.1:本机端口')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', config['host']):
        raise DownloadError('SSH 地址格式应为 用户@服务器')
    if not Path(config['key']).is_file():
        raise DownloadError('未找到 SSH 私钥，请选择已有密钥文件')
    if not shutil.which('ssh'):
        raise DownloadError('未找到系统 OpenSSH 客户端 ssh')
    return address.port


class Tunnel:
    def __init__(self, config, base_url, cancel, log):
        self.config, self.base_url, self.cancel, self.log = config, base_url, cancel, log
        self.process = None

    def __enter__(self):
        if self.config is None:
            return self
        port = validate_config(self.config, self.base_url)
        cfg = self.config
        args = [shutil.which('ssh'), '-N', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
                '-o', 'ServerAliveCountMax=2', '-L', f'127.0.0.1:{port}:127.0.0.1:{cfg["remote_port"]}',
                '-i', cfg['key'], '-p', str(cfg['port']), cfg['host']]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            if self.cancel.wait(0.5):
                raise Cancelled()
            if self.process.poll() is not None:
                raise DownloadError('SSH 隧道启动失败：检查密钥、known_hosts、服务器与本机端口占用；已有隧道请取消勾选')
            health = Client(self.base_url, self.cancel, self.log).envelope('/health', {})
            if health.get('ok') is not True:
                raise DownloadError('3090 下载接口未就绪')
            self.log('3090 SSH 隧道已连接')
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
