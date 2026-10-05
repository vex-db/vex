#!/usr/bin/env python3
"""Same-binary, fixed-client AWS sweep of runtime sorted-set partition counts."""
import argparse
import hashlib
import json
import pathlib
import random
import shutil
import time
import compare_sorted_set_scaling as b
s = b.s
COUNTS = [64, 128, 256, 512, 1024]
SHAPES = {'uniform64': (64, 4096, False), 'uniform1024': (1024, 256, False), 'hot': (64, 4096, True)}


def trace(boards, members, hot):
    rng = random.Random(20260927)
    lines = []
    for i in range(32768):
        board, member = rng.randrange(boards), rng.randrange(members)
        key = 0 if hot else board
        command = f'"ZRANK" "lb:{key}" "m:{member}"' if i % 5 == 0 else f'"ZINCRBY" "lb:{key}" "1" "m:{member}"'
        lines.append(f'{i+1}.000000 [0 127.0.0.1:12345] {command}\n')
    return ''.join(lines)


# Same complete-member/order/score-conservation checks as the previous comparison,
# parameterized for the larger key count. Hot runs retain the 64-set preload.
CHECK = r'''import socket,sys,json
host,mode=sys.argv[1:3];boards,members=map(int,sys.argv[3:5]);sock=socket.create_connection((host,6379),30);sock.settimeout(120);f=sock.makefile('rb')
def reply():
 line=f.readline();assert line,line
 kind=line[:1];assert kind!=b'-',line
 if kind==b'*':return [reply() for _ in range(int(line[1:]))]
 if kind==b'$':
  n=int(line[1:]);assert n>=0,line
  data=f.read(n);assert f.read(2)==b'\r\n';return data.decode()
 return line[1:].decode().strip()
def ask(parts):
 parts=[str(x).encode() for x in parts]
 sock.sendall(b'*'+str(len(parts)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in parts));return reply()
if mode=='preload':
 for b in range(boards):
  for start in range(0,members,128):
   args=['ZADD','lb:'+str(b)]
   for m in range(start,min(start+128,members)):args+=['0','m:'+str(m)]
   assert int(ask(args))==min(128,members-start)
 assert float(ask(['ZINCRBY','lb:0','1','m:0']))==1
 assert float(ask(['ZINCRBY','lb:0','-1','m:0']))==0
cards=[];score_sum=0
for b in range(boards):
 card=int(ask(['ZCARD','lb:'+str(b)]));assert card==members,card;cards.append(card)
 values=ask(['ZRANGE','lb:'+str(b),'0','-1','WITHSCORES']);assert len(values)==2*members
 assert set(values[::2])=={'m:'+str(m) for m in range(members)}
 scores=[float(x) for x in values[1::2]];assert all(x>=0 and x.is_integer() for x in scores)
 pairs=list(zip(values[::2],scores));assert pairs==sorted(pairs,key=lambda x:(x[1],x[0])), 'Incorrect sorted order'
 for rank in (0,members//2,members-1):assert int(ask(['ZRANK','lb:'+str(b),values[rank*2]]))==rank
 score_sum+=sum(int(x) for x in scores)
expected=0 if mode=='preload' else int(sys.argv[5]);assert score_sum==expected,(score_sum,expected)
print(json.dumps({'cardinalities':cards,'score_sum':score_sum,'expected':expected}))
'''


def prepare(o):
    o.output.mkdir(parents=True, exist_ok=False)
    old = json.loads((b.SOURCE/'resources.json').read_text())['items'][0]['metadata']['name']
    for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
        obj = json.loads((b.SOURCE/name).read_text().replace(old, o.namespace).replace('14400', '21600'))
        if name == 'resources.json':
            for item in obj['items'][1:]:
                item['spec']['template']['spec']['expireAfter'] = '6h'
                for req in item['spec']['template']['spec']['requirements']:
                    if req['key'] == 'topology.kubernetes.io/zone': req['values'] = [o.zone]
                    if req['key'] == 'node.kubernetes.io/instance-type' and item['metadata']['name'].endswith('-server'):
                        req['values'] = [b.SERVER_TYPE]
        b.save(o.output/name, obj)
    o.template = json.loads((b.SOURCE/'server-candidate-16.json').read_text().replace(old, o.namespace))
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    binary = o.output/'frozen/static-after/bin/vex'
    binary.parent.mkdir(parents=True)
    shutil.copy2(o.binary, binary)
    b.BUILD = o.output/'frozen'
    order = []
    for si, shape in enumerate(SHAPES):
        cpus = [4, 8, 16] if si % 2 == 0 else [16, 8, 4]
        for ci, cpu in enumerate(cpus):
            counts = COUNTS[ci:] + COUNTS[:ci]
            if si % 2: counts = list(reversed(counts))
            order.extend(dict(shape=shape, cpu=cpu, partitions=p) for p in counts)
    protocol = dict(server=b.SERVER_TYPE, client='c7g.16xlarge', zone=o.zone,
        cpu_quotas=[4,8,16], partitions=COUNTS, shapes=SHAPES, order=order,
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        workers='equal CPU quota; unpinned', memory='8Gi', persistence=False,
        arrival_model='uncapped closed-loop; 32 client threads x 4 connections; pipeline 16',
        warmup_seconds=10, repeats=3, measure_seconds=60, primary_budget_ms=5,
        mixed='80% ZINCRBY / 20% ZRANK', client_gate='each thread <0.9 cores; aggregate <38.4 cores; throttling <1% of duration',
        network_gate='no increase in ENA allowance-exceeded counters', node_lifetime='6h maximum; explicit cleanup in finally')
    b.save(o.output/'protocol.json', protocol)
    source = o.output/'source';source.mkdir()
    for path in pathlib.Path(__file__).parent.glob('*.py'): shutil.copy2(path, source/path.name)
    for name in ('src/main.zig', 'src/config.zig', 'src/engine/types/sorted_set.zig', 'src/server/tcp.zig'):
        path = source/name;path.parent.mkdir(parents=True, exist_ok=True);shutil.copy2(b.ROOT/name, path)
    b.save(o.output/'check.py', CHECK)
    return protocol


def resume(o):
    protocol = json.loads((o.output/'protocol.json').read_text())
    resources = json.loads((o.output/'resources.json').read_text())
    assert resources['items'][0]['metadata']['name'] == o.namespace
    assert protocol['zone'] == o.zone
    b.BUILD = o.output/'frozen'
    binary = b.BUILD/'static-after/bin/vex'
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == protocol['binary_sha256']
    # Refuse changed machines before deleting/restarting any benchmark process.
    initial = json.loads((o.output/'nodes.json').read_text())
    current = json.loads(s.kub('get','nodes','-l','bench.vex.dev/run='+o.namespace,'-o','json'))
    identity = lambda data: {(x['metadata']['uid'],x['status']['nodeInfo']['bootID'],x['spec']['providerID']) for x in data['items']}
    assert identity(current) == identity(initial), 'benchmark hosts changed; use a new run'
    for kind,name in [('namespace',o.namespace),('nodepool',o.namespace+'-server'),('nodepool',o.namespace+'-client')]:
        obj=json.loads(s.kub('get',kind,name,'-o','json'))
        assert obj['metadata']['labels']['bench.vex.dev/run']==o.namespace
        assert not obj['metadata'].get('deletionTimestamp')
    old = json.loads((b.SOURCE/'resources.json').read_text())['items'][0]['metadata']['name']
    o.template=json.loads((b.SOURCE/'server-candidate-16.json').read_text().replace(old,o.namespace))
    o.template['spec']['containers'][0]['image']=o.template['spec']['containers'][1]['image']
    stamp=str(time.time_ns())
    source=o.output/('resume-source-'+stamp);source.mkdir()
    for path in pathlib.Path(__file__).parent.glob('*.py'): shutil.copy2(path,source/path.name)
    for cell in protocol['order']:
        path=o.output/f"{cell['shape']}-c{cell['cpu']}-p{cell['partitions']}"
        if path.exists() and not (path/'complete.json').exists():
            archive=o.output/'interrupted';archive.mkdir(exist_ok=True)
            path.rename(archive/(path.name+'-'+stamp))
    return protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--binary', type=pathlib.Path, required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--zone', default='ap-south-1b')
    parser.add_argument('--resume', action='store_true')
    o = parser.parse_args();o.output = o.output.resolve();o.binary = o.binary.resolve()
    assert o.namespace.startswith('vex-scaling-')
    assert json.loads(s.run(['aws','sts','get-caller-identity','--output','json']).stdout)['Account']=='208168340597'
    s.BASE,s.NS=o.output,o.namespace;s.K=['kubectl','--context',s.CTX,'-n',o.namespace]
    if o.resume:
        protocol=resume(o)
    else:
        for kind,name in [('namespace',o.namespace),('nodepool',o.namespace+'-server'),('nodepool',o.namespace+'-client')]:
            assert not s.kub('get',kind,name,'--ignore-not-found','-o','name').strip()
        protocol=prepare(o)
    try:
        if o.resume:
            # A fresh client guarantees that no detached interrupted load survives.
            s.kub('delete','pod','client','--ignore-not-found','--wait=true','--timeout=60s')
            s.kub('create','-f',str(o.output/'client-48.json'))
        else:
            for name in ('resources.json','holder.manifest.json','client-48.json'): s.kub('create','-f',str(o.output/name))
        s.kub('wait','--for=condition=Ready','pod/client','--timeout=600s',timeout=610)
        assert s.memtier_hash()==s.MEMTIER_SHA256
        s.kub('cp',str(o.output/'check.py'),'client:/results/check.py','-c','memtier')
        for number,cell in enumerate(protocol['order'],1):
            cpu,partitions,shape=cell['cpu'],cell['partitions'],cell['shape']
            boards,members,hot=SHAPES[shape]
            out=o.output/f'{shape}-c{cpu}-p{partitions}'
            if (out/'complete.json').exists():
                assert o.resume
                print('RETAIN',number,len(protocol['order']),out.name,flush=True)
                continue
            out.mkdir()
            print('START',number,len(protocol['order']),out.name,flush=True)
            host,engine,topology,instances=b.start(o,cpu,'after',out,partitions=partitions)
            logs=s.kub('logs','server','-c','engine');b.save(out/'startup.log',logs)
            assert f'sorted-set-partitions={partitions}' in logs
            path=out/'mixed.trace';contents=trace(boards,members,hot);b.save(path,contents)
            s.kub('cp',str(path),'client:/results/mixed.trace','-c','memtier')
            assert s.ex('client','memtier','sha256sum','/results/mixed.trace').split()[0]==hashlib.sha256(contents.encode()).hexdigest()
            result=b.remote_job(['python3','/results/check.py',host,'preload',str(boards),str(members)],'/results/preload-'+out.name,600)
            b.save(out/'preload.json',result.stdout);assert result.returncode==0,result.stderr
            before=b.ena(out,'before',instances)
            rows=[b.phase(o,out,host,engine,topology,'mixed','warmup',10,4,16)]
            for repeat in (1,2,3): rows.append(b.phase(o,out,host,engine,topology,'mixed',f'confirm-r{repeat}',60,4,16))
            after=b.ena(out,'after',instances)
            deltas={role:{k:after[role][k]-v for k,v in counters.items()} for role,counters in before.items()}
            result=b.remote_job(['python3','/results/check.py',host,'verify',str(boards),str(members),str(sum(r['writes'] for r in rows))],'/results/verify-'+out.name,600)
            b.save(out/'verify.json',result.stdout);assert result.returncode==0,result.stderr
            b.save(out/'complete.json',dict(**cell,rows=rows,network_deltas=deltas,
                valid=all(r['client_valid'] for r in rows) and all(v==0 for counts in deltas.values() for v in counts.values()),
                pass_5ms=all(r['pass_5ms'] for r in rows[1:])))
            print('COMPLETE',number,len(protocol['order']),out.name,flush=True)
    finally:
        result=s.run(s.K+['delete','-f',str(o.output/'resources.json'),'--ignore-not-found','--wait=false'],check=False)
        b.save(o.output/f'cleanup-request-{time.time_ns()}.log',result.stdout+result.stderr)
        print('CLEANUP',result.returncode,flush=True)


if __name__=='__main__': main()
