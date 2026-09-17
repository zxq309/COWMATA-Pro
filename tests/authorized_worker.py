"""Run an unchanged business worker against a temporary test CSV authority.

This entry point is test-only and never shipped in the portable application.
The production worker still requires its anonymous-pipe token and all policies.
"""
import runpy
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from cowmata_security.authority import Denied
from cowmata_security.simple_authority import SimpleAuthority
from cowmata_security.client import AccessDenied
import cowmata_security.worker as security_worker
from cowmata_security.protocol import dispatch

csv_path, worker_path, *arguments = sys.argv[1:]
authority = SimpleAuthority(csv_path)
def transport(request):
    try:
        return dispatch(authority, request)
    except Denied as error:
        raise AccessDenied(str(error)) from error
security_worker.read_config = lambda root: transport
sys.argv = [worker_path, *arguments]
runpy.run_path(worker_path, run_name="__main__")
