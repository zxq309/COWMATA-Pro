"""WSGI authority. Production: waitress behind an HTTPS reverse proxy."""
import argparse
import getpass
import json
from .authority import Authority, Denied
from .protocol import dispatch

class Application:
    def __init__(self, authority):
        self.authority=authority
    def __call__(self, env, start_response):
        status='200 OK'
        try:
            if env.get('PATH_INFO')!='/v1/auth' or env.get('REQUEST_METHOD')!='POST':
                raise ValueError('POST /v1/auth required')
            if env.get('CONTENT_TYPE','').split(';')[0]!='application/json':
                raise ValueError('JSON required')
            size=int(env.get('CONTENT_LENGTH','0'))
            if not 1<=size<=8192:
                raise ValueError('Invalid request size')
            raw=env['wsgi.input'].read(size)
            if len(raw)!=size:
                raise ValueError('Incomplete request')
            result=dispatch(self.authority,json.loads(raw),env.get('REMOTE_ADDR','unknown'))
            body={'ok':True,'result':result}
        except Denied as exc:
            status='403 Forbidden'; body={'ok':False,'error':str(exc)}
        except (ValueError,TypeError,KeyError):
            status='400 Bad Request'; body={'ok':False,'error':'Invalid request or duplicate account'}
        except Exception:
            status='503 Service Unavailable'; body={'ok':False,'error':'Authorization unavailable'}
        data=json.dumps(body).encode()
        start_response(status,[('Content-Type','application/json'),('Content-Length',str(len(data))),('Cache-Control','no-store'),('Pragma','no-cache'),('X-Content-Type-Options','nosniff')])
        return [data]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('init','recover','serve'))
    parser.add_argument('--csv',required=True)
    parser.add_argument('--account',default='admin')
    parser.add_argument('--product',choices=('pro','ledger'),default='pro')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    authority=Authority(args.csv,product=args.product)
    if args.command=='serve':
        from waitress import serve
        serve(Application(authority),host='127.0.0.1',port=args.port,threads=8,max_request_body_size=8192)
        return
    password=getpass.getpass('Administrator recovery password (16+ characters): ')
    if args.command=='init':
        if password!=getpass.getpass('Confirm recovery password: '):
            raise SystemExit('Passwords differ')
        authority.bootstrap(args.account,password)
    grant=authority.recover_grant(args.account,password,args.product)
    print('One-time login code (expires in 10 minutes; store privately):')
    print(grant['code'])

if __name__=='__main__':main()
