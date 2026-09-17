"""One protocol for HTTPS and the existing pinned SSH receiver."""
from .authority import Denied

def dispatch(authority, request, peer='local'):
    if not isinstance(request, dict):
        raise ValueError('Object required')
    action = request.get('action')
    token = request.get('session', '')
    if action == 'admin_login' and getattr(authority,'requires_password',False):
        return authority.admin_login(request.get('account'),request.get('password'),request.get('product'),peer,request.get('device_label','Desktop'))
    if action == 'password_device_login' and getattr(authority,'requires_password',False):
        return authority.password_device_login(request.get('account'),request.get('password'),request.get('device_secret'),request.get('product'),peer)
    if action == 'credentials':return authority.credentials(token)
    if action == 'batch_create':return authority.batch_create(token,request.get('count',10))
    if action == 'remove_account':return authority.remove_account(token,request.get('account'))
    if action == 'redeem' and getattr(authority,'requires_password',False):
        return authority.redeem(request.get('account'),request.get('code'),request.get('product'),peer,request.get('device_label','Desktop'),password=request.get('password'))
    if action == 'redeem':
        return authority.redeem(request.get('account'), request.get('code'), request.get('product'), peer, request.get('device_label','Desktop'))
    if action == 'device_login':
        return authority.device_login(request.get('device_secret'), request.get('product'))
    if action == 'devices':
        return {'devices': authority.devices(token)}
    if action == 'revoke_device':
        return authority.revoke_device(token, request.get('device_id'))
    if action == 'refresh':
        return authority.refresh(token, request.get('product'))
    if action == 'logout':
        return authority.logout(token)
    if action == 'users':
        return {'users': authority.users(token)}
    if action == 'create_user':
        return authority.create_user(token, request.get('account'), request.get('role', 'operator'))
    if action == 'issue':
        return authority.issue(token, request.get('account'), request.get('product'), request.get('ttl', 600))
    if action == 'set_active':
        return authority.set_active(token, request.get('account'), request.get('active'))
    if action == 'authorize':
        return authority.authorize(token, request.get('product'), request.get('capability'))
    raise Denied('Unknown action')
