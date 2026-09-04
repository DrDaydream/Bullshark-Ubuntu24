# Copyright(C) Facebook, Inc. and its affiliates.
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from fabric import Connection, ThreadingGroup as Group
from fabric.exceptions import GroupException
from paramiko import RSAKey
from paramiko.ssh_exception import PasswordRequiredException, SSHException
from os.path import basename, splitext
from shlex import quote
from time import sleep
from math import ceil
from copy import deepcopy
import subprocess

from benchmark.config import (
    Committee, Key, NodeParameters, BenchParameters, ConfigError,
    place_adversaries_last,
)
from benchmark.utils import BenchError, Print, PathMaker, progress_bar
from benchmark.commands import CommandMaker
from benchmark.adversary_schedule import build_client_schedules, client_silence_slot_ms
from benchmark.logs import LogParser, ParseError
from benchmark.instance import InstanceManager


class FabricError(Exception):
    ''' Wrapper for Fabric exception with a meaningfull error message. '''

    def __init__(self, error):
        assert isinstance(error, GroupException)
        message = list(error.result.values())[-1]
        super().__init__(message)


class ExecutionError(Exception):
    pass


class InstallError(Exception):
    ''' Aggregate installation errors from every failed host. '''

    def __init__(self, failures, host_count):
        details = '\n\n'.join(failures)
        super().__init__(
            f'Installation failed on {len(failures)}/{host_count} nodes:\n{details}'
        )


class Bench:
    def __init__(self, ctx):
        self.manager = InstanceManager.make()
        self.settings = self.manager.settings
        try:
            ctx.connect_kwargs.pkey = RSAKey.from_private_key_file(
                self.manager.settings.key_path
            )
            self.connect = ctx.connect_kwargs
        except (IOError, PasswordRequiredException, SSHException) as e:
            raise BenchError('Failed to load SSH key', e)

    def _check_stderr(self, output):
        if isinstance(output, dict):
            for x in output.values():
                if x.stderr:
                    raise ExecutionError(x.stderr)
        else:
            if output.stderr:
                raise ExecutionError(output.stderr)

    def _install_steps(self):
        apt = (
            'sudo timeout --signal=TERM --kill-after=30s 1800 '
            'env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a '
            'apt-get -o DPkg::Lock::Timeout=600 -o Acquire::Retries=5 '
            '-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 '
            '-o Acquire::ForceIPv4=true '
            '-o APT::Update::Error-Mode=any '
            '-o Dpkg::Options::=--force-confdef '
            '-o Dpkg::Options::=--force-confold'
        )
        repo_name = quote(self.settings.repo_name)
        repo_url = quote(self.settings.repo_url)
        repo_branch = quote(self.settings.branch)
        update_repo = (
            f'git -C {repo_name} -c http.lowSpeedLimit=1000 '
            f'-c http.lowSpeedTime=60 fetch --progress origin {repo_branch} && '
            f'git -C {repo_name} checkout {repo_branch} && '
            f'git -C {repo_name} merge --ff-only FETCH_HEAD'
        )
        return [
            (
                'cloud-init',
                'if command -v cloud-init >/dev/null 2>&1; then\n'
                '    deadline=$((SECONDS + 900))\n'
                '    while true; do\n'
                '        cloud_status=$(sudo cloud-init status 2>&1)\n'
                '        cloud_status_code=$?\n'
                '        echo "$cloud_status"\n'
                '        if [ "$cloud_status_code" -ne 0 ]; then\n'
                '            sudo cloud-init status --long >&2\n'
                '            exit "$cloud_status_code"\n'
                '        fi\n'
                '        case "$cloud_status" in\n'
                '            *"status: done"*|*"status: disabled"*) break ;;\n'
                '            *"status: error"*|*"status: degraded"*)\n'
                '                sudo cloud-init status --long >&2\n'
                '                exit 1 ;;\n'
                '        esac\n'
                '        if (( SECONDS >= deadline )); then\n'
                '            echo "Timed out waiting for cloud-init" >&2\n'
                '            exit 124\n'
                '        fi\n'
                '        sleep 10\n'
                '    done\n'
                '    sudo cloud-init status --long\n'
                'else\n'
                '    echo "cloud-init is not installed; continuing"\n'
                'fi'
            ),
            (
                'apt-locks',
                'locks="/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock '
                '/var/cache/apt/archives/lock /var/lib/apt/lists/lock"\n'
                'deadline=$((SECONDS + 900))\n'
                'while sudo fuser $locks >/dev/null 2>&1; do\n'
                '    if (( SECONDS >= deadline )); then\n'
                '        echo "Timed out waiting for apt/dpkg locks" >&2\n'
                '        sudo fuser -v $locks >&2\n'
                '        exit 124\n'
                '    fi\n'
                '    echo "apt/dpkg lock is held; current holders:"\n'
                '    sudo fuser -v $locks 2>&1\n'
                '    sleep 5\n'
                'done'
            ),
            (
                'dpkg-configure',
                'sudo timeout --signal=TERM --kill-after=30s 900 '
                'env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a '
                'dpkg --configure -a'
            ),
            ('apt-update', f'{apt} update'),
            (
                'base-packages',
                f'{apt} install -y build-essential cmake curl git '
                'software-properties-common'
            ),
            (
                'enable-universe',
                'sudo timeout --signal=TERM --kill-after=30s 300 '
                'env DEBIAN_FRONTEND=noninteractive '
                'add-apt-repository --yes --no-update universe'
            ),
            ('apt-update-universe', f'{apt} update'),
            (
                'clang-packages',
                f'{apt} install -y clang-14 llvm-14 llvm-14-dev libclang-14-dev'
            ),
            (
                'clang-alternatives',
                'sudo update-alternatives --install /usr/bin/clang clang '
                '/usr/bin/clang-14 140 && '
                'sudo update-alternatives --install /usr/bin/clang++ clang++ '
                '/usr/bin/clang++-14 140 && '
                'sudo update-alternatives --set clang /usr/bin/clang-14 && '
                'sudo update-alternatives --set clang++ /usr/bin/clang++-14'
            ),
            (
                'rustup-install',
                'if [ -x "$HOME/.cargo/bin/rustup" ]; then\n'
                '    echo "rustup is already installed; skipping installer"\n'
                'else\n'
                '    timeout --signal=TERM --kill-after=30s 900 '
                "bash -o pipefail -c 'curl --proto \"=https\" --tlsv1.2 "
                '--fail --show-error '
                '--location --retry 5 --retry-delay 2 --retry-connrefused '
                '--connect-timeout 20 --max-time 300 https://sh.rustup.rs '
                "| sh -s -- -y'\n"
                'fi'
            ),
            (
                'rust-stable',
                'timeout --signal=TERM --kill-after=30s 1800 bash -c '
                "'source \"$HOME/.cargo/env\" && "
                "RUSTUP_MAX_RETRIES=5 rustup default stable'"
            ),
            (
                'cargo-environment',
                'if ! grep -q "^export LIBCLANG_PATH=/usr/lib/llvm-14/lib$" '
                '"$HOME/.cargo/env"; then\n'
                '    printf "%s\\n" "" \\\n'
                '        "export PATH=/usr/lib/llvm-14/bin:\\$PATH" \\\n'
                '        "export CC=/usr/bin/clang-14" \\\n'
                '        "export CXX=/usr/bin/clang++-14" \\\n'
                '        "export CLANG_PATH=/usr/bin/clang-14" \\\n'
                '        "export LIBCLANG_PATH=/usr/lib/llvm-14/lib" \\\n'
                '        "export CXXFLAGS=\\\"-include cstdint\\\"" '
                '>> "$HOME/.cargo/env"\n'
                'fi'
            ),
            (
                'repository',
                f'if [ -d {repo_name}/.git ]; then\n'
                '    timeout --signal=TERM --kill-after=30s 900 bash -c '
                f'{quote(update_repo)}\n'
                f'elif [ -e {repo_name} ]; then\n'
                f'    echo "Repository path exists but is not a git checkout: '
                f'{repo_name}" >&2\n'
                '    exit 1\n'
                'else\n'
                '    timeout --signal=TERM --kill-after=30s 900 '
                'git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 '
                f'clone --progress --branch {repo_branch} {repo_url} {repo_name}\n'
                'fi'
            ),
        ]

    @staticmethod
    def _install_script(host, steps):
        total = len(steps)
        script = [
            'set -u',
            'set -o pipefail',
            f'INSTALL_HOST={quote(host)}',
            'run_step() {',
            '    position="$1"',
            '    name="$2"',
            '    command="$3"',
            '    marker="[INSTALL][$INSTALL_HOST][$position][$name]"',
            '    output_marker="${marker}[output]"',
            "    printf '%s START\\n' \"$marker\"",
            '    bash -o pipefail -c "$command" 2>&1 | '
            "stdbuf -o0 tr '\\r' '\\n' | "
            'while IFS= read -r line || [ -n "$line" ]; do',
            "        printf '%s %s\\n' \"$output_marker\" \"$line\"",
            '    done &',
            '    pipeline_pid=$!',
            '    while kill -0 "$pipeline_pid" 2>/dev/null; do',
            '        timeout 30 tail --pid="$pipeline_pid" -f /dev/null '
            '2>/dev/null',
            '        kill -0 "$pipeline_pid" 2>/dev/null || break',
            "        printf '%s RUNNING\\n' \"$marker\"",
            '    done',
            '    wait "$pipeline_pid"',
            '    status=$?',
            '    if [ "$status" -ne 0 ]; then',
            "        printf '%s ERROR exit=%s\\n' \"$marker\" \"$status\"",
            '        return "$status"',
            '    fi',
            "    printf '%s OK\\n' \"$marker\"",
            '}',
        ]
        for index, (name, command) in enumerate(steps, start=1):
            position = f'{index}/{total}'
            script.append(
                f'run_step {quote(position)} {quote(name)} {quote(command)} '
                '|| { status=$?; '
                "printf '[INSTALL][%s][COMPLETE] ERROR exit=%s\\n' "
                '"$INSTALL_HOST" "$status"; exit "$status"; }'
            )
        script.append("printf '[INSTALL][%s][COMPLETE] OK\\n' \"$INSTALL_HOST\"")
        return '\n'.join(script)

    def _install_host(self, host, steps):
        script = self._install_script(host, steps)
        connection = None
        for attempt in range(1, 6):
            Print.info(
                f'[INSTALL][{host}][CONNECT] START attempt={attempt}/5'
            )
            try:
                connection = Connection(
                    host,
                    user='ubuntu',
                    connect_kwargs=self.connect,
                    connect_timeout=30,
                )
                connection.open()
                Print.info(
                    f'[INSTALL][{host}][CONNECT] OK attempt={attempt}/5'
                )
                break
            except Exception as error:
                message = f'{type(error).__name__}: {error}'
                Print.info(
                    f'[INSTALL][{host}][CONNECT] ERROR attempt={attempt}/5 '
                    f'{message}'
                )
                if connection is not None:
                    connection.close()
                    connection = None
                if attempt == 5:
                    return f'- {host}: connection error after 5 attempts: {message}'
                sleep(attempt * 2)

        try:
            result = connection.run(
                f'bash -lc {quote(script)}',
                hide=False,
                in_stream=False,
                warn=True,
                pty=False,
            )
        except Exception as error:
            message = f'{type(error).__name__}: {error}'
            Print.info(f'[INSTALL][{host}][RUN] ERROR {message}')
            return f'- {host}: remote execution error: {message}'
        finally:
            connection.close()

        if result.ok:
            return None

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        error_markers = [
            line for line in lines
            if ' ERROR exit=' in line and '[COMPLETE]' not in line
        ]
        failed_step = error_markers[-1] if error_markers else 'step unknown'
        output_tail = '\n'.join(f'    {line}' for line in lines[-8:])
        return (
            f'- {host}: remote exit={result.exited}; {failed_step}\n'
            f'  Last output:\n{output_tail}'
        )

    def install(self):
        hosts = self.manager.hosts(flat=True)
        if not hosts:
            raise BenchError(
                'Failed to install repo on testbed',
                ExecutionError('No testbed nodes are available')
            )

        Print.info(
            f'Installing Rust, dependencies, and the repo on '
            f'{len(hosts)} nodes in parallel...'
        )
        steps = self._install_steps()
        failures = {}
        with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
            futures = {
                executor.submit(self._install_host, host, steps): host
                for host in hosts
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                host = futures[future]
                try:
                    failure = future.result()
                except Exception as error:
                    message = f'{type(error).__name__}: {error}'
                    Print.info(f'[INSTALL][{host}][WORKER] ERROR {message}')
                    failure = f'- {host}: install worker error: {message}'
                if failure:
                    failures[host] = failure
                status = 'ERROR' if failure else 'OK'
                Print.info(
                    f'[INSTALL][SUMMARY] COMPLETE {completed}/{len(hosts)} '
                    f'host={host} status={status}'
                )

        if failures:
            ordered_failures = [failures[host] for host in hosts if host in failures]
            raise BenchError(
                'Failed to install repo on testbed',
                InstallError(ordered_failures, len(hosts))
            )
        Print.heading(f'Initialized testbed of {len(hosts)} nodes')

    def kill(self, hosts=[], delete_logs=False):
        assert isinstance(hosts, list)
        assert isinstance(delete_logs, bool)
        hosts = hosts if hosts else self.manager.hosts(flat=True)
        delete_logs = CommandMaker.clean_logs() if delete_logs else 'true'
        cmd = [delete_logs, f'({CommandMaker.kill()} || true)']
        try:
            g = Group(*hosts, user='ubuntu', connect_kwargs=self.connect)
            g.run(' && '.join(cmd), hide=True)
        except GroupException as e:
            raise BenchError('Failed to kill nodes', FabricError(e))

    def _select_hosts(self, bench_parameters):
        # Collocate the primary and its workers on the same machine.
        if bench_parameters.collocate:
            nodes = max(bench_parameters.nodes)

            # Ensure there are enough hosts.
            hosts = self.manager.hosts()
            if sum(len(x) for x in hosts.values()) < nodes:
                return []

            # Select the hosts in different data centers.
            ordered = zip(*hosts.values())
            ordered = [x for y in ordered for x in y]
            return ordered[:nodes]

        # Spawn the primary and each worker on a different machine. Each
        # authority runs in a single data center.
        else:
            primaries = max(bench_parameters.nodes)

            # Ensure there are enough hosts.
            hosts = self.manager.hosts()
            if len(hosts.keys()) < primaries:
                return []
            for ips in hosts.values():
                if len(ips) < bench_parameters.workers + 1:
                    return []

            # Ensure the primary and its workers are in the same region.
            selected = []
            for region in list(hosts.keys())[:primaries]:
                ips = list(hosts[region])[:bench_parameters.workers + 1]
                selected.append(ips)
            return selected

    def _background_run(self, host, command, log_file):
        name = splitext(basename(log_file))[0]
        cmd = f'tmux new -d -s "{name}" "{command} |& tee {log_file}"'
        c = Connection(host, user='ubuntu', connect_kwargs=self.connect)
        output = c.run(cmd, hide=True)
        self._check_stderr(output)

    def _update(self, hosts, collocate):
        if collocate:
            ips = list(set(hosts))
        else:
            ips = list(set([x for y in hosts for x in y]))

        Print.info(
            f'Updating {len(ips)} machines (branch "{self.settings.branch}")...'
        )
        cmd = [
            f'(cd {self.settings.repo_name} && git fetch -f)',
            f'(cd {self.settings.repo_name} && git checkout -f {self.settings.branch})',
            f'(cd {self.settings.repo_name} && git pull -f)',
            'source $HOME/.cargo/env',
            f'(cd {self.settings.repo_name}/node && {CommandMaker.compile()})',
            CommandMaker.alias_binaries(
                f'./{self.settings.repo_name}/target/release/'
            )
        ]
        g = Group(*ips, user='ubuntu', connect_kwargs=self.connect)
        g.run(' && '.join(cmd), hide=True)

    def _config(self, hosts, node_parameters, bench_parameters):
        Print.info('Generating configuration files...')

        # Cleanup all local configuration files.
        cmd = CommandMaker.cleanup()
        subprocess.run([cmd], shell=True, stderr=subprocess.DEVNULL)

        # Recompile the latest code.
        cmd = CommandMaker.compile().split()
        subprocess.run(cmd, check=True, cwd=PathMaker.node_crate_path())

        # Create alias for the client and nodes binary.
        cmd = CommandMaker.alias_binaries(PathMaker.binary_path())
        subprocess.run([cmd], shell=True)

        # Generate configuration files.
        keys = []
        key_files = [PathMaker.key_file(i) for i in range(len(hosts))]
        for filename in key_files:
            cmd = CommandMaker.generate_key(filename).split()
            subprocess.run(cmd, check=True)
            keys += [Key.from_file(filename)]

        keys = place_adversaries_last(keys, bench_parameters.faults)
        for key, filename in zip(keys, key_files):
            key.print(filename)

        names = [x.name for x in keys]

        if bench_parameters.collocate:
            workers = bench_parameters.workers
            addresses = OrderedDict(
                (x, [y] * (workers + 1)) for x, y in zip(names, hosts)
            )
        else:
            addresses = OrderedDict(
                (x, y) for x, y in zip(names, hosts)
            )
        committee = Committee(addresses, self.settings.base_port)
        committee.print(PathMaker.committee_file())

        node_parameters.print(PathMaker.parameters_file())

        # Cleanup all nodes and upload configuration files.
        progress = progress_bar(names, prefix='Uploading config files:')
        for i, name in enumerate(progress):
            for ip in committee.ips(name):
                c = Connection(ip, user='ubuntu', connect_kwargs=self.connect)
                c.run(f'{CommandMaker.cleanup()} || true', hide=True)
                c.put(PathMaker.committee_file(), '.')
                c.put(PathMaker.key_file(i), '.')
                c.put(PathMaker.parameters_file(), '.')

        return committee

    def _run_single(self, rate, committee, bench_parameters, node_parameters, debug=False):
        faults = bench_parameters.faults

        # Kill any potentially unfinished run and delete logs.
        hosts = committee.ips()
        self.kill(hosts=hosts, delete_logs=True)

        # Run clients for every worker; dynamically silent workers apply backpressure.
        Print.info('Booting clients...')
        workers_addresses = committee.workers_addresses(0)
        rate_share = ceil(rate / committee.workers())
        names = list(committee.json['authorities'])
        silence_slot_ms = client_silence_slot_ms(
            node_parameters.json['max_header_delay']
        )
        silence_schedules = build_client_schedules(
            names, faults, bench_parameters.duration, silence_slot_ms
        )
        for i, addresses in enumerate(workers_addresses):
            for (id, address) in addresses:
                host = Committee.ip(address)
                cmd = CommandMaker.run_client(
                    address,
                    bench_parameters.tx_size,
                    rate_share,
                    [x for y in workers_addresses for _, x in y],
                    silence_schedules[names[i]],
                    silence_slot_ms,
                )
                log_file = PathMaker.client_log_file(i, id)
                self._background_run(host, cmd, log_file)

        # Run every primary; adversarial authorities are selected per round.
        Print.info('Booting primaries...')
        for i, address in enumerate(committee.primary_addresses(0)):
            host = Committee.ip(address)
            cmd = CommandMaker.run_primary(
                PathMaker.key_file(i),
                PathMaker.committee_file(),
                PathMaker.db_path(i),
                PathMaker.parameters_file(),
                debug=debug,
                faults=faults
            )
            log_file = PathMaker.primary_log_file(i)
            self._background_run(host, cmd, log_file)

        # Run every worker; batch production is paused dynamically.
        Print.info('Booting workers...')
        for i, addresses in enumerate(workers_addresses):
            for (id, address) in addresses:
                host = Committee.ip(address)
                cmd = CommandMaker.run_worker(
                    PathMaker.key_file(i),
                    PathMaker.committee_file(),
                    PathMaker.db_path(i, id),
                    PathMaker.parameters_file(),
                    id,  # The worker's id.
                    debug=debug
                )
                log_file = PathMaker.worker_log_file(i, id)
                self._background_run(host, cmd, log_file)

        # Wait for all transactions to be processed.
        duration = bench_parameters.duration
        for _ in progress_bar(range(20), prefix=f'Running benchmark ({duration} sec):'):
            sleep(ceil(duration / 20))
        self.kill(hosts=hosts, delete_logs=False)

    def _logs(self, committee, faults):
        # Delete local logs (if any).
        cmd = CommandMaker.clean_logs()
        subprocess.run([cmd], shell=True, stderr=subprocess.DEVNULL)

        # Download log files.
        workers_addresses = committee.workers_addresses(0)
        progress = progress_bar(workers_addresses, prefix='Downloading workers logs:')
        for i, addresses in enumerate(progress):
            for id, address in addresses:
                host = Committee.ip(address)
                c = Connection(host, user='ubuntu', connect_kwargs=self.connect)
                c.get(
                    PathMaker.client_log_file(i, id), 
                    local=PathMaker.client_log_file(i, id)
                )
                c.get(
                    PathMaker.worker_log_file(i, id), 
                    local=PathMaker.worker_log_file(i, id)
                )

        primary_addresses = committee.primary_addresses(0)
        progress = progress_bar(primary_addresses, prefix='Downloading primaries logs:')
        for i, address in enumerate(progress):
            host = Committee.ip(address)
            c = Connection(host, user='ubuntu', connect_kwargs=self.connect)
            c.get(
                PathMaker.primary_log_file(i), 
                local=PathMaker.primary_log_file(i)
            )

        # Parse logs and return the parser.
        Print.info('Parsing logs and computing performance...')
        return LogParser.process(PathMaker.logs_path(), faults=faults)

    def run(self, bench_parameters_dict, node_parameters_dict, debug=False):
        assert isinstance(debug, bool)
        Print.heading('Starting remote benchmark')
        try:
            bench_parameters = BenchParameters(bench_parameters_dict)
            node_parameters = NodeParameters(node_parameters_dict)
        except ConfigError as e:
            raise BenchError('Invalid nodes or bench parameters', e)

        # Select which hosts to use.
        selected_hosts = self._select_hosts(bench_parameters)
        if not selected_hosts:
            Print.warn('There are not enough instances available')
            return

        # Update nodes.
        try:
            self._update(selected_hosts, bench_parameters.collocate)
        except (GroupException, ExecutionError) as e:
            e = FabricError(e) if isinstance(e, GroupException) else e
            raise BenchError('Failed to update nodes', e)

        # Upload all configuration files.
        try:
            committee = self._config(
                selected_hosts, node_parameters, bench_parameters
            )
        except (subprocess.SubprocessError, GroupException) as e:
            e = FabricError(e) if isinstance(e, GroupException) else e
            raise BenchError('Failed to configure nodes', e)

        # Run benchmarks.
        for n in bench_parameters.nodes:
            committee_copy = deepcopy(committee)
            committee_copy.remove_nodes(committee.size() - n)

            for r in bench_parameters.rate:
                Print.heading(f'\nRunning {n} nodes (input rate: {r:,} tx/s)')

                # Run the benchmark.
                for i in range(bench_parameters.runs):
                    Print.heading(f'Run {i+1}/{bench_parameters.runs}')
                    try:
                        self._run_single(
                            r, committee_copy, bench_parameters, node_parameters, debug
                        )

                        faults = bench_parameters.faults
                        logger = self._logs(committee_copy, faults)
                        logger.print(PathMaker.result_file(
                            faults,
                            n, 
                            bench_parameters.workers,
                            bench_parameters.collocate,
                            r, 
                            bench_parameters.tx_size, 
                        ))
                    except (subprocess.SubprocessError, GroupException, ParseError) as e:
                        self.kill(hosts=selected_hosts)
                        if isinstance(e, GroupException):
                            e = FabricError(e)
                        Print.error(BenchError('Benchmark failed', e))
                        continue
