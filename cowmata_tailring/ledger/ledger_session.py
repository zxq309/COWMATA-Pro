"""Compatibility facade over the memory-only shared authorization session."""
from cowmata_security.client import current_session, AccessDenied

def clear():
    try:current_session().clear()
    except AccessDenied:pass

def current():
    try:
        session=current_session();info=session.identity
        if not session.token:return {}
        return {'token':session.token,'expires_at':info.get('expires_at',0),'user':{'username':info['account'],'display_name':info['account'],'role':info['role']}}
    except AccessDenied:return {}

def active():
    try:return current_session().allows('upload')
    except AccessDenied:return False

def token():return current().get('token','') if active() else ''

def set_session(reply):
    if reply.get('token')!=current().get('token'):raise AccessDenied('Unverified session')
