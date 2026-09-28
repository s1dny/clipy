import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_ledger: Path | None = None


def configure(ledger: Path) -> None:
    """Record API spend to this JSON-lines file from now on."""
    global _ledger
    _ledger = ledger


def record(service: str, usd: float, detail: str) -> None:
    """Append one spend entry; a no-op until configure() is called."""
    logger.debug("Cost: %s $%.4f (%s)", service, usd, detail)
    if _ledger is None:
        return
    _ledger.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "service": service,
        "usd": round(usd, 6),
        "detail": detail,
    }
    with _ledger.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry) + "\n")


def load(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    with ledger.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]
