"""Session-protected management API with configurable local/LAN listening."""
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import mimetypes
from pathlib import Path
import secrets
import socket
import sqlite3
import shutil
import sys
import traceback
from urllib.parse import parse_qs, urlsplit, quote
from urllib.request import Request, urlopen
import webbrowser

from manager import Manager, HOME, clean_log, now
from codex_chat import CodexChat


def local_addresses():
    addresses = {'127.0.0.1'}
    try:
        addresses.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(addresses - {'0.0.0.0'})


def network_settings(args):
    config = HOME / 'network_config.json'
    settings = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else {}
    host = '0.0.0.0' if args.lan else args.host or settings.get('host', '127.0.0.1')
    host = str(ipaddress.IPv4Address(host))
    port = args.port if args.port is not None else settings.get('port', 8765)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('端口必须为 1—65535 的整数')
    extra = settings.get('allowed_hosts', [])
    if not isinstance(extra, list) or any(not isinstance(item, str) or not item or
            any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in item)
            for item in extra):
        raise ValueError('allowed_hosts 必须为 IP 或主机名列表，不含端口、通配符或 URL')
    hosts = {'127.0.0.1', 'localhost'}
    if host == '0.0.0.0':
        hosts.update(local_addresses())
    else:
        hosts.add(host)
    hosts.update(item.lower() for item in extra)
    return host, port, sorted(hosts)


def handler(manager, token, static=None, allowed_hosts=None, direct_access=False):
    allowed_hosts = set(allowed_hosts or ('127.0.0.1', 'localhost'))
    static = Path(static or HOME / 'web/dist').resolve()
    chat = getattr(manager, 'codex_chat', None)
    if chat is None:
        chat = manager.codex_chat = CodexChat(manager)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'ProjectTools/1.0'

        def log_message(self, *args):
            pass

        def failure(self, detail):
            identity=secrets.token_hex(6)
            # Log paths, never query strings, request bodies, cookies or tokens.
            message=(f'[{now()}] [ERROR] 管理中心请求失败：request_id={identity}；'
                     f'method={self.command}；path={urlsplit(self.path).path}\n'+traceback.format_exc())
            print(clean_log(message),file=sys.stderr,flush=True)
            return self.respond(dict(detail=detail,request_id=identity),500)

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Closing a tab can interrupt headers, bodies, redirects or SSE.
                # The connection is gone; sending an error response would fail too.
                self.close_connection = True

        def respond(self, value, code=200):
            data = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            if direct_access:
                return True
            supplied = self.headers.get('Host', '').lower()
            return supplied in {f'{host}:{self.server.server_port}' for host in allowed_hosts} or (
                self.server.server_port == 80 and supplied in allowed_hosts)

        def authorized(self):
            if direct_access:
                return True
            if not self.valid_host():
                return False
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                return hmac.compare_digest(cookie['project_session'].value, token)
            except (KeyError, ValueError):
                return False

        def do_GET(self):
            if not self.valid_host():
                return self.respond(dict(detail='访问地址未允许，请核对 network_config.json。'), 403)
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == '/api/session':
                supplied = query.get('token', [''])[0]
                if not hmac.compare_digest(supplied, token):
                    return self.respond(dict(detail='请从“打开管理中心.cmd”进入'), 403)
                self.send_response(303)
                self.send_header('Set-Cookie', f'project_session={token}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Location', '/')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                return
            if parsed.path.startswith('/api/'):
                if not self.authorized():
                    return self.respond(dict(detail='会话未建立，请使用服务器提供的带会话访问链接。'), 403)
                try:
                    get = lambda name, default='': query.get(name, [default])[0]
                    offset, limit = int(get('offset', '0')), int(get('limit', '20'))
                    if offset < 0 or not 1 <= limit <= 100:
                        raise ValueError('分页参数无效')
                    if parsed.path == '/api/codex/options':
                        return self.respond(chat.options())
                    if parsed.path == '/api/codex/sessions':
                        return self.respond(dict(items=chat.list(get('view','active')),warning=chat.import_warning))
                    if parsed.path == '/api/codex/session':
                        return self.respond(chat.snapshot(get('id')))
                    if parsed.path == '/api/codex/events':
                        identity = get('id')
                        chat.snapshot(identity)
                        after = int(self.headers.get('Last-Event-ID') or get('after','0'))
                        if after < 0:
                            raise ValueError('事件位置无效')
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream; charset=utf-8')
                        self.send_header('Cache-Control','no-cache, no-store')
                        self.send_header('X-Accel-Buffering','no')
                        self.end_headers()
                        try:
                            while not chat.closed:
                                events = chat.events(identity, after)
                                if events:
                                    for event in events:
                                        payload = json.dumps(event,ensure_ascii=False)
                                        self.wfile.write(f'id: {event["seq"]}\ndata: {payload}\n\n'.encode('utf-8'))
                                        after=event['seq']
                                else:
                                    self.wfile.write(b': heartbeat\n\n')
                                self.wfile.flush()
                                with chat.condition:
                                    chat.condition.wait(timeout=.25)
                        except (OSError,sqlite3.Error):
                            return
                        return
                    if parsed.path == '/api/overview':
                        return self.respond(manager.overview())
                    if parsed.path == '/api/history-cleanup':
                        return self.respond(manager.history_cleanup_status())
                    if parsed.path == '/api/ima':
                        from ima_control import snapshot
                        return self.respond(snapshot(manager))
                    if parsed.path == '/api/ima/records':
                        from ima_control import records
                        return self.respond(records(manager, get('kind', 'plan'), offset, limit,
                                                    get('search'), get('status')))
                    if parsed.path == '/api/publications':
                        return self.respond(manager.publications(get('date')))
                    if parsed.path == '/api/tasks':
                        return self.respond(dict(items=manager.tasks(get('tool'),limit)))
                    if parsed.path == '/api/config':
                        return self.respond(manager.config(get('tool')))
                    if parsed.path == '/api/nas/hash-status':
                        return self.respond(manager.nas_hash_status())
                    if parsed.path == '/api/files':
                        return self.respond(manager.files(get('category','downloads'),get('search'),offset,limit,
                            filters=json.loads(get('filters','{}')),sort=get('sort','name'),direction=get('direction','asc'),
                            facet=get('facet'),facet_search=get('facet_search'),refresh=get('refresh')=='1'))
                    if parsed.path == '/api/preview':
                        return self.respond(manager.preview(get('id')))
                    if parsed.path == '/api/logs':
                        return self.respond(manager.logs(get('tool'),get('search'),get('level'),offset,limit,compact=get('compact','1')!='0'))
                    if parsed.path == '/api/wechat-jobs':
                        return self.respond(dict(items=manager.wechat_jobs()))
                    if parsed.path.startswith('/api/file/'):
                        path = manager.artifact(parsed.path.rsplit('/',1)[-1])
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/png' if path.suffix.lower()=='.png' else 'application/octet-stream')
                        self.send_header('Content-Disposition', ('inline' if path.suffix.lower()=='.png' else 'attachment')
                                         + "; filename*=UTF-8''" + quote(path.name))
                        self.send_header('X-Content-Type-Options','nosniff')
                        self.send_header('Cache-Control','no-store')
                        self.send_header('Content-Length',str(path.stat().st_size))
                        self.end_headers()
                        with path.open('rb') as stream:
                            shutil.copyfileobj(stream,self.wfile)
                        return
                    return self.respond(dict(detail='接口不存在'),404)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    raise
                except (ValueError,KeyError,OSError) as exc:
                    return self.respond(dict(detail=str(exc)[:500]),400)
                except Exception:
                    return self.failure('读取失败，请检查本地数据库或工具目录。')
            supplied = static / parsed.path.lstrip('/')
            path = supplied.resolve()
            if not path.is_relative_to(static):
                return self.respond(dict(detail='路径无效'),404)
            if parsed.path == '/' or not path.is_file():
                path = static / 'index.html'
            if not path.is_file():
                return self.respond(dict(detail='前端尚未构建，请运行 web 目录的 npm run build。'),503)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            expected_origin = f'http://{self.headers.get("Host", "")}'
            if not self.authorized() or self.headers.get('Origin', expected_origin) != expected_origin or self.headers.get('X-Admin-Request') != '1':
                return self.respond(dict(detail='请求未授权'),403)
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError('请求内容大小无效')
                data = json.loads(self.rfile.read(length))
                if self.path == '/api/ima/query':
                    from ima_control import query
                    return self.respond(query(manager, data['operation'], data.get('data', {})))
                if self.path == '/api/codex/sessions':
                    return self.respond(chat.create(data.get('title','新对话'),data.get('mode','read-only'),
                                                   data.get('model',''),data.get('effort','medium'),data.get('transport','inherit')))
                if self.path == '/api/codex/send':
                    return self.respond(chat.send(data['session_id'],data['text'],data['request_id']))
                if self.path == '/api/codex/session-action':
                    return self.respond(chat.session_action(data['session_id'],data['action'],data.get('title','')))
                if self.path == '/api/codex/configure':
                    return self.respond(chat.configure(data['session_id'],data.get('mode','read-only'),
                                                       data.get('model',''),data.get('effort','medium'),data.get('transport','inherit')))
                if self.path == '/api/codex/stop':
                    return self.respond(chat.stop(data['session_id']))
                if self.path == '/api/action':
                    if str(data['action']).startswith('push-') or data['action'] == 'briefing-send':
                        raise ValueError('请通过每日推送页面确认后执行')
                    return self.respond(manager.action(data['tool'],data['action']))
                if self.path == '/api/publications/push':
                    if data.get('confirmed') is not True:
                        raise ValueError('请先确认推送内容及接收会话')
                    return self.respond(manager.push_publications(data['date'],data['kind']))
                if self.path == '/api/actions/all':
                    if 'tools' in data and not isinstance(data['tools'], list):
                        raise ValueError('工具选择必须是列表')
                    return self.respond(manager.bulk_action(data['action'], **({'tools': data['tools']} if 'tools' in data else {})))
                if self.path == '/api/tasks/clear':
                    return self.respond(manager.clear_tasks(data['tool']))
                if self.path == '/api/logs/clear':
                    return self.respond(manager.clear_logs(data.get('tool',''),data.get('scope','history')))
                if self.path == '/api/config':
                    return self.respond(manager.save_config(data['tool'],data['data'],data.get('revision','')))
                if self.path == '/api/nas/hash-check':
                    return self.respond(manager.nas_hash_check())
                if self.path == '/api/nas/setup':
                    return self.respond(manager.nas_setup())
                return self.respond(dict(detail='接口不存在'),404)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                raise
            except (ValueError,KeyError,OSError) as exc:
                return self.respond(dict(detail=str(exc)[:500]),400)
            except Exception:
                return self.failure('操作失败，请查看运行日志。')

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', help='监听 IPv4 地址，默认 127.0.0.1')
    parser.add_argument('--lan', action='store_true', help='启用局域网访问，监听 0.0.0.0')
    parser.add_argument('--port',type=int)
    parser.add_argument('--no-browser',action='store_true')
    args = parser.parse_args()
    try:
        host, port, allowed_hosts = network_settings(args)
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    # Reopening the launcher must not reset an existing manager's task registry.
    session = HOME / 'runtime/session.json'
    if session.is_file():
        try:
            saved = json.loads(session.read_text(encoding='utf-8'))
            parsed = urlsplit(saved['url'])
            supplied = parse_qs(parsed.query).get('token', [''])[0]
            if (saved.get('host', '127.0.0.1') == host and parsed.port == port and
                    saved.get('allowed_hosts', ['127.0.0.1', 'localhost']) == allowed_hosts):
                request = Request(f'http://{parsed.netloc}/api/overview',
                                  headers={'Cookie': 'project_session=' + supplied})
                with urlopen(request, timeout=2) as response:
                    if len(json.load(response).get('tools', [])) == 3:
                        print('管理中心已在运行。', flush=True)
                        if not args.no_browser:
                            webbrowser.open(saved['url'])
                        return
        except (OSError,ValueError,KeyError):
            pass
    manager = Manager()
    token = secrets.token_urlsafe(32)
    direct_access = not ipaddress.IPv4Address(host).is_loopback
    server = ThreadingHTTPServer((host,port),handler(manager,token,allowed_hosts=allowed_hosts,
                                                   direct_access=direct_access))
    browser_host = '127.0.0.1' if host == '0.0.0.0' else host
    suffix = '/' if direct_access else f'/api/session?token={token}'
    url = f'http://{browser_host}:{server.server_port}{suffix}'
    lan_hosts = [name for name in allowed_hosts if name not in {'127.0.0.1', 'localhost'}] if host != '127.0.0.1' else []
    lan_urls = [f'http://{name}:{server.server_port}/' for name in lan_hosts]
    (manager.runtime/'session.json').write_text(json.dumps(dict(url=url,port=server.server_port,
        host=host,allowed_hosts=allowed_hosts,lan_urls=lan_urls)),encoding='utf-8')
    print(f'管理中心监听：{host}:{server.server_port}；本机访问：{url}',flush=True)
    if lan_urls:
        print('局域网访问：' + '、'.join(lan_urls),flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        if getattr(manager,'codex_chat',None):
            manager.codex_chat.close()
        server.server_close()


if __name__ == '__main__':
    main()
