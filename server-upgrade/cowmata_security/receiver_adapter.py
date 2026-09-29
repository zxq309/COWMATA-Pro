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
        inner=request.get('request',{})
        removed=_credential(config,inner) if isinstance(inner,dict) and inner.get('action')=='remove_account' else None
        result=dispatch(authority(config,inner.get('product')),inner,peer)
        mirror(config,inner,result,removed)
        return {'version':2,'ok':True,'result':result}
    except (Denied,ValueError,TypeError,KeyError) as exc:
        return {'version':2,'ok':False,'code':'AUTH_REQUIRED','error':str(exc)}

def _credential(config,inner):
    """The row about to be removed, so the sibling registry only drops the same person."""
    from pathlib import Path
    from .simple_authority import parse_registry
    database=config.get('security_csv',{}).get(inner.get('product'))
    try:return next((r for r in parse_registry(Path(database).read_bytes()) if r['account']==inner.get('account')),None)
    except (OSError,TypeError,ValueError):return None

def mirror(config,inner,result,removed=None):
    """One named account signs in to both the Annotator and the uploader."""
    action=inner.get('action') if isinstance(inner,dict) else None
    if action not in ('create_account','remove_account') or not isinstance(result,dict):return
    if action=='remove_account' and removed is None:result['mirror']={};return
    from pathlib import Path
    from .simple_authority import mirror_credentials
    status={}
    for product,database in sorted(config.get('security_csv',{}).items()):
        if product==inner.get('product') or not Path(database).is_file():continue
        try:
            if action=='create_account':status[product]=mirror_credentials(database,product,add=result.get('created'))
            else:status[product]=mirror_credentials(database,product,remove=removed)
        except Exception as exc:
            status[product]='failed: '+str(exc)
    result['mirror']=status

def ledger_principal(request,config):
    token=request.get('session_token','')
    # 4.4.0: the Annotator embeds the uploader. Writes need the 'upload'
    # capability of whichever product signed the session; reads need 'prepare'.
    product=request.get('security_product','ledger')
    a=authority(config,product)
    read_only=request.get('action') in ('pull','probe','inventory')
    view=a.authorize(token,product,'upload' if product=='ledger' or not read_only else 'prepare')
    return {'username':view['account'],'display_name':view['account'],'role':view['role']}
