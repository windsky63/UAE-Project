"""Independent logout helper. Session cookies travel through stdin, never a file."""

from app_paths import HOME, CODE
import json
import queue
import sys
import threading
from fusion_client import FusionClient, now


def main():
    messages = queue.Queue()

    def read_pipe():
        try:
            for line in sys.stdin:
                messages.put(json.loads(line))
        finally:
            messages.put({'eof': True})

    threading.Thread(target=read_pipe, daemon=True).start()
    client = None
    manual_test = False
    while True:
        try:
            message = messages.get(timeout=0.5)
        except queue.Empty:
            message = {}
        if message.get('done'):
            if client:
                client.http.close()
            return
        if 'cookies' in message:
            manual_test = message.get('manual_test') is True
            if client is None:
                client = FusionClient('', '')
                client.auth_attempted = True
            client.project_id = message.get('project_id')
            client.cabinet_id = message.get('cabinet_id')
            client.http.cookies.clear()
            for cookie in message['cookies']:
                client.http.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
        minute = now().hour * 60 + now().minute
        if message.get('eof') or (not manual_test and (minute >= 630 or minute < 60)):
            if client:
                print('执行独立注销：', now().isoformat(), flush=True)
                client.logout()
            return


if __name__ == '__main__':
    main()
