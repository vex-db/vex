#!/usr/bin/env python3
"""Local, memory-only Vex/Redis/Dragonfly lifecycle comparison (no host ports)."""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[3]
INVENTORY = ROOT / "bench/loadtest/runs/2026-09-25-three-engine/engines.json"
OUT = ROOT / "bench/loadtest/runs/2026-09-25-memory-lifecycle"
VEX_SAMPLER_IMAGE = "208168340597.dkr.ecr.ap-south-1.amazonaws.com/jarvis/vex@sha256:8b65b2d9aa2f12e0733a43b28580d12f261fb6eb541cc202092d85c7e738be9f"
POINTS = (200_000, 400_000, 600_000, 750_000, 800_000, 825_000, 850_000, 900_000, 1_000_000, 1_200_000, 1_500_000, 1_600_000)
BATCH, PREFIX, FIRST = 1000, "vex:key::", 1_000_000

DRIVER = r'''
import json,socket,sys,time
s=socket.create_connection(('server',6379),10);f=s.makefile('rb')
def reply():
 line=f.readline(); assert line and line[:1]!=b'-',line
 if line[:1]==b'+': return line[1:-2].decode()
 if line[:1]==b':': return int(line[1:])
 if line[:1]==b'$':
  n=int(line[1:]);
  if n<0:return None
  v=f.read(n).decode();assert f.read(2)==b'\r\n';return v
 if line[:1]==b'*':return [reply() for _ in range(int(line[1:]))]
 raise AssertionError(line)
def send(parts): s.sendall(('*'+str(len(parts))+'\r\n'+''.join('$'+str(len(x))+'\r\n'+x+'\r\n' for x in parts)).encode())
def key(i): return 'vex:key::'+str(1000000+i)
def batch(op,a,b,size=256,ttl=None):
 for i in range(a,b): send(([op,key(i),'x'*size] + (['PX',str(ttl)] if ttl else [])) if op=='SET' else [op,key(i)])
 for i in range(a,b):
  r=reply(); assert r == 'OK' if op=='SET' else r == 1,(op,i,r)
def info(): return {'memory':send(['INFO','memory']) or reply(),'stats':send(['INFO','stats']) or reply(),'keyspace':send(['INFO','keyspace']) or reply(),'dbsize':send(['DBSIZE']) or reply()}
for raw in sys.stdin:
 q=json.loads(raw);op=q['op']
 if op in ('set','del','ttlset'):
  for a in range(q['from'],q['to'],1000): batch('SET' if op!='del' else 'DEL',a,min(a+1000,q['to']),q.get('size',256),q.get('ttl'))
  answer={'ok':True}
 elif op=='flush':
  started=time.monotonic_ns();send(['FLUSHDB']);result=reply();answer={'reply':result,'duration_ms':(time.monotonic_ns()-started)/1e6}
 elif op=='info': answer=info()
 elif op=='gets':
  values=[]
  for i in q['ids']: send(['GET',key(i)])
  for i in q['ids']: values.append(reply())
  answer={'values':values,'dbsize':(send(['DBSIZE']) or reply())}
 else: raise AssertionError(op)
 print(json.dumps(answer),flush=True)
'''

SAMPLER = r'''
import hashlib,json,os,pathlib
P=pathlib.Path; name=sys.argv[1]; hashed=sys.argv[2]=='hash'
hits=[]
for p in P('/proc').glob('[0-9]*'):
 try:
  if (p/'comm').read_text().strip()==name:hits.append(p)
 except OSError:pass
assert len(hits)==1,(name,hits)
p=hits[0]; stat=(p/'stat').read_text().rsplit(')',1)[1].split(); st={a:b.strip() for a,b in (x.split(':',1) for x in (p/'status').read_text().splitlines() if ':' in x)}
row={'pid':int(p.name),'start':stat[19],'rss_bytes':int(st['VmRSS'].split()[0])*1024,'hwm_bytes':int(st['VmHWM'].split()[0])*1024,'tasks':[{'tid':int(t.name),'affinity':sorted(os.sched_getaffinity(int(t.name)))} for t in (p/'task').iterdir()]}
if hashed:row['exe_sha256']=hashlib.sha256((p/'exe').read_bytes()).hexdigest()
print(json.dumps(row))
'''


def sh(args, timeout=120, check=True):
    p = subprocess.run(["docker", *map(str, args)], text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"docker {args[:6]}: {p.stderr[-2000:]}")
    return p.stdout + (p.stderr if args[0] == "logs" else "")


def write(path, name, value=None):
    if value is not None: path = path / name
    else: value = name
    assert not path.exists(), f"refusing to overwrite {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")


def engines():
    data = json.loads(INVENTORY.read_text())
    assert set(data) == {"vex", "redis", "dragonfly"}
    return data


def absent(name):
    assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode, f"refusing existing {name}"


def cgroup(server):
    raw = sh(["exec", server, "sh", "-ec", "for x in memory.current memory.max memory.events memory.swap.current cpu.stat; do echo ===$x; cat /sys/fs/cgroup/$x; done"])
    parts, key = {}, None
    for line in raw.splitlines():
        if line.startswith("==="): key=line[3:];parts[key]=[]
        elif key: parts[key].append(line)
    events={a:int(b) for a,b in (x.split() for x in parts["memory.events"])}
    assert parts["memory.max"] == [str(4*1024**3)] and int(parts["memory.swap.current"][0]) == 0 and not any(events.get(x,0) for x in ("max","oom","oom_kill")), (parts,events)
    return raw


def sample(names, engine, hashed=False):
    row=json.loads(sh(["exec", names["sampler"], "python3", "-c", "import sys;" + SAMPLER, engines()[engine]["process_name"], "hash" if hashed else "light"]))
    if hashed: assert row["exe_sha256"] == engines()[engine]["binary_sha256"], row
    return row


def image_arch(image):
    value=sh(["image","inspect",image,"--format","{{.Architecture}} "]).strip()
    assert value == "arm64", (image,value)
    return value


def start(engine, tag):
    names = {k: f"vex-memory-{tag}-{k}" for k in ("net", "server", "client", "sampler")}
    for name in names.values(): absent(name)
    try:
        assert image_arch(engines()[engine]["image"]) == image_arch(VEX_SAMPLER_IMAGE) == "arm64"
        sh(["network", "create", names["net"]])
        env = ["-e", "VEX_SQ_HEAD_CACHE=0"] if engine == "vex" else []
        command = engines()[engine]["command"]
        sh(["run", "-d", "--name", names["server"], "--network", names["net"], "--network-alias", "server", "--cpus", "6", "--memory", "4g", "--memory-swap", "4g", "--security-opt", "seccomp=unconfined", *env, "--entrypoint", command[0], engines()[engine]["image"], *command[1:]])
        sh(["run", "-d", "--name", names["client"], "--network", names["net"], "--entrypoint", "sleep", VEX_SAMPLER_IMAGE, "3600"])
        sh(["run", "-d", "--name", names["sampler"], "--pid", f"container:{names['server']}", "--entrypoint", "sleep", VEX_SAMPLER_IMAGE, "3600"])
        for _ in range(60):
            try:
                sh(["exec", names["client"], "python3", "-c", "import socket;s=socket.create_connection(('server',6379),1);s.close()"], timeout=5); break
            except RuntimeError: time.sleep(1)
        else: raise RuntimeError("server did not become ready")
        return names, sample(names,engine,True)
    except Exception:
        stop(names); raise


def stop(names):
    for key in ("sampler", "client", "server", "net"):
        sh(["rm", "-f", names[key]] if key != "net" else ["network", "rm", names[key]], check=False)


def driver(names):
    return subprocess.Popen(["docker", "exec", "-i", names["client"], "python3", "-u", "-c", DRIVER], text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def ask(proc, payload):
    proc.stdin.write(json.dumps(payload) + "\n"); proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, "client driver ended"
    return json.loads(line)


def snapshot(out, label, names, engine, proc, idle_seconds=2, elapsed=None):
    if idle_seconds: time.sleep(idle_seconds)
    info = ask(proc, {"op":"info"})
    identity = sample(names,engine)
    assert sh(["inspect","--format","{{.RestartCount}}",names["server"]]).strip() == "0"
    record = {"label":label,"idle_seconds":idle_seconds,"elapsed_since_ttl_load_seconds":elapsed,"info":info,"process":identity,"cgroup_raw":cgroup(names["server"])}
    write(out / f"{label}.json", record)
    return record


def spotcheck(out, label, proc, ids, size):
    result=ask(proc,{"op":"gets","ids":ids})
    assert result["values"] == ["x"*size]*len(ids), (label,[len(x) if x else None for x in result["values"]])
    write(out/f"{label}-spotcheck.json",result)


def grow(out, names, engine, proc):
    current = 0
    for point in POINTS:
        ask(proc,{"op":"set","from":current,"to":point,"size":256}); current=point
        row=snapshot(out,f"grow-{point}",names,engine,proc); assert row["info"]["dbsize"]==point
        spotcheck(out,f"grow-{point}",proc,[0,point//2,point-1],256)
    for point in (800_000,200_000,0):
        ask(proc,{"op":"del","from":point,"to":current}); current=point
        row=snapshot(out,f"delete-{point}-immediate",names,engine,proc,0); assert row["info"]["dbsize"]==point
        row=snapshot(out,f"delete-{point}-5s",names,engine,proc,5); assert row["info"]["dbsize"]==point
    ask(proc,{"op":"set","from":0,"to":200_000,"size":256}); snapshot(out,"refill-200000",names,engine,proc); spotcheck(out,"refill-200000",proc,[0,100000,199999],256)
    flushed=ask(proc,{"op":"flush"}); assert flushed["reply"]=="OK"; write(out,"flush-command.json",flushed)
    row=snapshot(out,"flush-immediate",names,engine,proc,0); assert row["info"]["dbsize"]==0
    snapshot(out,"flush-5s",names,engine,proc,5)


def overwrite(out, names, engine, proc):
    for size in (4096,2048,128,32):
        ask(proc,{"op":"set","from":0,"to":100_000,"size":size}); row=snapshot(out,f"overwrite-{size}",names,engine,proc); assert row["info"]["dbsize"]==100_000
        spotcheck(out,f"overwrite-{size}",proc,[0,50000,99999],size)
    flushed=ask(proc,{"op":"flush"}); assert flushed["reply"]=="OK"; write(out,"flush-command.json",flushed)
    snapshot(out,"flush-immediate",names,engine,proc,0); snapshot(out,"flush-5s",names,engine,proc,5)


def ttl(out, names, engine, proc):
    ask(proc,{"op":"ttlset","from":0,"to":100_000,"size":256,"ttl":2000}); loaded=time.monotonic(); snapshot(out,"ttl-set",names,engine,proc)
    time.sleep(max(0,5-(time.monotonic()-loaded))); snapshot(out,"ttl-5s",names,engine,proc,0,elapsed=time.monotonic()-loaded)
    values=ask(proc,{"op":"gets","ids":[0,1,49999,99999]}); assert values["values"] == [None]*4,values; write(out,"ttl-5s-gets.json",values)
    time.sleep(max(0,30-(time.monotonic()-loaded))); snapshot(out,"ttl-30s",names,engine,proc,0,elapsed=time.monotonic()-loaded)
    values=ask(proc,{"op":"gets","ids":[0,1,49999,99999]}); assert values["values"] == [None]*4,values; write(out,"ttl-30s-gets.json",values)


def run_case(engine, case):
    out=OUT/engine/case; assert not out.exists(),out; out.mkdir(parents=True)
    tag=f"{engine}-{case}-20260925"; names=None
    try:
        write(out,"engine.json",engines()[engine]); write(out,"method.json",{"memory_only":True,"client_connection":"one persistent RESP connection for the complete case","batch_size":BATCH,"key_prefix":PREFIX,"key_bytes":16,"default_value_bytes":256,"idle_after_growth_seconds":2,"no_host_ports":True}); names,initial=start(engine,tag); write(out,"initial-process.json",initial); startup=sh(["logs",names["server"]]); write(out,"startup.log",startup); assert engine!="vex" or "io_uring backend active" in startup, startup
        proc=driver(names); {"grow":grow,"overwrite":overwrite,"ttl":ttl}[case](out,names,engine,proc)
        proc.stdin.close(); assert proc.wait(timeout=30)==0
        final=sample(names,engine,True); assert {k:final[k] for k in ("pid","start","exe_sha256")} == {k:initial[k] for k in ("pid","start","exe_sha256")}; write(out,"final-process.json",final)
    except Exception as exc:
        if not (out/"failure.json").exists(): write(out,"failure.json",{"engine":engine,"case":case,"reason":str(exc)})
        raise
    finally:
        if names: stop(names)


def self_test():
    compile(DRIVER,"driver","exec"); compile("import sys;"+SAMPLER,"sampler","exec")
    assert POINTS == tuple(sorted(POINTS)) and all(len(PREFIX+str(FIRST+n))==16 for n in (0,1_599_999))
    scope={};exec("import io\nf=io.BytesIO(b'$1\\r\\na\\r\\n$1\\r\\nb\\r\\n')\ndef reply():\n line=f.readline();n=int(line[1:]);v=f.read(n).decode();assert f.read(2)==b'\\r\\n';return v\nassert [reply(),reply()]==['a','b']",scope)
    assert (POINTS[-1],800_000,200_000,0)==(1_600_000,800_000,200_000,0)
    with tempfile.TemporaryDirectory() as directory:
        path=pathlib.Path(directory); write(path,"nested.json",{"ok":True}); assert json.loads((path/"nested.json").read_text())["ok"]
    assert "('server',6379)" in DRIVER and '"--network-alias", "server"' in HERE.read_text()
    print("memory lifecycle RESP and sequence self-test passed")


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--engine",choices=["vex","redis","dragonfly","all"],default="all"); parser.add_argument("--case",choices=["grow","overwrite","ttl","all"],default="all"); parser.add_argument("--self-test",action="store_true");args=parser.parse_args()
    if args.self_test:return self_test()
    for engine in ([args.engine] if args.engine!="all" else ["vex","redis","dragonfly"]):
        for case in ([args.case] if args.case!="all" else ["grow","overwrite","ttl"]): run_case(engine,case)


if __name__=="__main__": main()
