import base64
import binascii
import getpass
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
leave_reply = threading.Event()
logout_reply = threading.Event()
public_keys = {}
trusted_keys = {}
pending_pm_requests = []
send_lock = threading.Lock()
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


def console_password(prompt_text):
    global console_input_active, console_input_prompt
    with console_lock:
        if deferred_output:
            print("\n".join(deferred_output), flush=True)
            deferred_output.clear()
        console_input_prompt = prompt_text
        console_input_active = True
    try:
        return getpass.getpass(prompt_text)
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
    if previous_key and previous_key != peer_key:
        return color("WARNING: " + peer + "'s encryption key changed.", "31")
    return ""


def receive_private_message_request(sock, line):
    parts = line.split(" ", 3)
    if len(parts) != 4:
        return color("ERR malformed private-message request", "31")
    _, request_id, sender, encoded_key = parts
    try:
        peer_key = decode_base64(encoded_key)
        X25519PublicKey.from_public_bytes(peer_key)
    except (ValueError, binascii.Error):
        send_line(sock, "PM_DECISION " + request_id + " no")
        return color("ERR invalid public key for " + sender, "31")

    fingerprint = key_fingerprint(peer_key)
    with state_lock:
        previous_key = public_keys.get(sender)
        public_keys[sender] = peer_key
        if previous_key and previous_key != peer_key:
            trusted_keys.pop(sender, None)
        already_trusted = trusted_keys.get(sender) == fingerprint
        if not already_trusted:
            pending_pm_requests.append(
                (request_id, sender, peer_key, fingerprint)
            )
    if already_trusted:
        send_line(sock, "PM_DECISION " + request_id + " yes")
        return ""
    return ""


def approve_pending_private_messages(sock):
    while True:
        with state_lock:
            if not pending_pm_requests:
                return
            request_id, sender, peer_key, fingerprint = pending_pm_requests.pop(0)
            current_key = public_keys.get(sender)
            already_trusted = trusted_keys.get(sender) == fingerprint
        if current_key != peer_key:
            send_line(sock, "PM_DECISION " + request_id + " no")
            console_print(color(
                "Declined the request because " + sender + "'s key changed.",
                "31",
            ))
            continue
        if already_trusted:
            send_line(sock, "PM_DECISION " + request_id + " yes")
            continue

        answer = console_input(
            "Do you trust this user? " + sender
            + " has sent you a private message.\nAccept? [y/N]: "
        ).strip().lower()
        accepted = answer in ("y", "yes")
        if accepted:
            with state_lock:
                if public_keys.get(sender) == peer_key:
                    trusted_keys[sender] = fingerprint
                else:
                    accepted = False
        send_line(
            sock,
            "PM_DECISION " + request_id + (" yes" if accepted else " no"),
        )
        if not accepted:
            console_print("Private message request declined.")


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
        _, message = decrypt_private_message(
            sender, recipient, payload, peer_key
        )
    except (InvalidTag, ValueError, UnicodeDecodeError, binascii.Error):
        return color("ERR private-message authentication failed", "31")
    return color("[pm from " + sender + "] " + message, "35")


def send_private_message(sock, recipient, message):
    with state_lock:
        sender = name
        peer_key = public_keys.get(recipient)
    if not sender:
        console_print(color("Register before sending private messages.", "31"))
        return
    if not peer_key:
        console_print(color("No public key received for " + recipient + " yet.", "31"))
        return
    try:
        _, _, payload = encrypt_private_message(
            sender, recipient, message, peer_key
        )
    except ValueError as error:
        console_print(
            color("Could not encrypt private message: " + str(error), "31")
        )
        return
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
                elif line.startswith("OK logged in as "):
                    name = line[len("OK logged in as "):]
                elif line.startswith("OK joined "):
                    room = line[len("OK joined "):]
                elif line.startswith("OK left "):
                    room = ""
                elif line == "OK logged out":
                    name = ""
                    room = ""
                    public_keys.clear()
                    trusted_keys.clear()
                    pending_pm_requests.clear()
            if (
                line.startswith("OK registered as ")
                or line.startswith("OK logged in as ")
                or line.startswith("ERR ")
            ):
                registration_reply.set()
            if line.startswith("OK joined ") or line.startswith("ERR "):
                join_reply.set()
            if line.startswith("OK left ") or line.startswith("ERR "):
                leave_reply.set()
            if line == "OK logged out" or line.startswith("ERR "):
                logout_reply.set()
            if line.startswith("KEY "):
                line = receive_public_key(line)
            elif line.startswith("PM_REQUEST "):
                line = receive_private_message_request(sock, line)
            elif line.startswith("PM_CANCEL "):
                _, request_id = line.split(" ", 1)
                with state_lock:
                    pending_pm_requests[:] = [
                        request for request in pending_pm_requests
                        if request[0] != request_id
                    ]
                line = "Private message request expired or was cancelled."
            elif line.startswith("PMFROM "):
                line = receive_private_message(line)
            elif line.startswith("PM_PENDING "):
                _, _, recipient = line.split(" ", 2)
                line = "Private message request sent to " + recipient + "."
            elif line.startswith("PM_RESULT "):
                _, _, result = line.split(" ", 2)
                if result == "accepted":
                    line = "Your private message was accepted and delivered."
                elif result == "declined":
                    line = "Your private message was declined."
                else:
                    line = "Your private message request expired or was cancelled."
            elif line.startswith("OK registered as "):
                line = "Signed in as " + line[len("OK registered as "):] + "."
            elif line.startswith("OK logged in as "):
                line = "Logged in as " + line[len("OK logged in as "):] + "."
            elif line == "OK logged out":
                line = "Logged out."
            elif line.startswith("OK joined "):
                line = "Joined room #" + line[len("OK joined "):] + "."
            elif line.startswith("OK left "):
                line = "Left room #" + line[len("OK left "):] + "."
            elif line == "OK message sent":
                line = "Room message sent."
            elif line.startswith("ERR "):
                line = color("Error: " + line[len("ERR "):], "31")
            if line:
                console_print(show_line(line))


def send_line(sock, message):
    with send_lock:
        sock.sendall((message + "\n").encode())


def main():
    if len(sys.argv) != 2:
        print("Usage: python client.py <server_ip>")
        return
    server_ip = sys.argv[1]
    try:
        sock = socket.create_connection((server_ip, PORT))
    except OSError as error:
        print("Could not connect:", error)
        return

    console_print("=== SDN Chat ===")
    console_print("Server: " + server_ip + ":" + str(PORT))
    console_print("")
    receiver = threading.Thread(target=receive_messages, args=(sock,), daemon=True)
    receiver.start()

    try:
        while True:
            try:
                with state_lock:
                    registered = bool(name)
                if not registered:
                    action = console_input(
                        "Choose (r)egister, (l)ogin, or (q)uit: "
                    ).strip().lower()
                    if action == "q":
                        send_line(sock, "QUIT")
                        break
                    if action not in ("r", "l"):
                        console_print(
                            "Choose r to register, l to log in, or q to quit."
                        )
                        continue
                    username = console_input("Account name: ").strip()
                    if not username:
                        console_print("An account name is required.")
                        continue
                    password = console_password("Password: ")
                    registration_reply.clear()
                    command = "REGISTER" if action == "r" else "LOGIN"
                    send_line(
                        sock,
                        command + " " + username + " " + password
                        + " " + public_key_text,
                    )
                    if not registration_reply.wait(timeout=5):
                        console_print(color(
                            "No response from the server. Check the connection and try again.",
                            "31",
                        ))
                        continue
                    with state_lock:
                        registered = bool(name)
                    if not registered:
                        continue

                approve_pending_private_messages(sock)
                with state_lock:
                    joined = bool(room)
                    current_name = name
                    current_room = room
                if joined:
                    console_print(
                        "\n=== " + current_name + " | room #" + current_room + " ===\n"
                        "1) Send a message to this room\n"
                        "2) Send an encrypted private message\n"
                        "3) Switch rooms\n"
                        "4) Leave this room\n"
                        "5) Log out\n"
                        "6) Quit"
                    )
                    choice = console_input("Select an action [1-6]: ").strip()
                else:
                    console_print(
                        "\n=== " + current_name + " | no room ===\n"
                        "1) Join a room\n"
                        "2) Send an encrypted private message\n"
                        "3) Log out\n"
                        "4) Quit"
                    )
                    choice = console_input("Select an action [1-4]: ").strip()

                with state_lock:
                    has_pending_pm_requests = bool(pending_pm_requests)
                if has_pending_pm_requests:
                    approve_pending_private_messages(sock)
                    continue

                if joined and choice == "1":
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
                elif (joined and choice == "3") or (not joined and choice == "1"):
                    room_name = console_input(
                        "Room name (leave blank to cancel): "
                    ).strip()
                    if room_name:
                        join_reply.clear()
                        send_line(sock, "JOIN " + room_name)
                        if not join_reply.wait(timeout=5):
                            console_print(color(
                                "No response while joining the room. Try again.",
                                "31",
                            ))
                    else:
                        console_print("Join cancelled.")
                elif joined and choice == "4":
                    leave_reply.clear()
                    send_line(sock, "LEAVE")
                    if not leave_reply.wait(timeout=5):
                        console_print(color("No response while leaving the room.", "31"))
                elif (joined and choice == "5") or (not joined and choice == "3"):
                    logout_reply.clear()
                    send_line(sock, "LOGOUT")
                    if not logout_reply.wait(timeout=5):
                        console_print(color("No response to logout.", "31"))
                    continue
                elif (joined and choice == "6") or (not joined and choice == "4"):
                    send_line(sock, "QUIT")
                    break
                else:
                    console_print("Invalid menu choice.")
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
