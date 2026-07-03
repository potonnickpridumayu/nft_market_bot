import logging
import os

from tonutils.client import ToncenterV3Client
from tonutils.wallet import WalletV5R1

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
    client = ToncenterV3Client(
        is_testnet=is_testnet,
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