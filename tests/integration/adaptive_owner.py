#!/usr/bin/env python3
"""Adaptive transitions and asynchronous connection lifetime/order checks."""
import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import tempfile
import time

from sorted_set_owner import server, check_pipeline
from sorted_set_concurrency import Client, frame
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'bench/loadtest/scripts'))
from screen_key_owner import preload, verify


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary',required=True)
    p.add_argument('--client',required=True)
    p.add_argument('--output',type=pathlib.Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='vex-adaptive-invalid-') as directory:
        for value,extra,fixed in [('invalid',['--no-persistence'],None),('1',[],None),('1',['--no-persistence'],'hot')]:
            env=dict(os.environ,VEX_CONFIG='',VEX_EXPERIMENTAL_ADAPTIVE_OWNER=value)
            env.pop('VEX_EXPERIMENTAL_OWNER_KEY',None)
            if fixed:env['VEX_EXPERIMENTAL_OWNER_KEY']=fixed
            result=subprocess.run([str(pathlib.Path(args.binary).resolve()),'--reactor','--workers','4','--port','0',*extra],
                                  cwd=directory,env=env,capture_output=True,timeout=10)
            assert result.returncode != 0 and b'InvalidExperimentalOwnerConfiguration' in result.stderr
    phases=[];clocks=[]
    log=args.output/'server.log'
    with server(args.binary,None,8,adaptive=True,log_path=log) as (address,process):
        preload(address)
        for name,uniform in [('uniform-before',True),('hot',False),('uniform-after',True)]:
            command=[str(pathlib.Path(args.client).resolve()),'-host',f'{address[0]}:{address[1]}','-seconds','4']
            if uniform:command+=['-uniform']
            began=time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            out=subprocess.run(command,env=dict(os.environ,GOMAXPROCS='4'),capture_output=True,check=True,timeout=20)
            ended=time.clock_gettime_ns(time.CLOCK_MONOTONIC);raw=json.loads(out.stdout);phases.append(raw)
            assert all(not c['Error'] for c in raw['Clients'])
            (args.output/(name+'.json')).write_bytes(out.stdout)
            clocks.append(dict(phase=name,start=began,end=ended))
            print('PASS phase',name,flush=True)
        verify(address,phases)
        # Reactivate, then check ordered pipelines while requests are routed.
        subprocess.run([str(pathlib.Path(args.client).resolve()),'-host',f'{address[0]}:{address[1]}','-seconds','3'],
                       env=dict(os.environ,GOMAXPROCS='4'),stdout=subprocess.DEVNULL,check=True,timeout=20)
        with Client(address) as c:c.call('FLUSHALL')
        check_pipeline(address,'lb:0')
        # Concurrent EXEC and routed readers must not expose a partial transaction.
        with Client(address) as c:c.call('ZADD','lb:0',0,'atomic')
        barrier=threading.Barrier(8)
        def atomic(index):
            with Client(address) as c:
                barrier.wait()
                for _ in range(300):
                    if index==0:
                        c.socket.sendall(frame('MULTI')+frame('ZINCRBY','lb:0',1,'atomic')+frame('ZINCRBY','lb:0',-1,'atomic')+frame('EXEC'))
                        assert [c.read() for _ in range(4)]==[b'OK',b'QUEUED',b'QUEUED',[b'1.000000',b'0.000000']]
                    else:assert float(c.call('ZSCORE','lb:0','atomic'))==0
        with concurrent.futures.ThreadPoolExecutor(8) as pool:list(pool.map(atomic,range(8)))
        # Disconnect with replies pending, then force fd reuse. Old completions
        # must never appear on the replacement connection.
        for _ in range(128):
            with Client(address) as c:c.socket.sendall(frame('ZINCRBY','lb:0',1,'gone')*64)
            with Client(address) as c:assert c.call('PING')==b'PONG'
        assert process.poll() is None
    changes=[(kind,int(stamp)) for kind,stamp in re.findall(r'adaptive-owner (activate|release) monotonic_ns=(\d+)',log.read_text())]
    (args.output/'transitions.json').write_text(json.dumps(dict(phases=clocks,changes=changes,verified=False),indent=2)+'\n')
    first,hot,last=clocks
    assert not any(first['start']<=at<=first['end'] for _,at in changes),changes
    assert any(kind=='activate' and hot['start']<=at<=hot['end'] for kind,at in changes),changes
    assert any(kind=='release' and last['start']<=at<=last['end'] for kind,at in changes),changes
    assert not any(kind=='activate' and last['start']<=at<=last['end'] for kind,at in changes),changes
    (args.output/'transitions.json').write_text(json.dumps(dict(phases=clocks,changes=changes,verified=True),indent=2)+'\n')
    print('PASS adaptive activation/release, exact scores, ordered replies, EXEC, disconnect/fd reuse')


if __name__=='__main__':main()
