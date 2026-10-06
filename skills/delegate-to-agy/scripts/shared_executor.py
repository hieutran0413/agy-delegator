#!/usr/bin/env python3
"""Fixed-action local executor. No model calls; coordinator configures actions."""
import argparse, json, os, signal, subprocess, sys, time, uuid, threading
from pathlib import Path
import agy_proc as proc
import agy_store as store
ROOT=Path.home()/'.local/share/agy-executor'
POLICY=ROOT/'policy.json'

def identity_matches(meta):
    got=proc.process_identity(meta.get('pid'))
    return bool(got and got[0]==meta.get('started') and 'shared_executor.py' in got[1] and meta['id'] in got[1])

def run_dir(run_id):
    if str(uuid.UUID(run_id))!=run_id:raise ValueError('Invalid run ID')
    return ROOT/'runs'/run_id

def status(run_id):
    d=run_dir(run_id);meta=store.read_json(d/'run.json')
    if not meta:raise ValueError('Unknown run')
    log=d/'output.log'
    if not meta.get('done') and meta.get('pid') and not identity_matches(meta):
        meta.update(done=True,state='failed',error='Executor process exited without final result');store.write_json(d/'run.json',meta)
    with log.open('rb') if log.exists() else open(os.devnull,'rb') as f:
        f.seek(0,2);size=f.tell();f.seek(max(0,size-6000));tail=f.read().decode('utf8','replace')
    public={k:v for k,v in meta.items() if k not in {'argv','pid','started'}}
    return {**public,'outputTail':tail,'logPath':str(log),'outputTruncated':size>6000}

def start(workspace,action):
    workspace=str(Path(workspace).resolve(strict=True));policy=store.read_json(POLICY)
    spec=policy.get('workspaces',{}).get(workspace,{}).get(action)
    if not spec:raise ValueError('Action not configured by coordinator for this workspace')
    run_id=str(uuid.uuid4());d=run_dir(run_id);d.mkdir(parents=True,mode=0o700)
    meta={'id':run_id,'workspace':workspace,'action':action,'argv':spec['argv'],'timeout':spec['timeout'],'state':'starting','done':False,'createdAt':time.time()}
    store.write_json(d/'run.json',meta)
    with (d/'supervisor.log').open('wb') as log:
        p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker',run_id],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    threading.Thread(target=p.wait,daemon=True).start()
    got=proc.process_identity(p.pid)
    meta.update(pid=p.pid,started=got[0] if got else None);store.write_json(d/'run.json',meta)
    # Worker waits for this launch receipt before starting the configured command.
    return {'runId':run_id,'state':'starting','nextAction':'poll'}

def worker(run_id):
    d=run_dir(run_id)
    for _ in range(100):
        meta=store.read_json(d/'run.json')
        if meta.get('pid'):break
        time.sleep(.02)
    if not identity_matches(meta):return
    child=None
    def cancel(*_):
        if child:proc.terminate_group(child.pid,2,1,reap=child.poll)
        meta.update(done=True,state='cancelled',finishedAt=time.time());store.write_json(d/'run.json',meta)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,cancel)
    meta['state']='running';store.write_json(d/'run.json',meta)
    try:
        with (d/'output.log').open('wb') as log:
            # Child remains in supervisor group; cancellation kills only this run.
            prior_mask=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM})
            try:
                child=subprocess.Popen(meta['argv'],cwd=meta['workspace'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK,prior_mask))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK,prior_mask)
            deadline=time.monotonic()+meta['timeout']
            while child.poll() is None:
                if time.monotonic()>deadline or log.tell()>20*1024*1024:
                    proc.terminate_group(child.pid,2,1,reap=child.poll)
                    child.wait()
                    meta.update(state='timed_out',error='Time or output limit reached');break
                time.sleep(.1)
            else:meta['state']='finished' if child.returncode==0 else 'failed'
            meta['exitCode']=child.returncode
            if proc.group_alive(child.pid):proc.terminate_group(child.pid,2,1,reap=child.poll)
    except Exception as e:meta.update(state='failed',error=str(e))
    meta.update(done=True,finishedAt=time.time());store.write_json(d/'run.json',meta)

def stop(run_id):
    meta=store.read_json(run_dir(run_id)/'run.json')
    if not meta:raise ValueError('Unknown run')
    if not meta.get('done'):
        if not identity_matches(meta):raise ValueError('Process identity cannot be confirmed')
        os.killpg(meta['pid'],signal.SIGTERM)
        for _ in range(30):
            if not identity_matches(meta):break
            time.sleep(.1)
        if identity_matches(meta):os.killpg(meta['pid'],signal.SIGKILL)
    return status(run_id)

TOOLS=[{'name':'start_action','description':'Run a coordinator-configured action without model calls. Returns runId; poll until done. No arbitrary commands.', 'inputSchema':{'type':'object','properties':{'workspace':{'type':'string'},'action':{'type':'string'}},'required':['workspace','action'],'additionalProperties':False}},
 {'name':'action_status','description':'Compact execution status and last 6000 bytes; full log retained locally.', 'inputSchema':{'type':'object','properties':{'run_id':{'type':'string'}},'required':['run_id'],'additionalProperties':False}},
 {'name':'stop_action','description':'Stop only this execution after a user cancellation or configured stop condition.', 'inputSchema':{'type':'object','properties':{'run_id':{'type':'string'}},'required':['run_id'],'additionalProperties':False}}]

def dispatch(req):
    method=req['method']
    if method=='initialize':return {'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'shared-executor','version':'0.2.0'}}
    if method=='tools/list':return {'tools':TOOLS}
    if method=='ping':return {}
    if method=='tools/call':
        p=req['params'];a=p.get('arguments',{});name=p['name']
        spec=next((t for t in TOOLS if t['name']==name),None)
        try:
            if not spec or set(a)!=set(spec['inputSchema']['required']) or not all(isinstance(x,str) for x in a.values()):raise ValueError('Invalid arguments')
            if name=='action_status':
                deadline=time.monotonic()+20
                while True:
                    v=status(a['run_id'])
                    if v.get('done') or time.monotonic()>=deadline:break
                    time.sleep(.2)
            else:v=start(a['workspace'],a['action']) if name=='start_action' else stop(a['run_id'])
            return {'content':[{'type':'text','text':json.dumps(v)}],'isError':False}
        except Exception as e:return {'content':[{'type':'text','text':str(e)}],'isError':True}
    raise ValueError('Unknown method')

def main():
    os.umask(0o077);ROOT.mkdir(parents=True,exist_ok=True)
    if len(sys.argv)>1:
        if sys.argv[1]=='worker':worker(sys.argv[2]);return
        p=argparse.ArgumentParser();p.add_argument('command',choices=['register']);p.add_argument('--workspace',required=True);p.add_argument('--action',required=True);p.add_argument('--argv-file',required=True);p.add_argument('--timeout',type=int,default=120);a=p.parse_args()
        argv=json.loads(Path(a.argv_file).read_text())
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) for x in argv) or not 1<=a.timeout<=600:raise ValueError('Invalid action')
        v=store.read_json(POLICY);v.setdefault('workspaces',{}).setdefault(str(Path(a.workspace).resolve(strict=True)),{})[a.action]={'argv':argv,'timeout':a.timeout};store.write_json(POLICY,v);return
    for line in sys.stdin:
        req=None
        try:
            req=json.loads(line)
            if 'id' not in req:continue
            out={'jsonrpc':'2.0','id':req['id'],'result':dispatch(req)}
        except Exception as e:out={'jsonrpc':'2.0','id':req.get('id') if req else None,'error':{'code':-32603,'message':str(e)}}
        print(json.dumps(out),flush=True)
if __name__=='__main__':main()
