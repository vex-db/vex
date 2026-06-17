#!/bin/bash
# CLIENT box: dedicated load generator. The server box SSHes in and runs
# memtier_benchmark against the server box over the network. This box does
# nothing on its own except be ready + hold a safety self-terminate.
exec >/var/log/client.log 2>&1
set -x
# Safety net: self-terminate after 60 min no matter what.
( sleep 3600; shutdown -h now ) &
dnf install -y docker >/dev/null 2>&1
systemctl start docker
usermod -aG docker ec2-user
docker pull redislabs/memtier_benchmark:latest >/dev/null 2>&1
# Authorize the server box's throwaway key for SSH-driven load runs.
install -d -m 700 -o ec2-user -g ec2-user /home/ec2-user/.ssh
echo "PUBKEY_PLACEHOLDER" >> /home/ec2-user/.ssh/authorized_keys
chmod 600 /home/ec2-user/.ssh/authorized_keys
chown ec2-user:ec2-user /home/ec2-user/.ssh/authorized_keys
echo "===CLIENT-READY===" > /dev/console
