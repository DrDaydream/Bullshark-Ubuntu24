# Bullshark-Ubuntu24 在 AWS 部署 10/20/50 节点

每台 EC2 运行一个 Primary、一个 Worker 和一个 benchmark client；node-0 同时作为控制机。所有协议流量使用 AWS Private IPv4。

## 1. AWS 控制台

1. EC2 → Security Groups → Create security group，名称 `bullshark-sg`。
2. 入站 TCP 22 允许你的公网 IP `/32`。
3. 入站 TCP 22 和 TCP `3000-3004` 的 Source 选择 `bullshark-sg` 自身。
4. 出站保留 All traffic，不要将 3000-3004 开放给 `0.0.0.0/0`。
5. 创建 ED25519 key pair `bullshark-aws.pem`。
6. 启动 10、20 或 50 台 Ubuntu Server 24.04 x86_64 EC2，全部使用同一 VPC、同一可用区和安全组。建议至少 4 vCPU/16 GiB/30 GiB gp3。

端口：3000 Primary↔Primary，3001 Worker→Primary，3002 Primary→Worker，3003 Client→Worker，3004 Worker↔Worker。

## 2. 准备 node-0

在本地电脑执行：

```bash
chmod 400 ~/Downloads/bullshark-aws.pem
scp -i ~/Downloads/bullshark-aws.pem ~/Downloads/bullshark-aws.pem ubuntu@NODE0_PUBLIC_IP:/home/ubuntu/.ssh/bullshark-aws.pem
ssh -i ~/Downloads/bullshark-aws.pem ubuntu@NODE0_PUBLIC_IP
chmod 400 ~/.ssh/bullshark-aws.pem
```

在 node-0 克隆仓库，并为对应规模创建 hosts 文件：

```bash
git clone https://github.com/DrDaydream/Bullshark-Ubuntu24.git
cd Bullshark-Ubuntu24
cp deploy/hosts-10.txt.example deploy/hosts-10.txt
nano deploy/hosts-10.txt
```

每行一个私网 IP，第一行必须是 node-0。20/50 节点分别创建 `deploy/hosts-20.txt` 和 `deploy/hosts-50.txt`。

## 3. 安装并编译所有节点

以 10 节点为例：

```bash
while read -r ip; do
  ssh -i ~/.ssh/bullshark-aws.pem ubuntu@$ip 'bash -s' <<'REMOTE' &
set -Eeuo pipefail
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential cmake clang-14 libclang-14-dev git curl tmux python3 python3-pip chrony
sudo systemctl enable --now chrony
test -x "$HOME/.cargo/bin/cargo" || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
if test -d "$HOME/Bullshark-Ubuntu24/.git"; then
  git -C "$HOME/Bullshark-Ubuntu24" pull --ff-only
else
  git clone https://github.com/DrDaydream/Bullshark-Ubuntu24.git "$HOME/Bullshark-Ubuntu24"
fi
cd "$HOME/Bullshark-Ubuntu24"
LIBCLANG_PATH=/usr/lib/llvm-14/lib CLANG_PATH=/usr/bin/clang-14 CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 CXXFLAGS='-include cstdint' cargo build --release --features benchmark
REMOTE
done < deploy/hosts-10.txt
wait
```

## 4. 生成密钥和 committee

```bash
cd ~/Bullshark-Ubuntu24
NODES=10
HOSTS=deploy/hosts-10.txt
mkdir -p deploy
for ((i=0;i<NODES;i++)); do ./target/release/node generate_keys --filename deploy/node-$i.json; done
python3 - "$HOSTS" "$NODES" <<'PY'
import json, sys
from pathlib import Path
ips=[x.strip() for x in Path(sys.argv[1]).read_text().splitlines() if x.strip() and not x.startswith('#')]
n=int(sys.argv[2]); assert len(ips)==n and len(set(ips))==n
a={}
for i,ip in enumerate(ips):
    key=json.loads(Path(f'deploy/node-{i}.json').read_text())
    a[key['name']]={'primary':{'primary_to_primary':f'{ip}:3000','worker_to_primary':f'{ip}:3001'},'stake':1,'workers':{'0':{'primary_to_worker':f'{ip}:3002','transactions':f'{ip}:3003','worker_to_worker':f'{ip}:3004'}}}
Path('deploy/committee.json').write_text(json.dumps({'authorities':a},indent=4))
p={'header_size':1000,'max_header_delay':2000,'gc_depth':50,'sync_retry_delay':10000,'sync_retry_nodes':3,'batch_size':500000,'max_batch_delay':200}
Path('deploy/parameters.json').write_text(json.dumps(p,indent=4))
PY
mapfile -t IPS < <(awk 'NF && $1 !~ /^#/ {print $1}' "$HOSTS")
for ((i=0;i<NODES;i++)); do
  ssh -i ~/.ssh/bullshark-aws.pem ubuntu@${IPS[$i]} 'mkdir -p ~/Bullshark-Ubuntu24/deploy'
  scp -i ~/.ssh/bullshark-aws.pem deploy/node-$i.json deploy/committee.json deploy/parameters.json ubuntu@${IPS[$i]}:Bullshark-Ubuntu24/deploy/
done
```

切换 20/50 节点时，修改 `NODES` 和 `HOSTS`，然后重新生成并分发全部密钥/committee。

## 5. 运行

```bash
cd ~/Bullshark-Ubuntu24
chmod +x run-multi-servers.sh
./run-multi-servers.sh 10 20 10000
./run-multi-servers.sh 20 60 10000
./run-multi-servers.sh 50 60 10000
```

可以通过 `SSH_KEY`、`REMOTE_USER`、`REMOTE_DIR` 和 `HOSTS_FILE` 环境变量覆盖默认值。日志会下载到 `benchmark/logs`并自动解析 TPS/延迟。

如果出现 `NoneType has no attribute group`，先检查所有 client 日志是否包含 `Start sending transactions`，并检查每台 Worker 的 3003 端口。测试后及时 Stop/Terminate EC2，并检查遗留 EBS 和 Elastic IP 费用。
