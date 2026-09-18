"""Adapter for the existing restricted Ledger SSH receiver."""
from .authority import Authority, Denied
from .protocol import dispatch

def authority(config,product):
    database=config.get('security_csv',{}).get(product)
    if not database:raise Denied('安全授权服务尚未初始化，请联系管理员')
    from pathlib import Path
    if not Path(database).is_file():raise Denied('管理员账号表不可用')
    import csv,io
    from .simple_authority import decode_registry
    header=next(csv.reader(io.StringIO(decode_registry(Path(database).read_bytes()),newline=''),strict=True),[])
    if header==['账号','密码','授权码']:
        from .simple_authority import SimpleAuthority
        return SimpleAuthority(database,product=product)
    return Authority(database,product=product)

def handle_security(request, config, peer):
    try:
        return {'version':2,'ok':True,'result':dispatch(authority(config,request.get('request',{}).get('product')),request.get('request'),peer)}
    except (Denied,ValueError,TypeError,KeyError) as exc:
        return {'version':2,'ok':False,'code':'AUTH_REQUIRED','error':str(exc)}

def ledger_principal(request,config):
    token=request.get('session_token','')
    # Pro receives read-only CSV access; Ledger can submit only with its own product session.
    product=request.get('security_product','ledger')
    a=authority(config,product)
    view=a.authorize(token,product,'upload' if product=='ledger' else 'prepare')
    if product!='ledger' and request.get('action') not in ('pull','probe','inventory'):
        raise Denied('Pro 会话仅可读取现场台账')
    return {'username':view['account'],'display_name':view['account'],'role':view['role']}
