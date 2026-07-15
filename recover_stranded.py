"""Разовый возврат 2 TON, ушедших на неверный адрес сейфа (15.07.2026).

ЧТО ПРОИЗОШЛО. Адрес кошелька версии W5 зависит от сети: сетевой идентификатор
входит в инициализацию контракта, поэтому одна сид-фраза даёт РАЗНЫЕ адреса в
testnet и mainnet. Тестовый адрес сейфа перекодировали в форму основной сети —
для W5 так нельзя, и 2 TON ушли на UQA2…ttEY: адрес реальный и наш (ключ тот
же), но ни Tonkeeper, ни бэкенд к нему не обращаются — оба считают W5-адрес по
правилам основной сети и видят UQB4…Vw_5.

КАК ЗАБИРАЕМ. Собираем клиент с ТЕСТОВЫМ сетевым идентификатором (он даёт нужный
адрес UQA2…ttEY), но с base_url ОСНОВНОЙ сети — то есть кошелёк тот, а ходим в
mainnet. Контракт W5 сверяет wallet_id из сообщения со своим, а не с сетью, так
что подпись он примет. Уходит весь остаток (CARRY_ALL_REMAINING_BALANCE +
DESTROY_ACCOUNT_IF_ZERO), хвоста на брошенном адресе не остаётся.

ЗАПУСК:
    railway run python recover_stranded.py               # только показать, НЕ отправлять
    railway run python recover_stranded.py --amount 0.05 # пробный перевод малой суммы
    railway run python recover_stranded.py --send        # забрать весь остаток

Порядок: сначала --amount 0.05 (проверяем, что контракт вообще принимает нашу
подпись — это единственное, что нельзя проверить, не отправив), убеждаемся в
блокчейне, что монеты дошли, и только потом --send.

Ключ toncenter НЕ используется: в переменных лежит тестовый, для основной сети
он не подойдёт. Без ключа лимит 1 запрос/сек — для одного перевода достаточно.
"""
import asyncio
import os
import sys

from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1
from ton_core.contrib.types import NetworkGlobalID, SendMode

# Куда ушли деньги: W5, собранный с тестовым сетевым идентификатором.
STRANDED = "UQA2-P0sWJofS2PuPFrDln3nyBNJhw2wddDwUhxSU1b0ttEY"
# Настоящий сейф в основной сети: W5 с идентификатором mainnet.
ESCROW = "UQB4vUXjoZBrSPVnwtJOcSneR-tj5KxpWKc9LR_guvn9Vw_5"

# Именно v2: tonutils работает с Toncenter v2 (в ton_client.py, где запросы идут
# напрямую через aiohttp, используется v3 — не перепутать). С v3 клиент молча
# не находит счёт и показывает нулевой баланс.
MAINNET_URL = "https://toncenter.com/api/v2"


def _parse_amount() -> float | None:
    """--amount 0.05 → пробный перевод на 0.05 TON. Без флага → None."""
    if "--amount" not in sys.argv:
        return None
    i = sys.argv.index("--amount")
    if i + 1 >= len(sys.argv):
        raise SystemExit("--amount без числа: укажите, напр. --amount 0.05")
    return float(sys.argv[i + 1].replace(",", "."))


async def main() -> None:
    send = "--send" in sys.argv
    amount = _parse_amount()
    if amount is not None and send:
        raise SystemExit("--amount и --send вместе не имеют смысла: "
                         "--amount шлёт часть, --send забирает всё.")

    words = os.environ["ESCROW_MNEMONIC"].split()
    if len(words) != 24:
        raise SystemExit(f"ESCROW_MNEMONIC: ожидалось 24 слова, получено {len(words)}")

    client = ToncenterClient(
        NetworkGlobalID.TESTNET,   # только ради адреса W5
        base_url=MAINNET_URL,      # запросы — в основную сеть
        api_key=None,              # тестовый ключ тут не подойдёт
    )
    wallet, _, _, _ = WalletV5R1.from_mnemonic(client, words)
    addr = wallet.address.to_str(is_bounceable=False, is_test_only=False)

    print("Адрес, которым управляем :", addr)
    print("Ожидался                 :", STRANDED)
    if addr != STRANDED:
        raise SystemExit("АДРЕС НЕ СОВПАЛ — ничего не делаем.")
    print("Совпал. Отправляем на сейф:", ESCROW)

    if not client.connected:
        await client.connect()
    try:
        await wallet.refresh()
        bal = wallet.info.balance if wallet.info else None
        if bal is not None:
            print("Баланс на адресе         :", f"{int(bal) / 1e9:.4f} TON")
    except Exception as e:
        print("Баланс прочитать не вышло:", type(e).__name__, str(e)[:100])

    if not send and amount is None:
        print()
        print("Это показ без отправки. Дальше по порядку:")
        print("  1) проба малой суммой:  railway run python recover_stranded.py --amount 0.05")
        print("  2) забрать весь остаток: railway run python recover_stranded.py --send")
        await client.close()
        return

    if amount is not None:
        print()
        print(f"ПРОБНЫЙ перевод: {amount} TON (остальное останется на месте)")
        ext = await wallet.transfer(
            destination=ESCROW,
            amount=int(amount * 1_000_000_000),
            bounce=False,  # сейф ещё не развёрнут — деньги не должны отскочить
            state_init=wallet.state_init,  # первая операция разворачивает контракт
        )
    else:
        print()
        print("Забираем ВЕСЬ остаток и закрываем счёт.")
        ext = await wallet.transfer(
            destination=ESCROW,
            amount=0,  # игнорируется при CARRY_ALL_REMAINING_BALANCE
            send_mode=(
                SendMode.CARRY_ALL_REMAINING_BALANCE
                | SendMode.DESTROY_ACCOUNT_IF_ZERO
                | SendMode.IGNORE_ERRORS
            ),
            bounce=False,
            state_init=wallet.state_init,
        )

    tx = ext.hash.hex() if hasattr(ext, "hash") else str(ext)
    print()
    print("ОТПРАВЛЕНО. Хеш внешнего сообщения:", tx)
    print("Деньги должны появиться здесь через ~10-30 секунд:")
    print(f"    https://tonviewer.com/{ESCROW}")
    await client.close()


asyncio.run(main())
