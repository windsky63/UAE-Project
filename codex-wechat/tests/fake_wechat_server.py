"""Offline resident-backend fixture. Never invokes a model or touches WeChat."""
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
threads, modes = {}, {}
approval_denied = threading.Event()


def record(message):
    with lock, record_path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(message, ensure_ascii=False) + '\n')


def send(message):
    with lock:
        print(json.dumps(message, ensure_ascii=False), flush=True)


def reply(identity, result):
    send(dict(id=identity, result=result))


def event(method, thread, **params):
    send(dict(method=method, params=dict(threadId=thread, **params)))


def generate(thread, turn, payload, structured):
    mode = payload['message']
    if mode == 'crash':
        os._exit(9)
    if mode in ('account-quota', 'account-login'):
        event('error', thread, willRetry=True, error=dict(
            message='usage limit exceeded PRIVATE_SECRET' if mode == 'account-quota' else 'not logged in PRIVATE_SECRET'))
        return
    if mode == 'network-down':
        for _ in range(600):
            event('error', thread, willRetry=True, error=dict(message='error sending request: failed to connect PRIVATE_ENDPOINT'))
            time.sleep(.05)
        return
    if mode == 'slow-initial':
        time.sleep(.4)
    if mode == 'transient-network-slow':
        event('error', thread, willRetry=True, error=dict(message='failed to connect PRIVATE_ENDPOINT'))
        time.sleep(.4)
    if mode == 'idle':
        time.sleep(30)
        return
    def usage(used):
        event('thread/tokenUsage/updated', thread, tokenUsage=dict(
            last=dict(inputTokens=max(0, used-15), outputTokens=15, totalTokens=used,
                      cachedInputTokens=100, reasoningOutputTokens=4),
            total=dict(totalTokens=payload.get('cumulative_used', 9999999)),
            modelContextWindow=payload.get('context_window')))
    if 'early_context_used' in payload and not payload.get('omit_usage'):
        usage(payload['early_context_used'])
    event('item/started', thread, item=dict(id='tool', type='commandExecution',
          command='PRIVATE_COMMAND_DO_NOT_FORWARD'))
    if mode == 'hang':
        time.sleep(30)
        return
    time.sleep(payload.get('work_seconds', 0))
    if mode == 'retry':
        event('error', thread, willRetry=True, error=dict(message='PRIVATE_ENDPOINT'))
    if mode == 'approval':
        send(dict(id='request-approval', method='item/commandExecution/requestApproval',
                  params=dict(threadId=thread)))
        if not approval_denied.wait(3):
            os._exit(8)
    event('item/reasoning/textDelta', thread, delta='PRIVATE_REASONING_DO_NOT_FORWARD')
    event('item/completed', thread, item=dict(id='tool', type='commandExecution',
          aggregatedOutput='PRIVATE_TOOL_OUTPUT'))
    event('item/agentMessage/delta', thread, itemId='answer', delta='PARTIAL_MUST_NOT_BE_SENT')
    if mode != 'bad-final':
        answer = '答复：' + payload['chat_id'] + ':' + mode
        if structured:
            answer = json.dumps(dict(reply=answer, memory_note='内部短摘要'), ensure_ascii=False)
        event('item/completed', thread, item=dict(id='answer', type='agentMessage', phase='final_answer', text=answer))
    event('item/completed', thread, item=dict(id='comment', type='agentMessage', phase='commentary', text='NOT_FINAL'))
    if not payload.get('omit_usage'):
        usage(payload.get('context_used', 138))
    event('turn/completed', thread, turn=dict(id=turn, status='completed'))
    if 'late_context_used' in payload:
        usage(payload['late_context_used'])


record(dict(startup=os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    record(request)
    identity, method = request.get('id'), request.get('method')
    params = request.get('params') or {}
    if method is None:
        if identity == 'request-approval' and request.get('error', {}).get('code') == -32601:
            approval_denied.set()
        continue
    if method == 'initialize':
        assert params['capabilities']['experimentalApi'] is True
        reply(identity, {})
    elif method == 'initialized':
        pass
    elif method == 'config/read':
        if '--fail-config-once' in sys.argv:
            requests = [json.loads(line) for line in record_path.read_text(encoding='utf-8').splitlines()]
            if sum(row.get('method') == 'config/read' for row in requests) == 1:
                send(dict(id=identity, error=dict(code=-32600, message='configuration unavailable')))
                continue
        reply(identity, dict(config=dict(model_provider='fixture', model_providers=dict(
            fixture=dict(name='fixture', base_url='http://localhost.invalid', wire_api='responses')))))
    elif method == 'thread/start':
        assert params['ephemeral'] is True
        assert params['permissions'] == 'project_tools_read'
        assert params['approvalPolicy'] == 'never'
        assert 'sandbox' not in params
        if '--fail-prewarm-once' in sys.argv:
            requests = [json.loads(line) for line in record_path.read_text(encoding='utf-8').splitlines()]
            if sum(row.get('method') == 'thread/start' for row in requests) == 2:
                send(dict(id=identity, error=dict(code=-32600, message='empty session startup failed')))
                continue
        thread = uuid.uuid4().hex
        threads[thread] = params
        reply(identity, dict(thread=dict(id=thread)))
    elif method == 'turn/start':
        assert params['permissions'] == 'project_tools_read'
        assert params['approvalPolicy'] == 'never'
        prompt = params['input'][0]['text']
        payload = json.loads(prompt.split('Task data follows:\n')[-1])
        thread, turn = params['threadId'], uuid.uuid4().hex
        modes[thread] = payload['message']
        # A foreign thread must never supply an answer to this task.
        event('item/completed', 'foreign-thread', item=dict(type='agentMessage', phase='final_answer', text='FOREIGN_SECRET'))
        reply(identity, dict(turn=dict(id=turn, status='inProgress')))
        threading.Thread(target=generate, args=(thread, turn, payload, 'outputSchema' in params), daemon=True).start()
    elif method == 'thread/unsubscribe':
        if modes.get(params['threadId']) == 'cleanup-fail':
            send(dict(id=identity, error=dict(code=-32601, message='unsupported fixture cleanup')))
        else:
            reply(identity, dict(status='unsubscribed'))
    else:
        send(dict(id=identity, error=dict(code=-32601, message='unsupported fixture method')))
