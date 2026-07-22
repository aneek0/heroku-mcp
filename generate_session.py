"""Генерация отдельной Telegram-сессии для Heroku MCP Server (с поддержкой 2FA)

Использование:
  python3 generate_session.py --phone +79991234567
  python3 generate_session.py --phone +79991234567 --password my2fapassword
  python3 generate_session.py --qr
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("telethon не установлен. pip install telethon")
    sys.exit(1)

SESSION_DIR = Path(__file__).resolve().parent / "sessions"
SESSION_NAME = "heroku_mcp"


def parse_args():
    parser = argparse.ArgumentParser(description="Генерация Telegram-сессии для Heroku MCP")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--phone", help="Номер телефона (например, +79991234567)")
    group.add_argument("--qr", action="store_true", help="QR-код (не работает с 2FA)")
    parser.add_argument("--password", help="Пароль 2FA (если требуется)")
    parser.add_argument("--api-id", type=int, help="API ID (из my.telegram.org)")
    parser.add_argument("--api-hash", help="API Hash (из my.telegram.org)")
    return parser.parse_args()


def _load_creds(args):
    if args.api_id and args.api_hash:
        return args.api_id, args.api_hash
    api_id = os.environ.get("HEROKU_MCP_API_ID")
    api_hash = os.environ.get("HEROKU_MCP_API_HASH")
    if api_id and api_hash:
        return int(api_id), api_hash
    if yaml:
        for path in (Path("config.yaml"), Path(__file__).resolve().parent / "config.yaml"):
            if path.is_file():
                data = yaml.safe_load(path.read_text()) or {}
                cfg = data.get("heroku_mcp", {})
                if cfg.get("api_id") and cfg.get("api_hash"):
                    return cfg["api_id"], cfg["api_hash"]
    print("Ошибка: API_ID и API_HASH не найдены. Укажи --api-id/--api-hash,")
    print("         или добавь в config.yaml, или задай HEROKU_MCP_API_* в env.")
    sys.exit(1)


async def main():
    args = parse_args()
    api_id, api_hash = _load_creds(args)
    session_path = SESSION_DIR / SESSION_NAME
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    print(f"API_ID: {api_id}")
    print(f"Session: {session_path}.session")
    print()

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Сессия уже авторизована: {me.first_name} (ID: {me.id})")
        await client.disconnect()
        return

    if args.phone:
        phone = args.phone
        print(f"Отправка кода на {phone}...")

        try:
            await client.send_code_request(phone)
        except Exception as e:
            print(f"Ошибка отправки кода: {e}")
            await client.disconnect()
            sys.exit(1)

        # Код передаём через аргумент или временный файл
        code = input("Введи код: ").strip()
        if not code:
            print("Код не введён. Отмена.")
            await client.disconnect()
            sys.exit(1)

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            if args.password:
                password = args.password
            else:
                password = input("🔐 Требуется пароль 2FA: ").strip()
            await client.sign_in(password=password)
        except Exception as e:
            print(f"Ошибка входа: {e}")
            await client.disconnect()
            sys.exit(1)

    elif args.qr:
        print("Генерирую QR-код...\n")

        try:
            qr_login = await client.qr_login()
        except Exception as e:
            print(f"Ошибка генерации QR: {e}")
            print("Возможно, включена 2FA. Используй --phone.")
            await client.disconnect()
            sys.exit(1)

        print("╔══════════════════════════════════════════╗")
        print("║  Отсканируй QR-код в приложении Telegram  ║")
        print("║  (Настройки → Устройства → Привязать      ║")
        print("║   устройство → Сканировать QR)            ║")
        print("╚══════════════════════════════════════════╝\n")

        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(qr_login.url)
            qr.make()
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"Ссылка (отсканируй вручную): {qr_login.url}")

        print("\nОжидание сканирования...")

        try:
            await qr_login.wait(timeout=120)
        except TimeoutError:
            print("\n⏰ Таймаут. QR-код просрочен, запусти скрипт заново.")
            await client.disconnect()
            sys.exit(1)
        except SessionPasswordNeededError:
            if args.password:
                password = args.password
            else:
                password = input("🔐 Требуется пароль 2FA: ").strip()
            await client.sign_in(password=password)

    me = await client.get_me()
    print(f"\n✅ Успешно авторизовано!")
    print(f"   Пользователь: {me.first_name} {me.last_name or ''}")
    print(f"   ID: {me.id}")
    if me.username:
        print(f"   Username: @{me.username}")
    print(f"\nСессия сохранена: {session_path}.session")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
