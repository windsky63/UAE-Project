"""Local protocol fixture. This is not an OpenAI model or a production fallback."""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

lock=threading.Lock()
turns={}
tool_results={}


def send(message):
    with lock:
        print(json.dumps(message,ensure_ascii=False),flush=True)


def event(method,thread,**params):
    send(dict(method=method,params=dict(threadId=thread,**params)))


def reply(identity,result):
    send(dict(id=identity,result=result))


def work(thread,turn):
    if turns[turn].get('business'):
        request_id = 'business-' + turn
        ready = threading.Event()
        tool_results[request_id] = dict(ready=ready)
        event('item/started', thread, item=dict(id=request_id, type='dynamicToolCall',
              tool='project_briefing', arguments=dict(action='generate_and_send'), status='inProgress'))
        send(dict(id=request_id, method='item/tool/call', params=dict(threadId=thread, turnId=turn,
             callId=request_id, tool='project_briefing', arguments=dict(action='generate_and_send'))))
        if not ready.wait(8):
            event('turn/completed', thread, turn=dict(id=turn, status='failed', error=dict(message='Tool response missing')))
            return
        result = tool_results[request_id]['result']
        event('item/completed', thread, item=dict(id=request_id, type='dynamicToolCall',
              tool='project_briefing', status='completed', **result))
        event('item/completed', thread, item=dict(id='answer-'+turn, type='agentMessage', phase='final_answer',
              text=json.dumps(result, ensure_ascii=False)))
        event('turn/completed', thread, turn=dict(id=turn, status='completed'))
        return
    if turns[turn].get('retry'):
        event('error',thread,error=dict(message='Reconnecting... 1/5'),willRetry=True)
        time.sleep(.8)
    event('item/started',thread,item=dict(id='reason-'+turn,type='reasoning',summary=[],content=['PRIVATE_CHAIN_TEST_TOKEN']))
    event('item/reasoning/summaryTextDelta',thread,itemId='reason-'+turn,summaryIndex=0,delta='核对本地样本文件与记录。')
    event('item/reasoning/textDelta',thread,itemId='reason-'+turn,delta='PRIVATE_CHAIN_TEST_TOKEN')
    event('item/completed',thread,item=dict(id='reason-'+turn,type='reasoning',summary=['核对本地样本文件与记录。'],content=['PRIVATE_CHAIN_TEST_TOKEN']))
    path=Path('fixture.txt').resolve()
    command=dict(id='cmd-'+turn,type='commandExecution',command='Get-Content fixture.txt',cwd=str(Path.cwd()),
                 commandActions=[dict(type='read',path=str(path),name='fixture.txt',command='Get-Content fixture.txt')],status='inProgress')
    event('item/started',thread,item=command)
    text=path.read_text(encoding='utf-8')
    event('item/commandExecution/outputDelta',thread,itemId=command['id'],delta=text)
    event('item/completed',thread,item={**command,'status':'completed','exitCode':0,'aggregatedOutput':text})
    event('item/started',thread,item=dict(id='answer-'+turn,type='agentMessage',text='',phase='final_answer'))
    answer=''
    for chunk in ['已读取 `fixture.txt`。\n\n','样本结果：**预制 2 个、安装 1 个**。\n','本条回复用于验证流式界面，未调用真实模型。']:
        if turns[turn]['cancelled']:return
        answer+=chunk
        event('item/agentMessage/delta',thread,itemId='answer-'+turn,delta=chunk)
        time.sleep(.25)
    event('item/completed',thread,item=dict(id='answer-'+turn,type='agentMessage',text=answer,phase='final_answer'))
    event('turn/completed',thread,turn=dict(id=turn,status='completed'))


for line in sys.stdin:
    request=json.loads(line)
    method=request.get('method')
    identity=request.get('id')
    p=request.get('params',{})
    if method is None and identity in tool_results:
        tool_results[identity]['result'] = request.get('result', request.get('error'))
        tool_results[identity]['ready'].set()
        continue
    if method=='initialize':reply(identity,dict(userAgent='local-protocol-fixture'))
    elif method=='initialized':pass
    elif method=='config/read':reply(identity,dict(config={}))
    elif method in ('thread/start','thread/resume'):
        assert p['approvalPolicy']=='never'
        assert p['permissions'] in ('project_tools_read','project_tools_edit')
        assert 'sandbox' not in p
        if method == 'thread/resume':
            assert 'dynamicTools' not in p
        for tool in p.get('dynamicTools', []):
            assert tool['type'] == 'function'
        if method=='thread/resume' and p.get('threadId')=='missing-rollout':
            send(dict(id=identity,error=dict(code=-32600,message='no rollout found for thread id missing-rollout')))
            continue
        if method=='thread/resume' and p.get('threadId')=='auth-failure':
            send(dict(id=identity,error=dict(code=-32600,message='authentication failed')))
            continue
        reply(identity,dict(thread=dict(id=p.get('threadId') or 'thread-'+uuid.uuid4().hex),instructionSources=[]))
    elif method=='thread/unsubscribe':reply(identity,dict(status='unsubscribed'))
    elif method=='turn/start':
        assert p['summary']=='concise'
        assert p['permissions'] in ('project_tools_read','project_tools_edit')
        assert 'sandboxPolicy' not in p
        thread=p['threadId'];turn='turn-'+uuid.uuid4().hex
        turns[turn]=dict(thread=thread,cancelled=False,retry='reconnect' in p['input'][-1]['text'],
                         business='business fixture' in p['input'][-1]['text'])
        reply(identity,dict(turn=dict(id=turn,status='inProgress')))
        event('turn/started',thread,turn=dict(id=turn,status='inProgress'))
        if 'hang' not in p['input'][-1]['text']:
            threading.Thread(target=work,args=(thread,turn),daemon=True).start()
    elif method=='turn/interrupt':
        turn=p['turnId'];turns[turn]['cancelled']=True
        reply(identity,{})
        event('turn/completed',p['threadId'],turn=dict(id=turn,status='interrupted'))
