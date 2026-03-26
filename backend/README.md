# Backend Service (FastAPI)

This folder contains the FastAPI application that powers the Portobello Raw Materials proof-of-concept. Most developers will interact with the project from the repository root (`../README.md`), but the notes below capture service‑specific details.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

To enable `/api/weight` locally, install the optional vision stack separately:

```bash
pip install -r requirements-torch-cpu.txt
pip install -r requirements-vision.txt
```

Environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `DATABASE_URL` | SQLAlchemy connection string | `sqlite:///./materials.db` |
| `SAM_CHECKPOINT_PATH` | Segment Anything checkpoint for vision-based weight estimation | `./sam_vit_b_01ec64.pth` |
| `TORCH_HOME` | Torch hub cache directory (useful when running offline) | Torch default |

## Data seeding & utilities

- `data/shipments.json` – starter deliveries for demo mode. Modify or replace to suit your scenario.
- `data/orders.csv` – upcoming orders consumed by `/api/orders` and factored into reorder recommendations.
- `load_shipments.py` – CLI helper to bulk import shipment JSON:  
  `python load_shipments.py --file data/shipments.json`

## Testing

```bash
pytest ../backend/tests
```

Tests run against an ephemeral SQLite database and include coverage for materials, deliveries, orders, and demo helpers.

Refer back to the root documentation for the complete project overview, API reference, and demo playbook.
