import logging
import os

from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1
from ton_core.contrib.types import NetworkGlobalID
from tonutils.contracts import WalletV5R1, NFTTransferBuilder

logger = logging.getLogger(__name__)

# Адрес сейфа (testnet, WalletV5R1) — эталон для сверки
EXPECTED_ESCROW_ADDRESS = "0QA2-P0sWJofS2PuPFrDln3nyBNJhw2wddDwUhxSU1b0tmqS"

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
    if derived != EXPECTED_ESCROW_ADDRESS:
        raise RuntimeError(
            f"Адрес из мнемоники ({derived}) не совпадает с сейфом "
            f"({EXPECTED_ESCROW_ADDRESS}). Проверь слова и версию кошелька."
        )

    logger.info("🔐 Эскроу-кошелёк инициализирован: %s", derived)
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