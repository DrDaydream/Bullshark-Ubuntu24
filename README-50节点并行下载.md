# Bullshark 50 节点并行下载、依赖安装与编译

在 node0 上执行。SSH 使用 `~/.ssh/config`，五个 PEM 按 Region 选择；`deploy/hosts-50.txt` 每行填写一个私网 IPv4，第一行是 node0。脚本使用 `SSH_KEY=` 留空的方式读取 SSH config。

~~~bash
cd ~/Bullshark-Ubuntu24
HOSTS=deploy/hosts-50.txt
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS" | xargs -P 50 -I {} ssh {} '
  if [ -d ~/Bullshark-Ubuntu24/.git ]; then git -C ~/Bullshark-Ubuntu24 pull --ff-only;
  elif [ ! -e ~/Bullshark-Ubuntu24 ]; then git clone https://github.com/DrDaydream/Bullshark-Ubuntu24.git ~/Bullshark-Ubuntu24;
  else echo "existing non-git directory" >&2; exit 1; fi'
~~~

依赖安装和编译（建议 `-P 10`，每台机器最多两个 Rust 编译线程）：

~~~bash
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS" | xargs -P 10 -I {} ssh {} '
  set -e
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential clang-14 libclang-14-dev llvm-14 cmake pkg-config libssl-dev librocksdb-dev git curl
  if ! command -v cargo >/dev/null 2>&1; then curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; fi
  cd ~/Bullshark-Ubuntu24
  . "$HOME/.cargo/env" 2>/dev/null || true
  cargo fetch
  test -e /usr/lib/llvm-14/lib/libclang.so || { echo "LLVM 14 libclang not found on $(hostname)" >&2; exit 1; }
  LIBCLANG_PATH=/usr/lib/llvm-14/lib CLANG_PATH=/usr/bin/clang-14 \
  CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 CXXFLAGS="-include cstdint" \
  CARGO_BUILD_JOBS=2 cargo build --quiet --release --features benchmark
'
~~~

~~~bash
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS" | xargs -P 50 -I {} ssh {} '
  printf "%s: " "$(hostname)"; test -x ~/Bullshark-Ubuntu24/target/release/node && echo "build ok" || echo "build failed"'
~~~

## 已通过 Orca-A 安装依赖

如果所有节点已成功编译 Orca-A，就不需要重复安装 APT 软件包或 Rust。直接复用系统依赖和 `~/.cargo` 缓存，编译 Bullshark：

~~~bash
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$HOSTS" | xargs -P 10 -I {} ssh {} '
  set -e; cd ~/Bullshark-Ubuntu24
  . "$HOME/.cargo/env"
  LIBCLANG_PATH=/usr/lib/llvm-14/lib CLANG_PATH=/usr/bin/clang-14 \
  CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 CXXFLAGS="-include cstdint" \
  CARGO_BUILD_JOBS=2 cargo build --quiet --release --features benchmark
'
~~~

Bullshark 仍需生成自己的 `~/Bullshark-Ubuntu24/target/release`。
