#!/bin/bash
# PRODUCTION SERVER box: runs vex/dragonfly/redis one at a time pinned to N
# cores (rest idle). Drives load from MULTIPLE client boxes in parallel
# (throughput summed), preloads the keyspace before GET tests, and samples
# the pinned cores' CPU during each cell to prove the SERVER is the
# bottleneck. Emits CSV to console, then tears down all clients + itself.
exec >/var/log/server.log 2>&1
set -x
( sleep 4200; echo "===SAFETY-TIMEOUT===" > /dev/console; shutdown -h now ) &  # 70-min safety net

CLIENT_IPS="CLIENT_IPS_PLACEHOLDER"
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 600")
SERVER_IP=$(curl -s -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" http://169.254.169.254/latest/meta-data/local-ipv4)
install -d -m 700 /root/.ssh
echo "PRIVKEY_B64_PLACEHOLDER" | base64 -d > /root/.ssh/id_rsa
chmod 600 /root/.ssh/id_rsa
SSHOPT="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -i /root/.ssh/id_rsa"
MT="redislabs/memtier_benchmark:latest"
KMAX=2000000

dnf install -y docker >/dev/null 2>&1
systemctl start docker
docker pull redis:8.0.3 >/dev/null 2>&1
docker pull docker.dragonflydb.io/dragonflydb/dragonfly:latest >/dev/null 2>&1
docker pull ghcr.io/pratyush-sngh/vex:latest >/dev/null 2>&1

# Wait for every client: SSH reachable + memtier image present.
for cip in $CLIENT_IPS; do
  for i in $(seq 1 120); do
    ssh $SSHOPT ec2-user@"$cip" "docker image inspect $MT >/dev/null 2>&1" && break
    sleep 5
  done
done
echo "===CLIENTS-LINK-UP===" > /dev/console

NCLI=$(echo $CLIENT_IPS | wc -w)
R=/tmp/results.csv
echo "cores,server,cmd,pipeline,ops,server_cpu_pct,n_clients" > "$R"

wait_ready(){ for i in $(seq 1 80); do docker run --rm --network host redis:8.0.3 redis-cli -p "$1" ping 2>/dev/null | grep -q PONG && return 0; sleep 0.5; done; return 1; }

# Sum busy + total jiffies over cores 0..N-1 -> "busy total"
cpu_snapshot(){
  local n=$1 busy=0 tot=0 c
  for c in $(seq 0 $((n-1))); do
    set -- $(awk -v k="cpu$c" '$1==k{print}' /proc/stat)
    local b=$(( $2 + $3 + $4 + ${7:-0} + ${8:-0} + ${9:-0} ))
    local idl=$(( $5 + ${6:-0} ))
    busy=$((busy + b)); tot=$((tot + b + idl))
  done
  echo "$busy $tot"
}

# Run memtier on ALL clients in parallel, sum ops/sec; sample server CPU.
run_cell(){ # n port pipeline ratio -> "sum_ops cpu_pct"
  local n=$1 port=$2 p=$3 ratio=$4 i=0 cip
  read b0 t0 < <(cpu_snapshot "$n")
  for cip in $CLIENT_IPS; do
    i=$((i+1))
    ssh $SSHOPT ec2-user@"$cip" "docker run --rm --network host $MT -s $SERVER_IP -p $port -t 16 -c 64 --pipeline $p --ratio $ratio --key-maximum $KMAX --test-time 20 --hide-histogram 2>/dev/null | grep -iE '^Totals' | awk '{print \$2}' | head -1" > /tmp/cli_$i.out 2>/dev/null &
  done
  wait
  read b1 t1 < <(cpu_snapshot "$n")
  local sum=0 v f
  for f in /tmp/cli_*.out; do v=$(cat "$f" 2>/dev/null); v=${v%%.*}; sum=$((sum + ${v:-0})); done
  rm -f /tmp/cli_*.out
  local cpu="?"; [ $((t1 - t0)) -gt 0 ] && cpu=$(( 100 * (b1 - b0) / (t1 - t0) ))
  echo "$sum $cpu"
}

# Preload KMAX keys (SETs from the first client) so GETs hit real data.
preload(){
  local first=$(echo $CLIENT_IPS | awk '{print $1}')
  ssh $SSHOPT ec2-user@"$first" "docker run --rm --network host $MT -s $SERVER_IP -p $1 -t 16 -c 64 --ratio 1:0 -n $((KMAX/1024 + 200)) --key-maximum $KMAX --key-pattern P:P --hide-histogram >/dev/null 2>&1"
}

bench(){ # name port n cmd pipeline
  local name=$1 port=$2 n=$3 cmd=$4 p=$5 ratio
  [ "$cmd" = "set" ] && ratio="1:0" || ratio="0:1"
  read ops cpu < <(run_cell "$n" "$port" "$p" "$ratio")
  echo "$n,$name,$cmd,$p,${ops:-ERR},${cpu:-?},$NCLI" >> "$R"
  echo "done $name cores=$n $cmd P=$p -> ops=${ops:-ERR} server_cpu=${cpu:-?}%" > /dev/console
}

start_vex(){ docker run -d --name srv --network host --cpuset-cpus="0-$(($1-1))" ghcr.io/pratyush-sngh/vex:latest --reactor --workers "$1" --no-persistence --port 6380 >/dev/null 2>&1; }
start_df(){  docker run -d --name srv --network host --cpuset-cpus="0-$(($1-1))" docker.dragonflydb.io/dragonflydb/dragonfly:latest --proactor_threads="$1" --port 6379 >/dev/null 2>&1; }

for N in 4 8 16 32 48; do
  for spec in "vex 6380" "dragonfly 6379"; do
    set -- $spec; name=$1; port=$2
    docker rm -f srv >/dev/null 2>&1
    if [ "$name" = "vex" ]; then start_vex "$N"; else start_df "$N"; fi
    if wait_ready "$port"; then
      preload "$port"
      for c in set get; do for p in 1 30; do bench "$name" "$port" "$N" "$c" "$p"; done; done
    else echo "$name N=$N FAILED-START" > /dev/console; fi
    docker rm -f srv >/dev/null 2>&1
  done
done
# redis single-thread baseline
docker run -d --name srv --network host --cpuset-cpus="0" redis:8.0.3 redis-server --port 6381 --save "" --appendonly no >/dev/null 2>&1
if wait_ready 6381; then preload 6381; for c in set get; do for p in 1 30; do bench redis 6381 1 "$c" "$p"; done; done; fi
docker rm -f srv >/dev/null 2>&1

for rep in 1 2 3 4 5; do
  echo "===VEXBENCH-RESULTS-START===" > /dev/console
  cat "$R" > /dev/console
  echo "===VEXBENCH-RESULTS-END===" > /dev/console
  sleep 25
done
for cip in $CLIENT_IPS; do ssh $SSHOPT ec2-user@"$cip" "sudo shutdown -h now" >/dev/null 2>&1; done
sleep 30
shutdown -h now
