import argparse
import csv
import json
import os
import shutil
import signal
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
        if mode == "sdn":
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


def require_capture_tools():
    missing = [tool for tool in ("tcpdump", "tshark") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            "PCAP measurement requires " + " and ".join(missing)
            + "; install tcpdump and Wireshark/tshark"
        )


def analyze_capture(pcap_path):
    process = subprocess.run(
        [
            "tshark",
            "-r",
            pcap_path,
            "-T",
            "fields",
            "-e",
            "frame.time_epoch",
            "-e",
            "frame.len",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(
            "Could not read PCAP " + pcap_path + ": " + process.stderr.strip()
        )

    packets = []
    for line in process.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or not fields[1]:
            raise RuntimeError("Invalid packet data in PCAP: " + pcap_path)
        try:
            packets.append((float(fields[0]), int(fields[1])))
        except ValueError as error:
            raise RuntimeError("Invalid packet data in PCAP: " + pcap_path) from error
    if not packets:
        raise RuntimeError("PCAP contains no packets: " + pcap_path)

    start = min(timestamp for timestamp, _ in packets)
    end = max(timestamp for timestamp, _ in packets)
    second_bins = {}
    total_bytes = 0
    for timestamp, frame_bytes in packets:
        second = int(timestamp - start)
        bucket = second_bins.setdefault(second, {"packets": 0, "bytes": 0})
        bucket["packets"] += 1
        bucket["bytes"] += frame_bytes
        total_bytes += frame_bytes

    timeseries = [
        {
            "second": second,
            "packets": values["packets"],
            "captured_bytes": values["bytes"],
            "throughput_mbps": values["bytes"] * 8 / 1_000_000,
        }
        for second, values in sorted(second_bins.items())
    ]
    duration = end - start
    return {
        "packets": len(packets),
        "captured_bytes": total_bytes,
        "capture_duration_s": duration,
        "average_mbps": total_bytes * 8 / duration / 1_000_000 if duration else 0.0,
        "peak_1s_mbps": max(row["throughput_mbps"] for row in timeseries),
        "timeseries": timeseries,
    }


def run_case(network, count, flood, pcap_path):
    server = None
    client = None
    capture = network.get("h4").popen(
        [
            "tcpdump",
            "-n",
            "-U",
            "-i",
            "h4-eth0",
            "-s",
            "0",
            "-w",
            pcap_path,
            "tcp port " + str(PORT) + " or tcp port " + str(IPERF_PORT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.25)
        if capture.poll() is not None:
            _, errors = capture.communicate()
            raise RuntimeError("Could not start tcpdump: " + errors.strip())

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
        result = run_measurement(network, count)
    finally:
        for process in (client, server):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        if capture.poll() is None:
            capture.send_signal(signal.SIGINT)
        _, errors = capture.communicate(timeout=15)
        if capture.returncode not in (0, -signal.SIGINT):
            raise RuntimeError("tcpdump failed: " + errors.strip())
    return result, analyze_capture(pcap_path)


def save_results(network, mode, counts, runs, path, pcap_dir):
    samples = []
    averages = []
    pcap_samples = []
    pcap_averages = []
    pcap_timeseries = []
    for count in counts:
        for flood in (False, True):
            values = []
            pcap_throughput = []
            pcap_peaks = []
            pcap_packet_counts = []
            pcap_byte_counts = []
            for run in range(1, runs + 1):
                capture_name = (
                    mode + "_clients-" + str(count)
                    + "_flood-" + str(flood).lower()
                    + "_run-" + str(run) + "_" + str(time.time_ns()) + ".pcap"
                )
                capture_path = os.path.join(pcap_dir, capture_name)
                result, capture_stats = run_case(
                    network, count, flood, capture_path
                )
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
                pcap_sample = {
                    "mode": mode,
                    "clients": count,
                    "flood": str(flood).lower(),
                    "run": run,
                    "pcap_file": os.path.relpath(capture_path),
                    "packets": capture_stats["packets"],
                    "captured_bytes": capture_stats["captured_bytes"],
                    "capture_duration_s": capture_stats["capture_duration_s"],
                    "average_mbps": capture_stats["average_mbps"],
                    "peak_1s_mbps": capture_stats["peak_1s_mbps"],
                }
                pcap_samples.append(pcap_sample)
                pcap_throughput.append(capture_stats["average_mbps"])
                pcap_peaks.append(capture_stats["peak_1s_mbps"])
                pcap_packet_counts.append(capture_stats["packets"])
                pcap_byte_counts.append(capture_stats["captured_bytes"])
                pcap_timeseries.extend(
                    {
                        "mode": mode,
                        "clients": count,
                        "flood": str(flood).lower(),
                        "run": run,
                        "pcap_file": pcap_sample["pcap_file"],
                        **row,
                    }
                    for row in capture_stats["timeseries"]
                )
            averages.append({
                "mode": mode,
                "clients": count,
                "flood": str(flood).lower(),
                "runs": runs,
                "average_ms": statistics.mean(values),
            })
            pcap_averages.append({
                "mode": mode,
                "clients": count,
                "flood": str(flood).lower(),
                "runs": runs,
                "average_mbps": statistics.mean(pcap_throughput),
                "average_peak_1s_mbps": statistics.mean(pcap_peaks),
                "average_packets": statistics.mean(pcap_packet_counts),
                "average_captured_bytes": statistics.mean(pcap_byte_counts),
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

    output_base = os.path.splitext(path)[0]
    pcap_results_path = output_base + "_pcap_results.csv"
    with open(pcap_results_path, "a", newline="") as output:
        fields = (
            "mode", "clients", "flood", "run", "pcap_file", "packets",
            "captured_bytes", "capture_duration_s", "average_mbps",
            "peak_1s_mbps",
        )
        writer = csv.DictWriter(output, fieldnames=fields)
        if output.tell() == 0:
            writer.writeheader()
        writer.writerows(pcap_samples)

    pcap_summary_path = output_base + "_pcap_summary.csv"
    with open(pcap_summary_path, "a", newline="") as output:
        fields = (
            "mode", "clients", "flood", "runs", "average_mbps",
            "average_peak_1s_mbps", "average_packets",
            "average_captured_bytes",
        )
        writer = csv.DictWriter(output, fieldnames=fields)
        if output.tell() == 0:
            writer.writeheader()
        writer.writerows(pcap_averages)

    timeseries_path = output_base + "_pcap_timeseries.csv"
    with open(timeseries_path, "a", newline="") as output:
        fields = (
            "mode", "clients", "flood", "run", "pcap_file", "second",
            "packets", "captured_bytes", "throughput_mbps",
        )
        writer = csv.DictWriter(output, fieldnames=fields)
        if output.tell() == 0:
            writer.writeheader()
        writer.writerows(pcap_timeseries)

    print(
        "Saved chat latency to", path, "and", summary_path,
        "; PCAP metrics to", pcap_results_path, pcap_summary_path,
        "and", timeseries_path,
    )


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
        require_capture_tools()
        if args.mode == "sdn":
            controller_ready()
        modes = ("baseline", "sdn") if args.mode == "sdn" else ("baseline",)
        pcap_dir = os.path.abspath(os.path.splitext(args.output)[0] + "_pcaps")
        os.makedirs(pcap_dir, exist_ok=True)
        for mode in modes:
            network, server = start_network(mode)
            try:
                save_results(
                    network, mode, args.clients, args.runs, args.output, pcap_dir
                )
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
