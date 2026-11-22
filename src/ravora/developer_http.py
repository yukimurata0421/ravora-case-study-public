"""Authenticated loopback-only developer console. No provider credentials in responses."""
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

from ravora.providers import ProviderError


WEB=Path(__file__).resolve().parent/'web/developer'


class RequestError(ValueError):
    def __init__(self,code,status=400):self.code=code;self.status=status


class DeveloperHTTP(ThreadingHTTPServer):
    daemon_threads=True
    # Restart while connections from the previous console finish TCP TIME_WAIT.
    allow_reuse_address=True
    allow_reuse_port=False
    def __init__(self,session,secret,port=8767,*,handler=None):
        self.session=session;self.secret=secret;self.slots=threading.BoundedSemaphore(8)
        super().__init__(('127.0.0.1',port),handler or Handler)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version='RAVORA'
    sys_version=''
    def setup(self):super().setup();self.connection.settimeout(5)
    def log_message(self,*_):pass

    def reply(self,status,value,mime='application/json; charset=utf-8'):
        body=json.dumps(value,ensure_ascii=False).encode() if mime.startswith('application/json') else value
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; worker-src 'self'; media-src 'self' blob:; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Permissions-Policy','microphone=(self), camera=(), geolocation=()')
        self.send_header('Connection','close');self.end_headers();self.wfile.write(body);self.close_connection=True

    def guard(self,*,api=True):
        port=self.server.server_port
        if self.headers.get('Host') not in (f'127.0.0.1:{port}',f'localhost:{port}'):raise RequestError('INVALID_HOST',403)
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):raise RequestError('INVALID_ORIGIN',403)
        if self.headers.get('Sec-Fetch-Site')=='cross-site':raise RequestError('CROSS_SITE_REQUEST',403)
        if api and not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+self.server.secret):raise RequestError('UNAUTHORIZED',401)

    def body(self,mime,limit):
        if self.headers.get('Transfer-Encoding'):raise RequestError('CHUNKED_BODY_UNSUPPORTED')
        if self.headers.get_content_type()!=mime:raise RequestError('INVALID_CONTENT_TYPE',415)
        value=self.headers.get('Content-Length','')
        if not re.fullmatch(r'[0-9]{1,8}',value):raise RequestError('INVALID_CONTENT_LENGTH',411)
        size=int(value)
        if not 0<size<=limit:raise RequestError('BODY_TOO_LARGE',413)
        data=self.rfile.read(size)
        if len(data)!=size:raise RequestError('TRUNCATED_BODY')
        return data

    def object(self,keys):
        from ravora.gemini_memory import strict_json
        try:value=strict_json(self.body('application/json',8192).decode())
        except RequestError:raise
        except (ValueError,UnicodeError,ProviderError):raise RequestError('INVALID_JSON') from None
        if not isinstance(value,dict) or set(value)!=set(keys):raise RequestError('INVALID_FIELDS')
        return value

    def do_GET(self):self.handle_safe(self.get)
    def do_POST(self):self.handle_safe(self.post)

    def handle_safe(self,callback):
        try:callback()
        except RequestError as exc:self.reply(exc.status,{'error':exc.code})
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        except Exception:self.reply(500,{'error':'DEVELOPER_SERVICE_FAILED'})
