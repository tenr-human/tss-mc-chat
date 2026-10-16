import base64
import binascii
import hashlib
import os
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
registration_reply = threading.Event()
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


def prompt():
    with state_lock:
        current_name = name
        current_room = room
    if current_name and current_room:
        return current_name + "@" + current_room + " > "
    if current_name:
        return current_name + " > "
    return "> "


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
    fingerprint = key_fingerprint(peer_key)
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
        print(
            "\r\033[2K"
            + "* Demo: decrypted at this client; session-key fingerprint: "
            + key_fingerprint(key)
        )
    return color("[pm from " + sender + "] " + message, "35")


def send_private_message(sock, recipient, message, demo=False):
    with state_lock:
        sender = name
        peer_key = public_keys.get(recipient)
        trusted_fingerprint = trusted_keys.get(recipient)
    if not sender:
        print(color("Register before sending private messages.", "31"))
        return
    if not peer_key:
        print(color("No public key received for " + recipient + " yet.", "31"))
        return
    fingerprint = key_fingerprint(peer_key)
    if trusted_fingerprint != fingerprint:
        print(
            color(
                "Unverified key for " + recipient
                + ". Compare /keys out of band, then use /trust "
                + recipient + " " + fingerprint,
                "31",
            )
        )
        return
    try:
        key, nonce, payload = encrypt_private_message(
            sender, recipient, message, peer_key
        )
    except ValueError as error:
        print(color("Could not encrypt private message: " + str(error), "31"))
        return
    if demo or demo_mode:
        print("* Demo: recipient public-key fingerprint: " + fingerprint)
        print(
            "* Demo: X25519 + HKDF-SHA256 session-key fingerprint: "
            + key_fingerprint(key)
        )
        print("* Demo: ChaCha20-Poly1305 nonce: " + nonce.hex())
        print("* Demo: ciphertext sent to server (base64): " + payload)
        print("* Demo: server command: PM " + recipient + " " + payload)
    send_line(sock, "PM " + recipient + " " + payload)


def receive_messages(sock):
    global name, room
    buffer = b""
    while True:
        try:
            data = sock.recv(1024)
        except OSError as error:
            print("\nConnection error:", error)
            return
        if not data:
            print("\nServer disconnected")
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
            if line.startswith("KEY "):
                line = receive_public_key(line)
            elif line.startswith("PMFROM "):
                line = receive_private_message(line)
            print("\r\033[2K" + show_line(line))
            print(prompt(), end="", flush=True)


def send_line(sock, message):
    sock.sendall((message + "\n").encode())


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

    print("=== SDN Chat ===  server " + server_ip + ":" + str(PORT))
    print("Type /help for commands.\n")
    if demo_mode:
        print("Crypto demo mode: keys and ciphertext details will be shown.")
    receiver = threading.Thread(target=receive_messages, args=(sock,), daemon=True)
    receiver.start()
    print(prompt(), end="", flush=True)

    try:
        while True:
            try:
                text = input()
            except EOFError:
                text = "/quit"
            if text == "/help":
                print(
                    "/register name, /join room, /pm name message, "
                    "/pm-demo name message, /keys, "
                    "/trust name fingerprint, /quit"
                )
                print("Anything else is sent to your room.")
            elif text == "/keys":
                with state_lock:
                    known_keys = list(public_keys.items())
                    trusted = dict(trusted_keys)
                if not known_keys:
                    print("No peer keys received yet.")
                for peer, peer_key in known_keys:
                    status = (
                        "trusted"
                        if trusted.get(peer) == key_fingerprint(peer_key)
                        else "not verified"
                    )
                    print(peer + ": " + key_fingerprint(peer_key) + " (" + status + ")")
            elif text.startswith("/trust "):
                peer, separator, fingerprint = text[len("/trust "):].partition(
                    " "
                )
                with state_lock:
                    peer_key = public_keys.get(peer)
                    if (
                        separator
                        and peer_key
                        and fingerprint == key_fingerprint(peer_key)
                    ):
                        trusted_keys[peer] = fingerprint
                        trusted = True
                    else:
                        trusted = False
                if trusted:
                    print("* Trusted " + peer + "'s public key for this session.")
                else:
                    print(color(
                        "Fingerprint does not match /keys for an online user.",
                        "31",
                    ))
            elif text == "/quit":
                send_line(sock, "QUIT")
                break
            elif text.startswith("/register "):
                registration_reply.clear()
                send_line(
                    sock,
                    "REGISTER " + text[len("/register "):]
                    + " " + public_key_text,
                )
                if not registration_reply.wait(timeout=5):
                    print(color("No response to registration.", "31"))
            elif text.startswith("/join "):
                send_line(sock, "JOIN " + text[len("/join "):])
            elif text.startswith("/pm-demo "):
                recipient, separator, message = text[len("/pm-demo "):].partition(
                    " "
                )
                if not separator or not recipient or not message:
                    print(color("Use /pm-demo name message.", "31"))
                else:
                    send_private_message(sock, recipient, message, demo=True)
            elif text.startswith("/pm "):
                recipient, separator, message = text[len("/pm "):].partition(" ")
                if not separator or not recipient or not message:
                    print(color("Use /pm name message.", "31"))
                else:
                    send_private_message(sock, recipient, message)
            elif text.startswith("/"):
                print(color("Unknown command. Type /help for commands.", "31"))
            elif text:
                send_line(sock, "MSG " + text)
            print(prompt(), end="", flush=True)
    except OSError as error:
        print("\nConnection error:", error)
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        receiver.join(timeout=1)
        sock.close()


if __name__ == "__main__":
    main()
