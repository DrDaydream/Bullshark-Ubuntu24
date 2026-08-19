# Bullshark-Ubuntu24：AWS EC2 10 / 20 / 50 节点完整部署

本文对应当前仓库的 `run-multi-servers.sh`。每台 EC2 运行一个 Primary、一个 Worker 和一个 benchmark client；node-0 兼任控制机和第 0 个协议节点。所有 committee 地址必须使用 Private IPv4。

## 1. 资源规划

| 项目 | 值 |
|---|---|
| AMI | Ubuntu Server 24.04 LTS，x86_64 |
| 节点数 | 10、20 或 50 |
| 登录用户 | `ubuntu` |
| 项目目录 | `/home/ubuntu/Bullshark-Ubuntu24` |
| 仓库 | `https://github.com/DrDaydream/Bullshark-Ubuntu24.git` |
| 推荐实例 | 至少 4 vCPU / 16 GiB，50 节点建议 8 vCPU |
| 磁盘 | 至少 30 GiB gp3 |
| 控制机 | node-0，同时参与协议 |
| 网络 | 同一 Region、同一 VPC，建议同一 AZ |

协议要求 `n >= 3f+1`。10/20/50 节点建议最大敌手数分别为 3、6、16。第一次先运行 10 节点、20 秒、10,000 总 TPS。50 节点前在 AWS Service Quotas 检查 On-Demand vCPU 配额。

## 2. AWS 控制台和安全组

1. AWS Console -> EC2 -> Security Groups -> Create security group。
2. 名称填写 `bullshark-sg`，选择集群 VPC。
3. EC2 -> Key Pairs 创建 ED25519 密钥 `bullshark-aws.pem`。
4. Launch instances，选择 Ubuntu 24.04 x86_64、相同 VPC/子网/安全组。
5. Number of instances 填 10、20 或 50；建议同一实例类型、同一 AZ、30 GiB gp3。
6. 实例 2/2 status checks 通过后，命名为 `bullshark-node-0` 至 `bullshark-node-N-1`。

入站规则：

| 协议/端口 | Source | 用途 |
|---|---|---|
| TCP 22 | 你的公网 IP /32 | 从本地登录 |
| TCP 22 | `bullshark-sg` 自身 | node-0 私网 SSH |
| TCP 3000-3004 | `bullshark-sg` 自身 | 集群协议通信 |

协议端口不要开放给 `0.0.0.0/0`。

| 端口 | 用途 |
|---:|---|
| 3000 | Primary <-> Primary |
| 3001 | Worker -> Primary |
| 3002 | Primary -> Worker |
| 3003 | Client -> Worker |
| 3004 | Worker <-> Worker |

## 2.1 五大洲跨 Region 部署

单 Region/VPC 可使用安全组自身引用。五大洲测试可在 5 个 Region 各放置 2/4/10 台，对应 10/20/50 节点，例如 `us-east-1`、`sa-east-1`、`eu-west-2`、`ap-southeast-1`、`ap-southeast-2`。

五个 VPC 使用非重叠 CIDR，例如 `10.10.0.0/16` 到 `10.50.0.0/16`。使用 AWS Cloud WAN 或 Transit Gateway inter-Region peering 建立私网互联，并在每个 VPC route table 配置其他四个 CIDR 的双向路由。每个 Region 都需要自己的安全组，入站 TCP 3000-3004 放行五个集群 CIDR，TCP 22 放行你的公网 IP /32 和 node-0 VPC CIDR。

hosts 和 committee 均填写互相可达的 Private IPv4；node-0 必须能私网 SSH 到所有节点。不要一部分使用公网、一部分使用私网。没有私网互联时，可用固定公网/Elastic IP 并逐个 /32 放行 22 和 3000-3004，但攻击面与费用更高。跨 Region 测试需记录节点分布、RTT 和数据传输费。


## 3. node-0 与 SSH

在本地电脑执行：

~~~bash
chmod 400 ~/Downloads/bullshark-aws.pem
scp -i ~/Downloads/bullshark-aws.pem ~/Downloads/bullshark-aws.pem \
  ubuntu@NODE0_PUBLIC_IP:/home/ubuntu/.ssh/bullshark-aws.pem
ssh -i ~/Downloads/bullshark-aws.pem ubuntu@NODE0_PUBLIC_IP
~~~

进入 node-0 后：

~~~bash
chmod 400 ~/.ssh/bullshark-aws.pem
nano ~/.ssh/config
~~~

写入：

~~~sshconfig
Host 10.*
    User ubuntu
    IdentityFile /home/ubuntu/.ssh/bullshark-aws.pem
    StrictHostKeyChecking accept-new
    ConnectTimeout 8
    ServerAliveInterval 5
    ServerAliveCountMax 2
~~~

若 VPC 不是 `10.*`，使用实际网段或 `Host *`。然后：

~~~bash
chmod 600 ~/.ssh/config
git clone https://github.com/DrDaydream/Bullshark-Ubuntu24.git ~/Bullshark-Ubuntu24
cd ~/Bullshark-Ubuntu24
cp deploy/hosts-10.txt.example deploy/hosts-10.txt
nano deploy/hosts-10.txt
~~~

hosts 每行只写一个 Private IPv4，node-0 放第一行。20/50 节点使用 `deploy/hosts-20.txt`、`deploy/hosts-50.txt`。

~~~bash
wc -l deploy/hosts-10.txt
sort deploy/hosts-10.txt | uniq -d
while read -r ip; do ssh "$ip" hostname; done < deploy/hosts-10.txt
~~~

分别应得到 10 行、无重复输出、全部 SSH 成功。

## 4. 全部节点安装和编译

在 node-0 的仓库目录执行；20/50 节点替换 hosts 文件：

~~~bash
while read -r ip; do
  ssh "$ip" 'bash -s' <<'REMOTE' &
set -Eeuo pipefail
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential cmake clang-14 libclang-14-dev git curl tmux jq \
  python3 python3-pip netcat-openbsd chrony
sudo systemctl enable --now chrony
if [[ ! -x "$HOME/.cargo/bin/cargo" ]]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
fi
source "$HOME/.cargo/env"
rustup default stable
if [[ -d "$HOME/Bullshark-Ubuntu24/.git" ]]; then
  git -C "$HOME/Bullshark-Ubuntu24" pull --ff-only
else
  git clone https://github.com/DrDaydream/Bullshark-Ubuntu24.git \
    "$HOME/Bullshark-Ubuntu24"
fi
cd "$HOME/Bullshark-Ubuntu24"
LIBCLANG_PATH=/usr/lib/llvm-14/lib \
CLANG_PATH=/usr/bin/clang-14 \
CC=/usr/bin/clang-14 \
CXX=/usr/bin/clang++-14 \
CXXFLAGS='-include cstdint' \
cargo build --release --features benchmark
test -x target/release/node
test -x target/release/benchmark_client
REMOTE
done < deploy/hosts-10.txt
wait
~~~

确认所有服务器的 commit 相同：

~~~bash
while read -r ip; do
  ssh "$ip" 'git -C ~/Bullshark-Ubuntu24 rev-parse HEAD'
done < deploy/hosts-10.txt
~~~

## 5. 生成密钥、committee 和参数

每次变更节点规模都要重新生成并分发：

~~~bash
cd ~/Bullshark-Ubuntu24
NODES=50
HOSTS_FILE=deploy/hosts-50.txt
rm -f deploy/node-*.json deploy/committee.json deploy/parameters.json
for ((i=0; i<NODES; i++)); do
  ./target/release/node generate_keys --filename "deploy/node-$i.json"
done
chmod 600 deploy/node-*.json

python3 - "$HOSTS_FILE" "$NODES" <<'PY'
import json
import sys
from pathlib import Path

nodes = int(sys.argv[2])
ips = [x.split("#", 1)[0].strip()
       for x in Path(sys.argv[1]).read_text().splitlines()]
ips = [x for x in ips if x]
assert len(ips) == nodes, (len(ips), nodes)
assert len(set(ips)) == nodes, "duplicate private IP"

authorities = {}
for i, ip in enumerate(ips):
    key = json.loads(Path(f"deploy/node-{i}.json").read_text())
    authorities[key["name"]] = {
        "primary": {
            "primary_to_primary": f"{ip}:3000",
            "worker_to_primary": f"{ip}:3001",
        },
        "stake": 1,
        "workers": {"0": {
            "primary_to_worker": f"{ip}:3002",
            "transactions": f"{ip}:3003",
            "worker_to_worker": f"{ip}:3004",
        }},
    }

Path("deploy/committee.json").write_text(
    json.dumps({"authorities": authorities}, indent=4)
)
Path("deploy/parameters.json").write_text(json.dumps({
    "header_size": 1000,
    "max_header_delay": 1000,
    "gc_depth": 50,
    "sync_retry_delay": 10000,
    "sync_retry_nodes": 3,
    "batch_size": 500000,
    "max_batch_delay": 1000,
}, indent=4))
PY

mapfile -t IPS < <(awk 'NF && $1 !~ /^#/ {print $1}' "$HOSTS_FILE")
for ((i=0; i<NODES; i++)); do
  ssh "${IPS[$i]}" 'mkdir -p ~/Bullshark-Ubuntu24/deploy'
  scp "deploy/node-$i.json" deploy/committee.json deploy/parameters.json \
    "${IPS[$i]}:Bullshark-Ubuntu24/deploy/"
done
~~~

检查公共配置哈希：

~~~bash
while read -r ip; do
  ssh "$ip" 'sha256sum ~/Bullshark-Ubuntu24/deploy/committee.json'
done < "$HOSTS_FILE"
~~~

所有输出必须相同。为了公平比较四个协议，应显式统一 `header_size`、`max_header_delay`、`batch_size`、`max_batch_delay`、总 TPS、时长和硬件。

## 6. Bullshark 敌手调度

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `BULLSHARK_FAULTS` | `0` | 每轮敌手数；0 表示无敌手 |
| `BULLSHARK_ADVERSARY_SEED` | `0` | 确定性随机种子 |
| `BULLSHARK_CLIENT_DURING_SILENCE` | `pause` | `pause` 或 `send` |
| `BULLSHARK_CLIENT_SILENCE_SLOT_MS` | `max_header_delay` | 预生成时序表的槽宽，毫秒 |

当 `BULLSHARK_FAULTS>0` 时，所有 EC2 仍启动。每轮选择恰好 f 个敌手：该轮 steady leader 必定在敌手集合中，因此默认不可用；其余 f-1 个按种子确定性选择。静默 Primary 不创建 Header，但继续接收消息。

每个 wave 第一轮和第三轮原定 leader 是 steady leader。steady 不可用时，节点从 wave 第一轮的其他顶点中一致选择 fallback leader；wave 末具有至少 `2f+1` 因果支持才 fallback commit，否则 fallback skip。结果统计 steady、fallback、fallback commit 和 fallback skip 比例。

`pause` 是默认实验模式：benchmark 在启动前为各 Client 生成单向墙钟时序表，静默槽内 Client 不发交易且本地 Worker 暂停 batch。`send` 保持 Client 和 batch 流量，但不会恢复 Primary 的 Header，用于对照输入负载和协议静默的影响。

## 7. 运行 10 / 20 / 50 节点

`run-multi-servers.sh` 参数为节点数、运行秒数、集群总 TPS。脚本把总 TPS 分摊到全部 Client。

无敌手基线：

~~~bash
cd ~/Bullshark-Ubuntu24
chmod +x run-multi-servers.sh
./run-multi-servers.sh 10 20 10000
./run-multi-servers.sh 20 60 10000
./run-multi-servers.sh 50 60 10000
~~~

最大建议敌手数：

~~~bash
BULLSHARK_FAULTS=3 BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=pause \
./run-multi-servers.sh 10 20 10000

BULLSHARK_FAULTS=6 BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=pause \
./run-multi-servers.sh 20 60 10000

BULLSHARK_FAULTS=16 BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=pause \
./run-multi-servers.sh 50 60 10000
~~~

保持输入流量的对照：

~~~bash
BULLSHARK_FAULTS=3 BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=send \
./run-multi-servers.sh 10 20 10000
~~~

脚本默认读取：

- `SSH_KEY` 留空时按 `~/.ssh/config` 为不同 Region 选择密钥
- `REMOTE_USER=ubuntu`
- `REMOTE_DIR=/home/ubuntu/Bullshark-Ubuntu24`
- `HOSTS_FILE=deploy/hosts-N.txt`

可显式覆盖：

~~~bash
HOSTS_FILE=/home/ubuntu/Bullshark-Ubuntu24/deploy/hosts-10.txt \
./run-multi-servers.sh 10 20 10000
~~~

脚本会等待全部 Worker 的 3003 和全部 Client 就绪，运行结束后把日志下载到 `benchmark/logs/` 并解析 TPS、延迟及 steady/fallback 统计。

## 8. 运行检查与排障

~~~bash
# commit 一致
while read -r ip; do
  ssh "$ip" 'git -C ~/Bullshark-Ubuntu24 rev-parse HEAD'
done < deploy/hosts-10.txt

# 测试运行期间检查交易端口
while read -r ip; do nc -vz -w 2 "$ip" 3003; done < deploy/hosts-10.txt

# 时间和资源
while read -r ip; do
  ssh "$ip" 'chronyc tracking | head -5; nproc; free -h; df -h /'
done < deploy/hosts-10.txt
~~~

常见问题：

- `hostname contains invalid characters`：hosts 中只允许纯私网 IPv4。
- `ready=0/N`：至少一个 Worker 未监听 3003；查看其 Worker 日志。
- `NoneType object has no attribute group`：至少一个 Client 未打印 `Start sending transactions`。
- 大量结果为 0：先确认测试真正开始、Primary 有提交、持续时间足够。
- fallback 比例不符合预期：确认 `BULLSHARK_FAULTS>0`，所有节点使用相同 commit、committee 和 seed。
- `librocksdb-sys` / bindgen：使用 clang-14 的完整编译环境变量。
- `Malformed` / `Serialization`：版本或公共配置不一致。
- `Connection refused` 表示进程未监听；`timed out` 通常是安全组、NACL、UFW 或地址错误。

~~~bash
ssh NODE_PRIVATE_IP 'tail -100 ~/Bullshark-Ubuntu24/run/logs/primary-INDEX.log'
ssh NODE_PRIVATE_IP 'tail -100 ~/Bullshark-Ubuntu24/run/logs/worker-INDEX-0.log'
ssh NODE_PRIVATE_IP 'tail -100 ~/Bullshark-Ubuntu24/run/logs/client-INDEX-0.log'
~~~

不要把 pem 或 `deploy/node-*.json` 提交到 GitHub。测试后停止或终止 EC2，并检查 EBS、Elastic IP、公网 IPv4 和跨 AZ 流量费用。
