"""Local demo launcher. Production uses the documented WSGI command."""
import argparse
import logging
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from server import Application

class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads=True

class PrivateRequestLog(WSGIRequestHandler):
    def log_request(self,code='-',size='-'):
        self.log_message('%s %s %s %s',self.command,self.path.split('?',1)[0],code,size)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Run Reconcile locally with synthetic data.')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--local-stage',action='store_true')
    args=parser.parse_args()
    if args.host!='127.0.0.1' and not (args.local_stage and __import__('os').environ.get('RECON_LOCAL_STAGE')=='1'):parser.error('Non-loopback binding requires the explicit disposable staging flag.')
    logging.basicConfig(level=logging.INFO)
    app=Application(data_dir=args.data_dir,demo=True)
    print(f'Reconcile demo: http://127.0.0.1:{args.port} (localhost only)',flush=True)
    try:
        with make_server(args.host,args.port,app,server_class=ThreadedServer,handler_class=PrivateRequestLog) as http: http.serve_forever()
    finally: app.close()
