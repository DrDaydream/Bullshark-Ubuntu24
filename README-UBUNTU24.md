# Bullshark on Ubuntu 24.04

本目录来自官方 `facebookresearch/narwhal` 仓库的 `bullshark` 分支，并已适配 Ubuntu 24.04、Python 3.12 和本地 `fab local`。

## 安装

```bash
sudo apt update
sudo apt install -y build-essential cmake clang-14 libclang-14-dev git curl tmux python3 python3-pip
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
cd benchmark
python3 -m pip install --user --break-system-packages -r requirements.txt
```

## 本地测试

必须从 `benchmark` 目录运行：

```bash
cd /path/to/Bullshark-Ubuntu24/benchmark
fab local
```

默认参数位于 `benchmark/fabfile.py`：4节点、1 Worker/节点、50,000 tx/s、512 B、20秒。

兼容修改包括：将会触发 Ubuntu 24.04 bindgen panic 的 RocksDB 0.16 升至 API 兼容的 0.22；更新 Python 3.12 依赖；自动选择 clang/libclang；显式保留 INFO 性能日志；使用项目测试专属 tmux socket。

本机验证结果（不同硬件不可直接横向比较）：End-to-end TPS 49,355，End-to-end latency 1,039 ms。

## 敌手选择限制

当 benchmark 的 `faults > 0` 时启用动态敌手调度，所有节点仍然启动并传播 DAG 数据。每个 wave 第一轮和第三轮的 steady-state leader 都不允许触发提交；各节点使用 wave 编号从第一轮的其他顶点中确定同一个伪随机 fallback leader。在第三轮结束后收齐 quorum 时，若 wave 末顶点到 fallback leader 的因果支持达到 `2f+1`，则 fallback commit，否则 fallback skip。`faults=0` 时保留原始 steady-state 调度。
