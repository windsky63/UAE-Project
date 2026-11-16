"""No model, filesystem tools or network: shared-process protocol fixture."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
record_path = Path(sys.argv[1])
lock = threading.Lock()
threads = {}
tool_results = {}


def record(value):
    with lock, record_path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value) + '\n')


def send(value):
    with lock:
        print(json.dumps(value), flush=True)


def event(method, thread, **params):
    send(dict(method=method, params=dict(threadId=thread, **params)))


def finish(thread, turn, payload):
    time.sleep(payload.get('delay', .1))
    if payload.get('tool'):
        identity = 'tool-' + turn
        ready = threading.Event()
        tool_results[identity] = dict(ready=ready)
        send(dict(id=identity, method='item/tool/call', params=dict(threadId=thread, turnId=turn,
                  tool='project_briefing', arguments=dict(action='status', task_id='fixture'))))
        if not ready.wait(5):
            return
    event('item/reasoning/textDelta', thread, delta='PRIVATE_REASONING')
    event('item/completed', thread, item=dict(id=turn, type='agentMessage', phase='final_answer', text=payload['answer']))
    event('turn/completed', thread, turn=dict(id=turn, status='completed'))


record(dict(startup=os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    record(request)
    identity, method = request.get('id'), request.get('method')
    if method is None and identity in tool_results:
        tool_results[identity]['ready'].set()
        continue
    params = request.get('params') or {}
    result = {}
    if method == 'initialize':
        pass
    elif method == 'initialized':
        continue
    elif method == 'config/read':
        time.sleep(params.get('delay', 0))
        result = dict(config=dict(model_provider='openai'))
    elif method in ('thread/start', 'thread/resume'):
        thread = params.get('threadId') or uuid.uuid4().hex
        threads[thread] = params
        result = dict(thread=dict(id=thread))
    elif method == 'turn/start':
        payload = json.loads(params['input'][0]['text'])
        if payload.get('crash'):
            os._exit(9)
        thread, turn = params['threadId'], uuid.uuid4().hex
        result = dict(turn=dict(id=turn))
        send(dict(id=identity, result=result))
        event('turn/started', thread, turn=dict(id=turn))
        threading.Thread(target=finish, args=(thread, turn, payload), daemon=True).start()
        continue
    elif method == 'thread/unsubscribe':
        threads.pop(params['threadId'], None)
    elif method == 'turn/interrupt':
        event('turn/completed', params['threadId'], turn=dict(id=params['turnId'], status='interrupted'))
    send(dict(id=identity, result=result))
