import logging
import os

from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1
from ton_core.contrib.types import NetworkGlobalID
from tonutils.contracts import WalletV5R1, NFTTransferBuilder

logger = logging.getLogger(__name__)

# Эталон для сверки — TON_WALLET_ADDRESS, тот же адрес, что использует ton_client
# для чтения баланса и транзакций. Раньше здесь была зашита testnet-форма сейфа,
# и переключение TON_NETWORK=mainnet роняло приложение на старте: одна и та же
# сид-фраза даёт один и тот же кошелёк в обеих сетях, но записывается он
# по-разному (0Q…/kQ… в testnet, UQ…/EQ… в mainnet), и сверка не сходилась.
# Источник истины теперь один — переменная окружения.
EXPECTED_ESCROW_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "").strip()

_wallet = None


def get_escrow_wallet():
    """Ленивая инициализация кошелька сейфа из ESCROW_MNEMONIC."""
    global _wallet
    if _wallet is not None:
        return _wallet

    mnemonic = os.environ.get("ESCROW_MNEMONIC", "").strip()
    words = mnemonic.split()
    if len(words) != 24:
        raise RuntimeError(
            f"ESCROW_MNEMONIC: ожидалось 24 слова, получено {len(words)}"
        )

    is_testnet = os.environ.get("TON_NETWORK", "testnet") == "testnet"
    network = NetworkGlobalID.TESTNET if is_testnet else NetworkGlobalID.MAINNET
    client = ToncenterClient(
        network,
        api_key=os.environ.get("TON_API_KEY"),
    )

    wallet, _, _, _ = WalletV5R1.from_mnemonic(client, words)

    derived = wallet.address.to_str(is_bounceable=False, is_test_only=is_testnet)
    if not EXPECTED_ESCROW_ADDRESS:
        raise RuntimeError(
            "TON_WALLET_ADDRESS не задан — не с чем сверить адрес из мнемоники. "
            f"Кошелёк из ESCROW_MNEMONIC даёт {derived} (сеть: "
            f"{'testnet' if is_testnet else 'mainnet'})."
        )
    if derived != EXPECTED_ESCROW_ADDRESS:
        raise RuntimeError(
            f"Адрес из мнемоники ({derived}) не совпадает с TON_WALLET_ADDRESS "
            f"({EXPECTED_ESCROW_ADDRESS}). Сеть: {'testnet' if is_testnet else 'mainnet'}. "
            f"Проверь слова, версию кошелька и что адрес записан в форме этой сети "
            f"(testnet — 0Q…, mainnet — UQ…)."
        )

    logger.info("🔐 Эскроу-кошелёк инициализирован: %s (сеть: %s)",
                derived, "testnet" if is_testnet else "mainnet")
    _wallet = wallet
    return _wallet

async def send_nft(nft_address: str, to_address: str, comment: str | None = None) -> str:
    """Отправить NFT с сейфа. Возвращает хеш внешнего сообщения."""
    wallet = get_escrow_wallet()
    if not wallet.client.connected:
        await wallet.client.connect()
    builder = NFTTransferBuilder(
        destination=to_address,
        nft_address=nft_address,
        response_address=wallet.address,  # сдача с газа — обратно на сейф
        forward_payload=comment,          # комментарий получателю (или None)
        forward_amount=1,                 # 1 нанотон — стандарт для нотификации
        bounce=True,  # при фейле ончейн газ вернётся на сейф
    )
    ext = await wallet.transfer_message(builder)
    logger.info("📤 NFT %s отправлен на %s", nft_address, to_address)
    return ext.hash.hex() if hasattr(ext, "hash") else str(ext)

async def send_ton(to_address: str, amount_ton: float, comment: str | None = None) -> str:
    """Отправить обычные TON с сейфа. Возвращает хеш внешнего сообщения."""
    wallet = get_escrow_wallet()
    if not wallet.client.connected:
        await wallet.client.connect()
    ext = await wallet.transfer(
        destination=to_address,
        amount=int(amount_ton * 1_000_000_000),  # нанотоны
        body=comment,
        bounce=False,  # чтобы дошло даже на неинициализированный кошелёк
    )
    logger.info("📤 %s TON отправлено на %s", amount_ton, to_address)
    return ext.hash.hex() if hasattr(ext, "hash") else str(ext)