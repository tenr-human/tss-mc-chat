import base64
import binascii
import json
import socket
import threading

from accounts import account_exists, register_account, verify_account
from config import (
    CONTROLLER_EVENT_HOST,
    CONTROLLER_EVENT_PORT,
    HOST,
    PORT,
)

users = {}
rooms = {}
lock = threading.Lock()
IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def send_line(conn, message):
    try:
        conn.sendall((message + "\n").encode())
        return True
    except OSError as error:
        print("Send error:", error)
        return False


def room_connections(room):
    with lock:
        names = list(rooms.get(room, set()))
        return [users[name]["conn"] for name in names if name in users]


def notify_controller(event, name, room, address):
    message = {
        "event": event,
        "user": name,
        "room": room,
        "ip": address[0],
        "port": address[1],
    }
    try:
        with socket.create_connection(
            (CONTROLLER_EVENT_HOST, CONTROLLER_EVENT_PORT), timeout=0.2
        ) as controller:
            controller.sendall((json.dumps(message) + "\n").encode())
    except OSError as error:
        print("Controller event unavailable:", error)


def valid_identifier(value):
    return (
        1 <= len(value) <= 32
        and all(char in IDENTIFIER_CHARACTERS for char in value)
    )


def register_or_login(conn, name, text, command):
    parts = text.split()
    if command == "REGISTER":
        if len(parts) == 2:
            new_name, public_key = parts
            password = None
        elif len(parts) == 3:
            new_name, password, public_key = parts
        else:
            send_line(conn, "ERR use REGISTER name [password] public_key")
            return name, False
    elif len(parts) == 3:
        new_name, password, public_key = parts
    else:
        send_line(conn, "ERR use LOGIN name password public_key")
        return name, False

    if name:
        send_line(conn, "ERR already authenticated")
        return name, False
    if not valid_identifier(new_name):
        send_line(conn, "ERR name must be 1-32 letters, numbers, dots, dashes or underscores")
        return name, False
    if password is not None and (
        len(password) < 8
        or len(password) > 128
        or any(char.isspace() for char in password)
    ):
        send_line(conn, "ERR password must be 8-128 characters with no spaces")
        return name, False
    try:
        if len(base64.b64decode(public_key, validate=True)) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        send_line(conn, "ERR invalid public key")
        return name, False

    try:
        with lock:
            if new_name in users:
                send_line(conn, "ERR user is already online")
                return name, False
            if command == "REGISTER":
                if password is None:
                    if account_exists(new_name):
                        send_line(conn, "ERR name is registered; use LOGIN")
                        return name, False
                elif not register_account(new_name, password):
                    send_line(conn, "ERR account already exists; use LOGIN")
                    return name, False
            elif not verify_account(new_name, password):
                send_line(conn, "ERR invalid username or password")
                return name, False

            other_users = [
                (username, user["public_key"], user["conn"])
                for username, user in users.items()
            ]
            users[new_name] = {
                "conn": conn,
                "room": "",
                "public_key": public_key,
            }
    except (OSError, RuntimeError) as error:
        print("Account storage error:", error)
        send_line(conn, "ERR account service is unavailable")
        return name, False

    response = "OK registered as " if command == "REGISTER" else "OK logged in as "
    send_line(conn, response + new_name)
    notify_controller("register", new_name, "", conn.getpeername())
    for username, peer_key, peer_conn in other_users:
        send_line(conn, "KEY " + username + " " + peer_key)
        send_line(peer_conn, "KEY " + new_name + " " + public_key)
    return new_name, False


def logout_user(conn, name):
    with lock:
        user = users.pop(name, None)
        room = user["room"] if user else ""
        if room:
            rooms[room].discard(name)
            if not rooms[room]:
                del rooms[room]
    if room:
        for other in room_connections(room):
            send_line(other, "* " + name + " left " + room)
    if user:
        notify_controller("leave", name, room, conn.getpeername())


def handle_command(conn, name, line):
    command, separator, text = line.partition(" ")

    if command in ("REGISTER", "LOGIN"):
        return register_or_login(conn, name, text if separator else "", command)

    if command == "QUIT":
        send_line(conn, "OK bye")
        return name, True

    if command == "LOGOUT":
        if text.strip():
            send_line(conn, "ERR LOGOUT does not take arguments")
            return name, False
        if not name:
            send_line(conn, "ERR register or login first")
            return name, False
        send_line(conn, "OK logged out")
        logout_user(conn, name)
        return "", False

    if not name:
        send_line(conn, "ERR register or login first")
        return name, False

    if command == "JOIN":
        room = text.strip()
        if not separator or not room:
            send_line(conn, "ERR use JOIN room")
            return name, False
        with lock:
            old_room = users[name]["room"]
            if old_room:
                rooms[old_room].discard(name)
            users[name]["room"] = room
            rooms.setdefault(room, set()).add(name)
        send_line(conn, "OK joined " + room)
        notify_controller("join", name, room, conn.getpeername())
        if old_room:
            for other in room_connections(old_room):
                send_line(other, "* " + name + " left " + old_room)
        for other in room_connections(room):
            if other != conn:
                send_line(other, "* " + name + " joined " + room)
        return name, False

    if command == "MSG":
        with lock:
            room = users[name]["room"]
        if not room:
            send_line(conn, "ERR join a room first")
            return name, False
        if not separator or not text:
            send_line(conn, "ERR message cannot be empty")
            return name, False
        send_line(conn, "OK message sent")
        for other in room_connections(room):
            send_line(other, "[" + room + "] " + name + ": " + text)
        return name, False

    if command == "PM":
        recipient, separator, ciphertext = text.partition(" ")
        if (
            not separator
            or not recipient
            or not ciphertext
            or any(char.isspace() for char in ciphertext)
        ):
            send_line(conn, "ERR use PM name encrypted_payload")
            return name, False
        with lock:
            target = users.get(recipient)
        if not target:
            send_line(conn, "ERR user is not online")
            return name, False
        send_line(conn, "OK private message sent")
        send_line(
            target["conn"],
            "PMFROM " + name + " " + recipient + " " + ciphertext,
        )
        return name, False

    send_line(conn, "ERR unknown command")
    return name, False


def handle_client(conn, address):
    name = ""
    buffer = b""
    print("Client connected:", address)
    try:
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.decode(errors="replace").rstrip("\r")
                name, should_quit = handle_command(conn, name, line)
                if should_quit:
                    return
    except OSError as error:
        print("Client connection error:", error)
    finally:
        if name:
            logout_user(conn, name)
        conn.close()
        print("Client disconnected:", address)


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()
    print("Chat server listening on", PORT)

    while True:
        try:
            conn, address = server.accept()
            thread = threading.Thread(target=handle_client, args=(conn, address))
            thread.start()
        except OSError as error:
            print("Accept error:", error)


if __name__ == "__main__":
    main()
