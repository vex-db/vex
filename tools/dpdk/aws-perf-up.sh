#!/usr/bin/env bash
# Bring up a c5n.18xlarge (or override) DPDK perf-testing instance on
# AWS. User-data on first boot installs DPDK + libdpdk-dev + hugepages
# so the box is ready to copy a binary onto and run.
#
# What this script does NOT do:
#   - Bind a secondary ENA NIC to vfio-pci. That's manual and
#     fragile (varies by kernel / distro / instance type). See
#     "Next steps" in the printed output and ../../docs/dpdk.md.
#
# Required prerequisites:
#   - aws CLI configured (aws sts get-caller-identity should work)
#   - An SSH keypair already in $AWS_REGION (set $KEY_NAME)
#   - A security group allowing inbound SSH (set $SECURITY_GROUP_ID)
#
# Cost reminder: on-demand c5n.18xlarge is ~$3.88/hr. Spot brings
# that to ~$1.50/hr. Tear down with aws-perf-down.sh when done.

set -euo pipefail

INSTANCE_TYPE=${INSTANCE_TYPE:-c5n.18xlarge}
REGION=${AWS_REGION:-ap-south-1}
KEY_NAME=${KEY_NAME:-vex-dpdk}
SECURITY_GROUP_ID=${SECURITY_GROUP_ID:?must export SECURITY_GROUP_ID for SSH inbound}
SUBNET_ID=${SUBNET_ID:-}
NAME_TAG=${NAME_TAG:-vex-dpdk-perf}

# Default to Canonical's latest Ubuntu 22.04 LTS amd64 AMI in the
# region. Override via $AMI if you need a different distro.
if [[ -z "${AMI:-}" ]]; then
    echo "[aws-perf-up] resolving latest Ubuntu 22.04 LTS AMI in $REGION..."
    AMI=$(aws ec2 describe-images --region "$REGION" \
        --owners 099720109477 \
        --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
                  "Name=state,Values=available" \
        --query "sort_by(Images, &CreationDate)[-1].ImageId" \
        --output text)
    if [[ -z "$AMI" || "$AMI" == "None" ]]; then
        echo "ERR: could not resolve Ubuntu 22.04 AMI in $REGION" >&2
        exit 1
    fi
fi

# user-data: installs DPDK + reserves 1024 × 2MB hugepages (~2GB) on
# NUMA node 0 at first boot. The file at /var/log/vex-dpdk-bootstrap.done
# tells you it's finished — useful for scripts that need to wait.
read -r -d '' USER_DATA <<'EOF' || true
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    dpdk \
    dpdk-dev \
    libdpdk-dev \
    libmd-dev \
    pkg-config \
    pciutils
# Hugepages.
echo 1024 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
mkdir -p /mnt/huge
echo "nodev /mnt/huge hugetlbfs defaults 0 0" >> /etc/fstab
mount -a || true
# Place for binaries.
mkdir -p /home/ubuntu/dpdk-bench
chown ubuntu:ubuntu /home/ubuntu/dpdk-bench
echo "DPDK ready" > /var/log/vex-dpdk-bootstrap.done
EOF

echo "[aws-perf-up] launching $INSTANCE_TYPE in $REGION (AMI=$AMI)"
INSTANCE_ID=$(aws ec2 run-instances \
    --region "$REGION" \
    --image-id "$AMI" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SECURITY_GROUP_ID" \
    ${SUBNET_ID:+--subnet-id "$SUBNET_ID"} \
    --user-data "$USER_DATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME_TAG},{Key=purpose,Value=dpdk-bench}]" \
    --query "Instances[0].InstanceId" \
    --output text)

echo "[aws-perf-up] $INSTANCE_ID launched — waiting for running..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$(aws ec2 describe-instances --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --query "Reservations[0].Instances[0].PublicIpAddress" \
    --output text)

cat <<EOM

[aws-perf-up] ready
  instance_id: $INSTANCE_ID
  public_ip:   $PUBLIC_IP
  region:      $REGION

# wait for user-data (DPDK install + hugepages) to finish:
ssh -i ~/.ssh/$KEY_NAME ubuntu@$PUBLIC_IP \\
    'until sudo test -f /var/log/vex-dpdk-bootstrap.done; do sleep 2; done; echo ready'

# copy a probe binary onto the box:
docker save vex-dpdk-probe:hello | bzip2 \\
    | ssh -i ~/.ssh/$KEY_NAME ubuntu@$PUBLIC_IP \\
        'bzcat | docker load'

# tear down when done:
INSTANCE_ID=$INSTANCE_ID AWS_REGION=$REGION ./tools/dpdk/aws-perf-down.sh

# Next steps: bind a secondary ENI to vfio-pci so the DPDK probe can
# attach the NIC. See docs/dpdk.md §Test plan.
EOM
