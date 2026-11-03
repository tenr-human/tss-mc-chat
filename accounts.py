import hashlib
import json
import os
import secrets
import threading
from pathlib import Path


ACCOUNT_FILE = Path(
    os.environ.get(
        "CHAT_ACCOUNT_FILE",
        Path(__file__).resolve().parent / "accounts.json",
    )
)
_ACCOUNT_LOCK = threading.Lock()


def _load_accounts():
    try:
        with ACCOUNT_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError("could not read the account file") from error

    if not isinstance(data, dict):
        raise RuntimeError("account file has an invalid format")
    return data


def _save_accounts(accounts):
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = ACCOUNT_FILE.with_name(ACCOUNT_FILE.name + ".tmp")
    descriptor = os.open(
        temporary_file,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(accounts, file, indent=2)
    os.replace(temporary_file, ACCOUNT_FILE)


def _password_hash(password, salt):
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2 ** 14,
        r=8,
        p=1,
        maxmem=32 * 1024 * 1024,
        dklen=32,
    ).hex()


def register_account(username, password):
    with _ACCOUNT_LOCK:
        accounts = _load_accounts()
        if username in accounts:
            return False

        salt = secrets.token_bytes(16)
        accounts[username] = {
            "salt": salt.hex(),
            "password_hash": _password_hash(password, salt),
        }
        _save_accounts(accounts)
        return True


def verify_account(username, password):
    with _ACCOUNT_LOCK:
        accounts = _load_accounts()
        account = accounts.get(username)
        if not isinstance(account, dict):
            return False
        try:
            salt_text = account["salt"]
            expected_hash = account["password_hash"]
            if not isinstance(salt_text, str) or not isinstance(expected_hash, str):
                return False
            salt = bytes.fromhex(salt_text)
            if len(salt) != 16 or len(expected_hash) != 64:
                return False
            bytes.fromhex(expected_hash)
        except (KeyError, TypeError, ValueError):
            return False
        actual_hash = _password_hash(password, salt)
        return secrets.compare_digest(actual_hash, expected_hash)


def account_exists(username):
    with _ACCOUNT_LOCK:
        return username in _load_accounts()


def reset_accounts():
    with _ACCOUNT_LOCK:
        _save_accounts({})
