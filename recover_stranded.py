"""Возврат денег с брошенного адреса UQA2…ttEY.

ЧТО ЭТО ЗА АДРЕС. Кошелёк версии W5 имеет РАЗНЫЙ адрес в mainnet и testnet:
сетевой идентификатор входит в инициализацию контракта. Адрес UQA2…ttEY — это
W5, собранный с ТЕСТОВЫМ идентификатором, но живущий в основной сети. Ни
Tonkeeper, ни бэкенд к нему не обращаются: оба считают W5-адрес по правилам
своей сети. Ключ от него — сид-фраза кошелька `xdxdxd`.

Сюда дважды уходили деньги:
  2 TON    — адрес получили перекодированием testnet-формы (так для W5 нельзя);
  0.12 TON — во фронте был ЗАШИТ старый адрес сейфа (константа SAFE_ADDRESS).
Обе причины устранены; скрипт нужен только чтобы забрать застрявшее.

КАК ЗАБИРАЕМ. Собираем клиент с ТЕСТОВЫМ сетевым идентификатором (он даёт нужный
адрес), но с base_url ОСНОВНОЙ сети. Контракт W5 сверяет wallet_id из сообщения
со своим, а не с сетью, — подпись принимает. Уходит весь остаток
(CARRY_ALL_REMAINING_BALANCE + DESTROY_ACCOUNT_IF_ZERO), хвоста не остаётся.

СИД-ФРАЗА. Берётся из файла old_seed.txt рядом со скриптом (24 слова в строку).
Из переменных Railway её брать нельзя: там теперь сид-фраза НОВОГО сейфа
`ruby escrow`, а нужна старая, от `xdxdxd`. После возврата файл удалите.

ЗАПУСК:
    railway run python recover_stranded.py --to <адрес>              # показать, НЕ отправлять
    railway run python recover_stranded.py --to <адрес> --send       # забрать остаток
"""
import asyncio
import os
import sys

from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1
from ton_core.contrib.types import NetworkGlobalID, SendMode

# Куда ушли деньги: W5, собранный с тестовым сетевым идентификатором.
STRANDED = "UQA2-P0sWJofS2PuPFrDln3nyBNJhw2wddDwUhxSU1b0ttEY"

# Именно v2: tonutils работает с Toncenter v2 (ton_client.py ходит в v3 напрямую —
# не перепутать). С v3 клиент молча не находит счёт и показывает нулевой баланс.
MAINNET_URL = "https://toncenter.com/api/v2"

SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "old_seed.txt")


def _read_words() -> list[str]:
    if not os.path.exists(SEED_FILE):
        raise SystemExit(
            f"Нет файла {SEED_FILE}\n"
            "Создайте его и вставьте туда 24 слова кошелька xdxdxd одной строкой.\n"
            "После возврата файл удалите."
        )
    with open(SEED_FILE, encoding="utf-8") as f:
        words = f.read().split()
    if len(words) != 24:
        raise SystemExit(f"В old_seed.txt {len(words)} слов — ожидалось 24.")
    return words


def _arg(name: str) -> str | None:
    if name not in sys.argv:
        return None
    i = sys.argv.index(name)
    if i + 1 >= len(sys.argv):
        raise SystemExit(f"{name} без значения")
    return sys.argv[i + 1]


async def main() -> None:
    send = "--send" in sys.argv
    to = _arg("--to")
    if not to:
        raise SystemExit("Укажите получателя: --to <адрес>")

    client = ToncenterClient(
        NetworkGlobalID.TESTNET,   # только ради адреса W5
        base_url=MAINNET_URL,      # запросы — в основную сеть
        api_key=None,
    )
    wallet, _, _, _ = WalletV5R1.from_mnemonic(client, _read_words())
    addr = wallet.address.to_str(is_bounceable=False, is_test_only=False)

    print("Адрес, которым управляем :", addr)
    print("Ожидался                 :", STRANDED)
    if addr != STRANDED:
        raise SystemExit(
            "АДРЕС НЕ СОВПАЛ — ничего не делаем.\n"
            "Скорее всего, в old_seed.txt слова не от кошелька xdxdxd."
        )
    print("Совпал. Получатель       :", to)

    if not client.connected:
        await client.connect()
    try:
        await wallet.refresh()
        if wallet.info:
            print("Баланс на адресе         :", f"{int(wallet.info.balance) / 1e9:.4f} TON")
    except Exception as e:
        print("Баланс прочитать не вышло:", type(e).__name__, str(e)[:100])

    if not send:
        print()
        print("Это показ без отправки. Чтобы забрать остаток, добавьте --send")
        await client.close()
        return

    print()
    print("Забираем ВЕСЬ остаток и закрываем счёт.")
    ext = await wallet.transfer(
        destination=to,
        amount=0,  # игнорируется при CARRY_ALL_REMAINING_BALANCE
        send_mode=(
            SendMode.CARRY_ALL_REMAINING_BALANCE
            | SendMode.DESTROY_ACCOUNT_IF_ZERO
            | SendMode.IGNORE_ERRORS
        ),
        bounce=False,
        state_init=wallet.state_init,  # счёт uninit — операция разворачивает контракт
    )
    tx = ext.hash.hex() if hasattr(ext, "hash") else str(ext)
    print()
    print("ОТПРАВЛЕНО. Хеш внешнего сообщения:", tx)
    print("Деньги должны появиться через ~10-30 секунд:")
    print(f"    https://tonviewer.com/{to}")
    await client.close()


asyncio.run(main())
