# SDN Multi-Client Chat

A newline-delimited TCP chat app and a small Mininet/Open vSwitch network.
OS-Ken can learn switch addresses, prioritize registered chat connections, and
block a named user. A measurement command compares the SDN switch with a
standalone learning switch.

## What you need

- Linux with Python 3, Mininet, Open vSwitch, and `iperf`.
- `sudo` access for Mininet and OVS.
- OS-Ken and the encrypted chat clients use the project-local Python 3.12
  environment. The chat server and Mininet scripts use system `python3`.
- The client uses the `cryptography` package for private-message encryption;
  the setup script installs it in `.venv-sdn`.
- `xterm` is only needed to open separate interactive client windows.

## Set up the project environment once

From the project directory, run the helper script:

```sh
source setup_osken_env.sh
```

This installs `uv`, creates or reuses `.venv-sdn`, and installs OS-Ken
packages from `requirements-sdn.txt` and the chat crypto package from
`requirements-chat.txt`. It also adds the local `~/.local/bin` path and the
xterm library path to the current shell when it is sourced.

If you prefer a one-off run instead of sourcing it:

```sh
bash setup_osken_env.sh
```

Then activate the environment in your shell:

```sh
source .venv-sdn/bin/activate
```

The controller command below can also use the virtualenv directly without
activating it.

If xterm is installed under `~/.local`, it is already made visible for Mininet
in the sourced shell:

```sh
export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

## Run chat on one machine

Open two terminals in this directory. In the first:

```sh
python3 server.py
```

In the second:

```sh
./.venv-sdn/bin/python client.py 127.0.0.1
```

Register a name with `/register alice`, join a room with `/join room1`, then
type a message and press Enter to send it to everyone in that room. Use
`/pm bob hello` for a private message, `/help` for commands, and `/quit` to exit.
Before the first private message to someone, compare their `/keys` fingerprint
through a trusted channel and enter `/trust bob <fingerprint>`.

## Run the Mininet lab

The controller runs in the host network namespace. Start it from the project
directory in its own terminal:

```sh
./.venv-sdn/bin/osken-manager --ofp-tcp-listen-port 6653 \
  sdn/chat_qos_controller.py
```

Leave that terminal open. In another terminal, clean up old Mininet state and
start the SDN lab:

```sh
sudo mn -c
sudo env PATH="$HOME/.local/bin:$PATH" \
  LD_LIBRARY_PATH="$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu" \
  python3 mininet_lab.py lab --mode sdn
```

At the `mininet>` prompt, open three client terminals:

```text
mininet> xterm h1 h2 h3
```

In each xterm, run:

```sh
/absolute/path/to/tss-mc-chat/.venv-sdn/bin/python \
  /absolute/path/to/tss-mc-chat/client.py 10.0.0.4
```

Register a different name in each window and join the same room. To use the
plain OVS learning-switch baseline instead, run
`python3 mininet_lab.py lab --mode baseline`. The baseline does not need OS-Ken.

Exit the Mininet prompt with `exit` before starting another Mininet command.
The interactive lab and measurement runner both create interfaces named
`h1-eth0`, `s1-eth1`, and so on; running them at the same time causes
`RTNETLINK answers: File exists`. If a run ends unexpectedly, clean up:

```sh
sudo mn -c
```

To stop OS-Ken, press **Ctrl+C** in its terminal.

## Private-message encryption demo

Private messages are encrypted on the sender's client and decrypted on the
recipient's client. The server only forwards the encoded ciphertext. Room
messages are still ordinary text.

For a local demo, start the server in one terminal:

```sh
python3 server.py
```

Start each of two clients in a separate terminal:

```sh
./.venv-sdn/bin/python client.py 127.0.0.1 --crypto-demo
```

Register them as `alice` and `bob`. Each client will show the peer's public-key
fingerprint. Compare the full fingerprint out of band (for example, by reading
it aloud or using another trusted channel), then explicitly trust the matching
key in each client:

```text
/keys
/trust bob <bob's fingerprint>
```

Bob should similarly use `/trust alice <alice's fingerprint>`. Private messages
are blocked until the peer's key is trusted for that client session. `/keys`
lists the fingerprints and trust status again.

Send this from Alice:

```text
/pm-demo bob this text should not appear at the server
```

The sender displays the peer fingerprint, a fingerprint of the derived session
key, the random nonce, and the base64 ciphertext. Bob's demo client displays
the same session-key fingerprint when it successfully decrypts the message.
The line shown as `server command` is the encrypted payload that is sent through
the chat server; the server cannot turn it back into the original text.

Explain the trace in this order:

1. Each client creates a temporary X25519 key pair and sends only its public
   key through the server.
2. Alice uses her private key and Bob's verified public key to agree on a
   secret. Bob independently gets the same secret from his private key and
   Alice's verified public key.
3. HKDF-SHA256 derives a directional session key. The matching key fingerprints
   in both clients demonstrate that agreement without printing the secret key.
4. ChaCha20-Poly1305 encrypts the text and authenticates it with a fresh nonce.
   Base64 makes the nonce and ciphertext safe to send as one line.
5. The server forwards `PM bob <ciphertext>` as `PMFROM alice bob <ciphertext>`;
   it never receives the text or either client's private key.
6. Bob derives the same directional key and decrypts locally. Changing the
   ciphertext makes authentication fail rather than returning altered text.

The implementation uses X25519 for shared-secret agreement, HKDF-SHA256 to
derive a direction-specific session key, and ChaCha20-Poly1305 for authenticated
encryption. A fresh random nonce is used for every message. Private keys and
trusted fingerprints live only in client memory for that run; they are not
saved to disk. A changed peer key clears the trust decision.

Important limitation: the server distributes public keys, so a malicious server
could substitute keys and perform a man-in-the-middle attack. Comparing and
trusting peer fingerprints through a separate trusted channel detects that
substitution. The server can still see usernames, peer relationships, message
timing, and ciphertext sizes. Encryption also cannot protect a compromised
endpoint.

## Check flows and block a user

After a client registers and joins a room, inspect OVS flows from a host
terminal:

```sh
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```

The normal chat rule has priority 100; the learning-switch rule has priority
1. Block or unblock a registered user from another host terminal:

```sh
sudo python3 mininet_lab.py block --user alice
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
sudo python3 mininet_lab.py unblock --user alice
```

The block rule has priority 200 and matches the user's IP address and TCP source
port. The server sends the controller events that associate those network
details with a username.

## Send an iperf flood manually

At the Mininet prompt, start an iperf server on h4, then a client on h5:

```text
mininet> h4 iperf -s -p 5001 &
mininet> h5 iperf -c 10.0.0.4 -p 5001 -t 60
```

## Measure baseline versus SDN

Make sure OS-Ken is running, and no interactive Mininet lab is open. From a host
terminal:

```sh
sudo mn -c
sudo python3 mininet_lab.py measure --mode sdn \
  --clients 10 50 100 --runs 5 --output results.csv
```

`--mode sdn` runs both baseline and SDN cases, with and without an automatic
iperf flood. The script records measured samples in `results.csv` and averages
in `results_summary.csv`; it does not fill in example results. To run only the
baseline, use `--mode baseline`. For a quick test, reduce the workload:

```sh
sudo python3 mininet_lab.py measure --mode baseline \
  --clients 10 --runs 1 --output smoke-test.csv
```

Use a new output filename for a fresh set of results; otherwise rows are
appended. The bandwidth cap and OVS queues are configured by the lab runner,
so chat prioritization is meaningful under load.

## Project files

- [server.py](server.py): threaded TCP chat server and controller events.
- [client.py](client.py): terminal chat client.
- [config.py](config.py): IP addresses, ports, bandwidth, and queue settings.
- [requirements-chat.txt](requirements-chat.txt): cryptography dependency for
  encrypted private messages.
- [requirements-sdn.txt](requirements-sdn.txt): pinned OS-Ken dependencies.
- [setup_osken_env.sh](setup_osken_env.sh): project Python environment setup.
- [mininet_lab.py](mininet_lab.py): topology, interactive lab, policy commands,
  and measurement runner.
- [sdn/chat_qos_controller.py](sdn/chat_qos_controller.py): OS-Ken learning switch,
  chat flows, and event listener.
- [network diagram](docs/network-diagram.txt).
