"""
Utilities for ingesting shipment data from JSON files into the SQLite database.

Usage:
    python load_shipments.py              # loads data/data_shipments.json
    python load_shipments.py --file path  # load a specific file
    python load_shipments.py --reset      # remove existing deliveries before importing
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

from sqlalchemy import func

try:
    from .database import SessionLocal, init_db  # type: ignore
    from .models import Material, TruckDelivery, MaterialInventoryHistory  # type: ignore
    from .utils import normalize_delivery_status, parse_delivery_timestamp  # type: ignore
except ImportError:  # pragma: no cover
    from database import SessionLocal, init_db  # type: ignore
    from models import Material, TruckDelivery, MaterialInventoryHistory  # type: ignore
    from utils import normalize_delivery_status, parse_delivery_timestamp  # type: ignore

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_JSON = DATA_DIR / "shipments.json"

# Known aliases between shipment payloads and canonical material names stored in the DB.
MATERIAL_ALIASES: Dict[str, str] = {
    "minspar 1fg": "Minspar",
    "3 m lr28": "LR28",
    "3m lr28": "LR28",
    "3m ns 700": "3M NS 700",
    "sms": "SMS Clay",
    "sms clay": "SMS Clay",
    "f1 feldspar": "Feldspar",
}


def canonical_material_name(name: str) -> str:
    """Normalize free-form material names into the canonical DB value."""
    normalized = name.strip()
    canonical = MATERIAL_ALIASES.get(normalized.lower(), normalized)
    return canonical


def get_or_create_material(session, name: str) -> Material:
    """Return an existing material record or create it if missing."""
    canonical = canonical_material_name(name)
    material = (
        session.query(Material)
        .filter(func.lower(Material.type) == canonical.lower())
        .one_or_none()
    )

    if material:
        return material

    material = Material(type=canonical, weight=0.0, humidity=0.0, density=0.0)
    session.add(material)
    session.commit()  # Commit to assign an ID for FK usage downstream.
    session.refresh(material)
    return material


def parse_delivery_datetime(raw: str) -> datetime:
    """Validate and return shipment datetime values for persistence."""
    return parse_delivery_timestamp(raw)


def load_shipments(records: Iterable[dict], *, reset: bool = False) -> None:
    """Persist shipment records, optionally clearing existing deliveries first."""
    # This entry point is shared by CLI imports and potential integrations that provide
    # structured shipment payloads (e.g., nightly exports from logistics systems).
    init_db()
    session = SessionLocal()

    try:
        if reset:
            session.query(TruckDelivery).delete()
            session.query(Material).update({"weight": 0.0})
            timestamp = datetime.now(timezone.utc)
            for material in session.query(Material).all():
                session.add(
                    MaterialInventoryHistory(
                        material_id=material.id,
                        weight=0.0,
                        recorded_at=timestamp,
                    )
                )
            session.commit()

        imported = 0
        updated = 0
        material_totals = defaultdict(float)

        for entry in records:
            delivery_number = entry["deliveryNumber"]
            material_name = entry["material"]
            incoming_weight = float(entry.get("expectedWeight", entry.get("incomingWeight", 0)))
            status = normalize_delivery_status(entry.get("status"), default="upcoming")
            delivery_time = parse_delivery_datetime(entry["deliveryDateTime"])
            material_weight = float(entry.get("actualWeight", entry.get("materialWeight", 0.0)))

            material = get_or_create_material(session, material_name)

            existing: Optional[TruckDelivery] = (
                session.query(TruckDelivery)
                .filter(TruckDelivery.delivery_num == delivery_number)
                .one_or_none()
            )

            if existing:
                setattr(existing, "material_id", material.id)
                setattr(existing, "incoming_weight", incoming_weight)
                setattr(existing, "delivery_time", delivery_time)
                setattr(existing, "status", status)
                updated += 1
            else:
                shipment = TruckDelivery(
                    material_id=material.id,
                    delivery_num=delivery_number,
                    incoming_weight=incoming_weight,
                    delivery_time=delivery_time,
                    status=status,
                )
                session.add(shipment)
                imported += 1

            if status == "completed":
                # Accumulate material-specific totals so we can overwrite inventory
                # with the values present in the import file.
                material_totals[material.id] += material_weight

        session.commit()

        if material_totals:
            timestamp = datetime.now(timezone.utc)
            # Only update weights for materials represented in the shipment data.
            for material_id, total_weight in material_totals.items():
                material = session.get(Material, material_id)
                if material:
                    material.weight = math.floor(total_weight)
                    session.add(
                        MaterialInventoryHistory(
                            material_id=material.id,
                            weight=material.weight,
                            recorded_at=timestamp,
                        )
                    )
            session.commit()

        print(f"Shipments imported: {imported}, updated: {updated}")
    finally:
        session.close()


def load_json_records(path: Path) -> Iterable[dict]:
    if not path.exists():
        raise FileNotFoundError(f"JSON shipments file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_cli_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load shipment JSON data into the SQLite database."
    )
    parser.add_argument(
        "--file",
        "-f",
        type=Path,
        default=DEFAULT_JSON,
        help=f"Path to the shipments JSON file (default: {DEFAULT_JSON})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing truck deliveries before importing.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_cli_args(argv)
    records = load_json_records(args.file)
    load_shipments(records, reset=args.reset)


if __name__ == "__main__":
    main()
