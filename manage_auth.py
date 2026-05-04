"""
Управління користувачами HTTP Basic Auth дашборду.

Використання:
  python manage_auth.py add <username>           — додати/оновити пароль (запит з stdin)
  python manage_auth.py remove <username>        — видалити користувача
  python manage_auth.py list                     — показати усіх користувачів

Файл зберігається як `.dashboard_auth` поряд з цим скриптом, формат:
  username:pbkdf2_hash
Хеш робить werkzeug.security.generate_password_hash (pbkdf2:sha256, 600k iter).
"""
from pathlib import Path
from werkzeug.security import generate_password_hash
import getpass
import sys

AUTH_FILE = Path(__file__).parent / ".dashboard_auth"


def _read() -> dict[str, str]:
    users: dict[str, str] = {}
    if not AUTH_FILE.exists():
        return users
    for line in AUTH_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        u, h = line.split(":", 1)
        users[u.strip()] = h.strip()
    return users


def _write(users: dict[str, str]) -> None:
    body = "\n".join(f"{u}:{h}" for u, h in sorted(users.items())) + "\n"
    AUTH_FILE.write_text(body, encoding="utf-8")
    AUTH_FILE.chmod(0o600)


def cmd_add(username: str) -> int:
    pw1 = getpass.getpass(f"Пароль для {username}: ")
    pw2 = getpass.getpass("Повторіть пароль: ")
    if pw1 != pw2:
        print("Паролі не співпадають.", file=sys.stderr)
        return 1
    if len(pw1) < 8:
        print("Пароль має бути ≥ 8 символів.", file=sys.stderr)
        return 1
    users = _read()
    action = "оновлено" if username in users else "додано"
    users[username] = generate_password_hash(pw1)
    _write(users)
    print(f"Користувача '{username}' {action}. Усього користувачів: {len(users)}.")
    return 0


def cmd_remove(username: str) -> int:
    users = _read()
    if username not in users:
        print(f"Користувача '{username}' немає.", file=sys.stderr)
        return 1
    del users[username]
    _write(users)
    print(f"Користувача '{username}' видалено. Залишилось: {len(users)}.")
    return 0


def cmd_list() -> int:
    users = _read()
    if not users:
        print("(порожньо — auth вимкнений)")
        return 0
    for u in sorted(users):
        print(u)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    if args[0] == "add" and len(args) == 2:
        return cmd_add(args[1])
    if args[0] == "remove" and len(args) == 2:
        return cmd_remove(args[1])
    if args[0] == "list" and len(args) == 1:
        return cmd_list()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
