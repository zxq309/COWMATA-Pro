"""Live packaging regression: the installed app must read the pinned host file in spaced paths."""
import json,os,shutil
from pathlib import Path
import pytest
from cowmata_security.client import AccessDenied
from cowmata_security.ssh_transport import SshTransport
ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(os.environ.get('COWMATA_TEST_LIVE_SSH')!='1',reason='Explicit live-server integration opt-in required')

@pytest.mark.parametrize('directory',['COWMATA Pro 中文','plain-folder'])
def test_pinned_connection_works_from_installation_paths(tmp_path,directory):
 root=tmp_path/directory;root.mkdir()
 shutil.copyfile(ROOT/'cowmata_tailring/edge_download/ledger_known_hosts',root/'known_hosts')
 config=json.loads((ROOT/'cowmata-security.json').read_text(encoding='utf8'));config['ssh']=str(ROOT/config['ssh']);config['known_hosts']='known_hosts'
 transport=SshTransport(config,root)
 # No account, password or code is sent or consumed: a recognized receiver
 # rejects this deliberately unsupported action AFTER host authentication.
 with pytest.raises(AccessDenied,match='^Unknown action$'):
  transport({'action':'diagnostic-invalid-action','product':'pro'})

def test_unknown_host_still_fails_closed_in_spaced_path(tmp_path):
 root=tmp_path/'COWMATA Pro 中文';root.mkdir();(root/'known_hosts').write_text('',encoding='ascii')
 config=json.loads((ROOT/'cowmata-security.json').read_text(encoding='utf8'));config['ssh']=str(ROOT/config['ssh']);config['known_hosts']='known_hosts'
 with pytest.raises(AccessDenied,match='服务器指纹'):
  SshTransport(config,root)({'action':'diagnostic-invalid-action','product':'pro'})
