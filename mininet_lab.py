import argparse
import csv
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

from config import (
    CHAT_BW_MBIT,
    CHAT_QUEUE_ID,
    CHAT_QUEUE_MIN_MBIT,
    CONTROLLER_EVENT_PORT,
    CONTROLLER_OPENFLOW_HOST,
    CONTROLLER_OPENFLOW_PORT,
    DEFAULT_QUEUE_ID,
    DEFAULT_QUEUE_MIN_MBIT,
    HOST_IPS,
    IPERF_PORT,
    PORT,
    POLICY_HOST,
    SERVER_IP,
    SERVER_SWITCH_PORT,
    SWITCH_LOCAL_IP,
    SWITCH_LOCAL_MAC,
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


class ChatTopo(Topo):
    def build(self):
        switch = self.addSwitch("s1", protocols="OpenFlow13")
        for number, ip in enumerate(HOST_IPS, 1):
            host = self.addHost("h" + str(number), ip=ip + "/24")
            self.addLink(host, switch, cls=TCLink, bw=CHAT_BW_MBIT)


def controller_ready():
    for port in (CONTROLLER_OPENFLOW_PORT, CONTROLLER_EVENT_PORT):
        try:
            with socket.create_connection(
                (CONTROLLER_OPENFLOW_HOST, port), timeout=1
            ):
                pass
        except OSError as error:
            raise RuntimeError(
                "Start the OS-Ken controller first; port "
                + str(port)
                + " is unavailable"
            ) from error


def set_queues():
    rate = CHAT_BW_MBIT * 1000000
    command = [
        "ovs-vsctl", "--", "set", "Port",
        "s1-eth" + str(SERVER_SWITCH_PORT), "qos=@qos",
        "--", "--id=@qos", "create", "QoS",
        "type=linux-htb",
        "other-config:max-rate=" + str(rate),
        "queues=" + str(DEFAULT_QUEUE_ID) + "=@default,"
        + str(CHAT_QUEUE_ID) + "=@chat",
        "--", "--id=@default", "create", "Queue",
        "other-config:min-rate=" + str(DEFAULT_QUEUE_MIN_MBIT * 1000000),
        "other-config:max-rate=" + str(rate),
        "--", "--id=@chat", "create", "Queue",
        "other-config:min-rate=" + str(CHAT_QUEUE_MIN_MBIT * 1000000),
        "other-config:max-rate=" + str(rate),
    ]
    subprocess.run(command, check=True)


def start_network(mode):
    network = Mininet(
        topo=ChatTopo(),
        switch=OVSSwitch,
        controller=None,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True,
    )
    if mode == "sdn":
        network.addController(
            "c0",
            controller=RemoteController,
            ip=CONTROLLER_OPENFLOW_HOST,
            port=CONTROLLER_OPENFLOW_PORT,
        )

    network.start()
    try:
        switch = network.get("s1")
        switch.cmd("ovs-vsctl set bridge s1 protocols=OpenFlow13")
        switch.cmd("ovs-vsctl set bridge s1 other-config:hwaddr=" + SWITCH_LOCAL_MAC)
        fail_mode = "secure" if mode == "sdn" else "standalone"
        switch.cmd("ovs-vsctl set-fail-mode s1 " + fail_mode)
        switch.cmd("ip addr add " + SWITCH_LOCAL_IP + "/24 dev s1")
        switch.cmd("ip link set s1 up")
        for number in range(1, len(HOST_IPS) + 1):
            network.get("h" + str(number)).setARP(SWITCH_LOCAL_IP, SWITCH_LOCAL_MAC)
        set_queues()

        server = network.get("h4").popen(
            [sys.executable, os.path.join(PROJECT_DIR, "server.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        if server.poll() is not None:
            raise RuntimeError("Chat server did not start on h4")
        return network, server
    except Exception:
        network.stop()
        raise


def stop_network(network, server):
    if server.poll() is None:
        server.terminate()
        server.wait(timeout=3)
    network.stop()


def launch_chat_clients(network):
    xterm = shutil.which("xterm")
    if not xterm:
        raise RuntimeError("xterm is required for the automatic chat terminals")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("A graphical display is required to open xterm windows")

    client_python = os.path.join(PROJECT_DIR, ".venv-sdn", "bin", "python")
    if not os.path.isfile(client_python):
        raise RuntimeError("Run setup_osken_env.sh before starting the chat lab")
    client = os.path.join(PROJECT_DIR, "client.py")
    for host_name in ("h1", "h2", "h3"):
        network.get(host_name).popen(
            [
                xterm,
                "-T",
                "Chat " + host_name,
                "-e",
                client_python,
                client,
                SERVER_IP,
                "--crypto-demo",
            ]
        )


def read_line(sock, buffer):
    while b"\n" not in buffer:
        data = sock.recv(4096)
        if not data:
            raise ConnectionError("Chat server closed the connection")
        buffer += data
    line, buffer = buffer.split(b"\n", 1)
    return line.decode(errors="replace").rstrip("\r"), buffer


def wait_for(sock, buffer, expected):
    while True:
        line, buffer = read_line(sock, buffer)
        if line == expected:
            return buffer
        if line.startswith("ERR "):
            raise RuntimeError(line)


def measured_client(server, username, start, times, errors):
    sock = None
    try:
        sock = socket.create_connection((server, PORT), timeout=30)
        sock.settimeout(60)
        buffer = b""
        sock.sendall(("REGISTER " + username + "\n").encode())
        buffer = wait_for(sock, buffer, "OK registered as " + username)
        sock.sendall(b"JOIN measure\n")
        buffer = wait_for(sock, buffer, "OK joined measure")
        start.wait()

        message = "measure-" + username
        began = time.perf_counter()
        sock.sendall(("MSG " + message + "\n").encode())
        expected = "[measure] " + username + ": " + message
        wait_for(sock, buffer, expected)
        times.append((time.perf_counter() - began) * 1000)
    except (OSError, RuntimeError, threading.BrokenBarrierError) as error:
        errors.append(str(error))
        start.abort()
    finally:
        if sock:
            sock.close()


def measure_clients(server, count):
    start = threading.Barrier(count)
    times = []
    errors = []
    run_id = str(time.time_ns())
    threads = []
    for number in range(count):
        username = "bench" + run_id + "_" + str(number)
        thread = threading.Thread(
            target=measured_client,
            args=(server, username, start, times, errors),
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise RuntimeError("Measurement failed: " + "; ".join(errors[:5]))
    if len(times) != count:
        raise RuntimeError("Not all clients received their room messages")
    return {
        "clients": count,
        "average_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "min_ms": min(times),
        "max_ms": max(times),
    }


def run_measurement(network, count):
    process = network.get("h1").popen(
        [
            sys.executable,
            os.path.join(PROJECT_DIR, "mininet_lab.py"),
            "benchmark",
            "--server",
            SERVER_IP,
            "--count",
            str(count),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        output, errors = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.communicate()
        raise RuntimeError("Measurement timed out with " + str(count) + " clients")
    if process.returncode:
        raise RuntimeError(errors.strip() or "Measurement command failed")
    for line in output.splitlines():
        if line.startswith("RESULT_JSON="):
            return json.loads(line[len("RESULT_JSON="):])
    raise RuntimeError("Measurement returned no results")


def run_case(network, count, flood):
    server = None
    client = None
    try:
        if flood:
            server = network.get("h4").popen(
                ["iperf", "-s", "-p", str(IPERF_PORT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)
            client = network.get("h5").popen(
                [
                    "iperf", "-c", SERVER_IP,
                    "-p", str(IPERF_PORT), "-t", "3600",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
            if server.poll() is not None or client.poll() is not None:
                raise RuntimeError("iperf flood did not start")
        return run_measurement(network, count)
    finally:
        for process in (client, server):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


def save_results(network, mode, counts, runs, path):
    samples = []
    averages = []
    for count in counts:
        for flood in (False, True):
            values = []
            for run in range(1, runs + 1):
                result = run_case(network, count, flood)
                samples.append({
                    "mode": mode,
                    "clients": count,
                    "flood": str(flood).lower(),
                    "run": run,
                    "average_ms": result["average_ms"],
                    "median_ms": result["median_ms"],
                    "min_ms": result["min_ms"],
                    "max_ms": result["max_ms"],
                })
                values.append(result["average_ms"])
                print(mode, count, flood, run, result["average_ms"], "ms")
            averages.append({
                "mode": mode,
                "clients": count,
                "flood": str(flood).lower(),
                "runs": runs,
                "average_ms": statistics.mean(values),
            })

    fields = ("mode", "clients", "flood", "run", "average_ms",
              "median_ms", "min_ms", "max_ms")
    with open(path, "a", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        if output.tell() == 0:
            writer.writeheader()
        writer.writerows(samples)

    summary_path = os.path.splitext(path)[0] + "_summary.csv"
    with open(summary_path, "a", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=("mode", "clients", "flood", "runs", "average_ms")
        )
        if output.tell() == 0:
            writer.writeheader()
        writer.writerows(averages)
    print("Saved measured results to", path, "and", summary_path)


def send_policy(action, username):
    event = {"event": action, "user": username}
    try:
        with socket.create_connection(
            (POLICY_HOST, CONTROLLER_EVENT_PORT), timeout=2
        ) as controller:
            controller.sendall((json.dumps(event) + "\n").encode())
        print(action.capitalize() + " request sent for " + username)
    except OSError as error:
        print("Could not contact controller:", error)


def main():
    parser = argparse.ArgumentParser(description="Run the SDN chat lab")
    parser.add_argument(
        "action", choices=("lab", "measure", "benchmark", "block", "unblock")
    )
    parser.add_argument("--mode", choices=("baseline", "sdn"), default="sdn")
    parser.add_argument("--clients", nargs="+", type=int, default=[10, 50, 100])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", default="results.csv")
    parser.add_argument("--server", default=SERVER_IP)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--user", default="")
    args = parser.parse_args()

    if args.action in ("block", "unblock"):
        if not args.user:
            parser.error("--user is required for block and unblock")
        send_policy(args.action, args.user)
        return
    if args.action == "benchmark":
        result = measure_clients(args.server, args.count)
        print("RESULT_JSON=" + json.dumps(result))
        return
    if args.action == "lab" and args.mode == "sdn":
        controller_ready()
    if args.action == "measure":
        if args.runs < 1 or any(count < 1 for count in args.clients):
            parser.error("client and run counts must be positive")
        if args.mode == "sdn":
            controller_ready()
        modes = ("baseline", "sdn") if args.mode == "sdn" else ("baseline",)
        for mode in modes:
            network, server = start_network(mode)
            try:
                save_results(network, mode, args.clients, args.runs, args.output)
            finally:
                stop_network(network, server)
        return

    network, server = start_network(args.mode)
    print("Chat server running on h4 at", SERVER_IP + ":" + str(PORT))
    try:
        if args.action == "lab":
            launch_chat_clients(network)
        CLI(network)
    finally:
        stop_network(network, server)


if __name__ == "__main__":
    main()
