"""Worker authorization crosses process boundaries only over an anonymous pipe."""
import json
import os
import sys
import threading
from .client import Session, current_session, install_session, read_config, AccessDenied
from .device_store import DeviceStore

def pipe_credentials(capability):
    session=current_session();session.require(capability)
    return json.dumps({'session':session.token}).encode('utf-8')+b'\n'

def authorize_worker(root, capability):
    raw=sys.stdin.buffer.readline(4097)
    if not raw or len(raw)>4096:
        raise AccessDenied('Missing worker authorization')
    values=json.loads(raw)
    token=values.get('session','')
    if not isinstance(token,str) or len(token)!=43:
        raise AccessDenied('Invalid worker authorization')
    transport=read_config(root)
    result=transport({'action':'authorize','session':token,'product':'pro','capability':capability})
    session=Session(transport,'pro',device_store=DeviceStore('pro'));session._accept(result);session.token=token
    session.require(capability);install_session(session)
    def monitor():
        while True:
            threading.Event().wait(60)
            try:session.refresh();session.require(capability)
            except Exception:
                # Interrupted writers keep their existing atomic-save/recovery behavior.
                sys.stderr.write('Authorization expired; worker stopped.\n');sys.stderr.flush();os._exit(75)
    threading.Thread(target=monitor,daemon=True,name='authorization-lease').start()
    return session
