import base64
import binascii
import hashlib
import os
import readline
import socket
import sys
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from config import PORT

name = ""
room = ""
state_lock = threading.Lock()
console_lock = threading.Lock()
console_input_active = False
console_input_prompt = ""
deferred_output = []
registration_reply = threading.Event()
join_reply = threading.Event()
public_keys = {}
trusted_keys = {}
demo_mode = False
private_key = X25519PrivateKey.generate()
public_key_bytes = private_key.public_key().public_bytes(
    Encoding.Raw, PublicFormat.Raw
)
public_key_text = base64.b64encode(public_key_bytes).decode()


def color(text, code):
    return "\033[" + code + "m" + text + "\033[0m"


def console_print(text):
    with console_lock:
        if console_input_active:
            if sys.stdin.isatty() and sys.stdout.isatty():
                line_buffer = readline.get_line_buffer()
                sys.stdout.write(
                    "\r\033[2K"
                    + text
                    + "\n"
                    + console_input_prompt
                    + line_buffer
                )
                sys.stdout.flush()
            else:
                deferred_output.append(text)
            return
        print(text, flush=True)


def console_input(prompt_text):
    global console_input_active, console_input_prompt
    with console_lock:
        if deferred_output:
            print("\n".join(deferred_output), flush=True)
            deferred_output.clear()
        console_input_prompt = prompt_text
        console_input_active = True
    try:
        return input(prompt_text)
    finally:
        with console_lock:
            console_input_active = False
            console_input_prompt = ""
            if deferred_output:
                print("\n" + "\n".join(deferred_output), flush=True)
                deferred_output.clear()


def show_line(line):
    if line.startswith("ERR"):
        return color(line, "31")
    if line.startswith("[pm "):
        return color(line, "35")
    if line.startswith("* "):
        return color(line, "2")
    if line.startswith("[") and "] " in line and ": " in line:
        header, message = line.split("] ", 1)
        sender, separator, text = message.partition(": ")
        if separator:
            return header + "] " + color(sender, "36") + ": " + text
    return line


def decode_base64(value):
    return base64.b64decode(value, validate=True)


def key_fingerprint(key_bytes):
    return hashlib.sha256(key_bytes).hexdigest()


def session_key_for(sender, recipient, peer_public_key):
    shared_secret = private_key.exchange(
        X25519PublicKey.from_public_bytes(peer_public_key)
    )
    info = b"sdn-chat-pm-v1\0" + sender.encode() + b"\0" + recipient.encode()
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    ).derive(shared_secret)
    return key


def encrypt_private_message(sender, recipient, message, peer_public_key):
    key = session_key_for(sender, recipient, peer_public_key)
    nonce = os.urandom(12)
    associated_data = (sender + "\0" + recipient).encode()
    encrypted = ChaCha20Poly1305(key).encrypt(
        nonce, message.encode(), associated_data
    )
    payload = base64.b64encode(nonce + encrypted).decode()
    return key, nonce, payload


def decrypt_private_message(sender, recipient, payload, peer_public_key):
    encrypted = decode_base64(payload)
    if len(encrypted) < 12 + 16:
        raise ValueError("encrypted message is too short")
    nonce = encrypted[:12]
    ciphertext = encrypted[12:]
    key = session_key_for(sender, recipient, peer_public_key)
    associated_data = (sender + "\0" + recipient).encode()
    message = ChaCha20Poly1305(key).decrypt(
        nonce, ciphertext, associated_data
    )
    return key, message.decode()


def receive_public_key(line):
    parts = line.split(" ", 2)
    if len(parts) != 3:
        return color("ERR invalid public key update", "31")
    _, peer, encoded_key = parts
    try:
        peer_key = decode_base64(encoded_key)
        X25519PublicKey.from_public_bytes(peer_key)
    except (ValueError, binascii.Error):
        return color("ERR invalid public key for " + peer, "31")
    with state_lock:
        previous_key = public_keys.get(peer)
        public_keys[peer] = peer_key
        if previous_key and previous_key != peer_key:
            trusted_keys.pop(peer, None)
        if demo_mode:
            trusted_keys[peer] = key_fingerprint(peer_key)
    fingerprint = key_fingerprint(peer_key)
    if demo_mode:
        return "* " + peer + " encryption key received (demo auto-trust enabled)"
    if previous_key and previous_key != peer_key:
        return color(
            "WARNING: " + peer + "'s key changed. Verify fingerprint: "
            + fingerprint,
            "31",
        )
    return "* " + peer + " public-key fingerprint: " + fingerprint


def receive_private_message(line):
    parts = line.split(" ", 3)
    if len(parts) != 4:
        return color("ERR malformed encrypted private message", "31")
    _, sender, recipient, payload = parts
    with state_lock:
        local_name = name
        peer_key = public_keys.get(sender)
    if recipient != local_name or not peer_key:
        return color("ERR cannot identify private-message sender", "31")
    try:
        key, message = decrypt_private_message(
            sender, recipient, payload, peer_key
        )
    except (InvalidTag, ValueError, UnicodeDecodeError, binascii.Error):
        return color("ERR private-message authentication failed", "31")
    if demo_mode:
        return (
            color("[pm from " + sender + "] " + message, "35")
            + "\n* Demo: message decrypted; session-key fingerprint: "
            + key_fingerprint(key)
        )
    return color("[pm from " + sender + "] " + message, "35")


def send_private_message(sock, recipient, message):
    with state_lock:
        sender = name
        peer_key = public_keys.get(recipient)
        trusted_fingerprint = trusted_keys.get(recipient)
    if not sender:
        console_print(color("Register before sending private messages.", "31"))
        return
    if not peer_key:
        console_print(color("No public key received for " + recipient + " yet.", "31"))
        return
    fingerprint = key_fingerprint(peer_key)
    if not demo_mode and trusted_fingerprint != fingerprint:
        console_print(
            color(
                "Unverified key for " + recipient
                + ". Compare the fingerprint out of band, then use the "
                "peer-key menu option to trust it.",
                "31",
            )
        )
        return
    try:
        key, nonce, payload = encrypt_private_message(
            sender, recipient, message, peer_key
        )
    except ValueError as error:
        console_print(
            color("Could not encrypt private message: " + str(error), "31")
        )
        return
    if demo_mode:
        console_print(
            "\n".join(
                (
                    "* Demo: X25519 + HKDF-SHA256 session-key fingerprint: "
                    + key_fingerprint(key),
                    "* Demo: ChaCha20-Poly1305 nonce: " + nonce.hex(),
                    "* Demo: ciphertext sent to server (base64): " + payload,
                    "* Demo: server command: PM " + recipient + " " + payload,
                )
            )
        )
    send_line(sock, "PM " + recipient + " " + payload)


def receive_messages(sock):
    global name, room
    buffer = b""
    while True:
        try:
            data = sock.recv(1024)
        except OSError as error:
            console_print("Connection error: " + str(error))
            return
        if not data:
            console_print("Server disconnected.")
            return
        buffer += data
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            line = raw_line.decode(errors="replace").rstrip("\r")
            with state_lock:
                if line.startswith("OK registered as "):
                    name = line[len("OK registered as "):]
                elif line.startswith("OK joined "):
                    room = line[len("OK joined "):]
            if line.startswith("OK registered as ") or line.startswith("ERR "):
                registration_reply.set()
            if line.startswith("OK joined ") or line.startswith("ERR "):
                join_reply.set()
            if line.startswith("KEY "):
                line = receive_public_key(line)
            elif line.startswith("PMFROM "):
                line = receive_private_message(line)
            elif line.startswith("OK registered as "):
                line = "Signed in as " + line[len("OK registered as "):] + "."
            elif line.startswith("OK joined "):
                line = "Joined room #" + line[len("OK joined "):] + "."
            elif line == "OK message sent":
                line = "Room message sent."
            elif line == "OK private message sent":
                line = "Private message sent securely."
            elif line.startswith("ERR "):
                line = color("Error: " + line[len("ERR "):], "31")
            console_print(show_line(line))


def send_line(sock, message):
    sock.sendall((message + "\n").encode())


def trust_peer_interactively():
    with state_lock:
        known_keys = list(public_keys.items())
        trusted = dict(trusted_keys)
    if not known_keys:
        console_print("No peer keys received yet. Wait for another user to join.")
        return
    untrusted = [
        (peer, peer_key)
        for peer, peer_key in known_keys
        if trusted.get(peer) != key_fingerprint(peer_key)
    ]
    if not untrusted:
        console_print("All currently known peer keys are trusted.")
        return
    console_print(
        "Compare a peer's fingerprint through a separate trusted channel."
    )
    for number, (peer, peer_key) in enumerate(untrusted, 1):
        console_print(str(number) + ") " + peer + ": " + key_fingerprint(peer_key))
    try:
        choice = int(console_input("Peer number to trust [0 cancels]: "))
    except ValueError:
        console_print("Enter one of the listed peer numbers.")
        return
    if choice == 0:
        return
    if not 1 <= choice <= len(untrusted):
        console_print("Enter one of the listed peer numbers.")
        return
    peer, peer_key = untrusted[choice - 1]
    confirmation = console_input(
        "Does this fingerprint match your trusted check? Type YES to trust: "
    ).strip()
    if confirmation != "YES":
        console_print("Peer key was not trusted.")
        return
    with state_lock:
        if public_keys.get(peer) != peer_key:
            console_print(
                color("Peer key changed; review it again before trusting.", "31")
            )
            return
        trusted_keys[peer] = key_fingerprint(peer_key)
    console_print("* Trusted " + peer + "'s public key for this session.")


def main():
    global demo_mode
    if len(sys.argv) not in (2, 3) or (
        len(sys.argv) == 3 and sys.argv[2] != "--crypto-demo"
    ):
        print("Usage: python client.py <server_ip> [--crypto-demo]")
        return
    server_ip = sys.argv[1]
    demo_mode = len(sys.argv) == 3
    try:
        sock = socket.create_connection((server_ip, PORT))
    except OSError as error:
        print("Could not connect:", error)
        return

    console_print("=== SDN Chat ===")
    console_print("Server: " + server_ip + ":" + str(PORT))
    if demo_mode:
        console_print(
            "Crypto demo: peer keys are trusted automatically for this session."
        )
        console_print(
            color(
                "Use only in a trusted lab: a malicious server can substitute keys.",
                "33",
            )
        )
    console_print("")
    receiver = threading.Thread(target=receive_messages, args=(sock,), daemon=True)
    receiver.start()

    try:
        while True:
            try:
                with state_lock:
                    registered = bool(name)
                if not registered:
                    username = console_input(
                        "Choose a display name (for example, alice): "
                    ).strip()
                    if not username:
                        console_print(
                            "A display name is required. Please try again."
                        )
                        continue
                    registration_reply.clear()
                    send_line(sock, "REGISTER " + username + " " + public_key_text)
                    if not registration_reply.wait(timeout=5):
                        console_print(color(
                            "No response from the server. Check the connection "
                            "and try again.",
                            "31",
                        ))
                        continue
                    with state_lock:
                        registered = bool(name)
                    if not registered:
                        continue

                with state_lock:
                    joined = bool(room)
                if not joined:
                    room_name = console_input(
                        "Choose a room name (for example, room1): "
                    ).strip()
                    if not room_name:
                        console_print("A room name is required. Please try again.")
                        continue
                    join_reply.clear()
                    send_line(sock, "JOIN " + room_name)
                    if not join_reply.wait(timeout=5):
                        console_print(color(
                            "No response while joining the room. Try again.",
                            "31",
                        ))
                        continue
                    with state_lock:
                        joined = bool(room)
                    if not joined:
                        continue

                with state_lock:
                    current_name = name
                    current_room = room
                console_print(
                    "\n=== " + current_name + " | room #" + current_room + " ===\n"
                    "1) Send a message to this room\n"
                    "2) Send an encrypted private message\n"
                    "3) Switch to another room\n"
                    "4) Review or trust a peer's encryption key\n"
                    "5) Quit"
                )
                choice = console_input("Select an action [1-5]: ").strip()
                if choice == "1":
                    message = console_input(
                        "Room message (leave blank to cancel): "
                    ).strip()
                    if message:
                        send_line(sock, "MSG " + message)
                    else:
                        console_print("Room message cancelled.")
                elif choice == "2":
                    recipient = console_input(
                        "Recipient's online display name "
                        "(leave blank to cancel): "
                    ).strip()
                    if not recipient:
                        console_print("Private message cancelled.")
                        continue
                    message = console_input(
                        "Private message to " + recipient
                        + " (leave blank to cancel): "
                    ).strip()
                    if recipient and message:
                        send_private_message(sock, recipient, message)
                    else:
                        console_print("Private message cancelled.")
                elif choice == "3":
                    room_name = console_input(
                        "New room name (leave blank to stay in #"
                        + current_room + "): "
                    ).strip()
                    if room_name:
                        join_reply.clear()
                        send_line(sock, "JOIN " + room_name)
                        if not join_reply.wait(timeout=5):
                            console_print(color(
                                "No response while switching rooms. Try again.",
                                "31",
                            ))
                    else:
                        console_print("Room switch cancelled.")
                elif choice == "4":
                    trust_peer_interactively()
                elif choice == "5":
                    send_line(sock, "QUIT")
                    break
                else:
                    console_print("Enter a number from 1 to 5.")
            except EOFError:
                send_line(sock, "QUIT")
                break
    except OSError as error:
        console_print("Connection error: " + str(error))
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        receiver.join(timeout=1)
        sock.close()


if __name__ == "__main__":
    main()
