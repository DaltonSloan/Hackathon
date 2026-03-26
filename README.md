# Portobello Raw Materials: Proof‑of‑Concept

A production‑style proof of concept that demonstrates how Portobello America can monitor inbound deliveries, live bin inventory, and outbound consumption from a single, portable dashboard. The stack is intentionally lightweight—everything runs in Docker—and every workflow is wired to real REST endpoints so the project can evolve into a production integration.

---

## Highlights

- **End‑to‑end demo** – seeded shipments, real‑time consumption simulator, and scripted “one button” runbooks for demos.
- **Operational dashboard** – static HTML/JS served by Nginx; shows bin health, 7‑day outlook, pending orders, truck history, and computer‑vision weight validation.
- **Analytics ready** – FastAPI backend with SQLAlchemy models captures inventory history, calculates consumption/delivery variability, and issues order recommendations (supports 4‑day lead time + planned orders).
- **Deploy anywhere** – single `docker compose up --build` spins up the full stack; VS Code Dev Container is included for local development.

---

## Repository Layout

| Path | Purpose |
|------|---------|
| `backend/` | FastAPI service, ORM models, simulators, computer-vision helpers |
| `backend/data/shipments.json` | Sample inbound truck data for seeding |
| `backend/data/orders.csv` | Planned outbound orders consumed by `/api/orders` |
| `frontend/` | Dashboard (HTML/CSS/JS) and lightweight Nginx image |
| `docker-compose.yml` | Orchestrates backend + frontend |
| `.devcontainer/` | VS Code Dev Container configuration |

---

## Prerequisites

- Docker Desktop (macOS/Windows) or Docker Engine (Linux)
- Python 3.11 (only if you want to run the backend directly)
- VS Code with the **Dev Containers** extension (optional but recommended)
- Git

---

## Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/<your-org>/Hackathon.git
cd Hackathon
```

### 1.5 First-time setup checks (recommended)
Run these commands to confirm Docker is installed and running before you try to boot the stack:

```bash
docker --version
docker compose version
docker info
```

- If `docker info` fails, start Docker Desktop first, then rerun the command.
- Keep Docker Desktop running while the app is up.

### 2. Launch with Docker Compose (preferred)
```bash
docker compose up --build
```
- Backend REST API: http://localhost:8000
- Interactive API docs (Swagger): http://localhost:8000/docs
- Dashboard: http://localhost:8080
- The default build skips the heavyweight vision stack so the backend image stays fast to build. If you need `/api/weight`, rebuild with `INSTALL_VISION_DEPS=1 docker compose up --build` and provide the SAM/MiDaS model assets noted below.

### 3. Work inside a Dev Container (optional)
1. Open the repo in VS Code.
2. Accept the “Reopen in Container” prompt, or run `Dev Containers: Reopen in Container`.
3. The container installs dependencies with `uv` and starts FastAPI on `8000` with auto‑reload. The frontend continues to run via Compose.

 ---

## Exact Step-By-Step: Run The Full App Locally

Use this checklist if you want a single, copy/paste-friendly sequence.

### Step 1: Open a terminal at project root
```bash
cd /path/to/Hackathon
```

### Step 2: Verify required tools
```bash
docker --version
docker compose version
```

### Step 3: Build and start services
```bash
docker compose up --build
```

Expected behavior:
- Compose builds `backend` and `frontend` images.
- You should eventually see Uvicorn logs for the API and Nginx logs for the frontend.
- Leave this terminal running while you use the app.

### Step 4: Confirm backend is healthy
Open a second terminal and run:

```bash
curl http://localhost:8000/api/health
```

Expected result: JSON response indicating service is healthy.

### Step 5: Open the UI
- Dashboard: http://localhost:8080
- API Docs: http://localhost:8000/docs

### Step 6: Seed demo data (optional, recommended)
```bash
curl -X POST http://localhost:8000/api/demo/seed \
       -H 'Content-Type: application/json' \
       -d '{
                "inventory": [
                   {"material": "Super Strength 2", "weight": 950},
                   {"material": "TN Stone", "weight": 620},
                   {"material": "SMS Clay", "weight": 480},
                   {"material": "Minspar", "weight": 810},
                   {"material": "Sandspar", "weight": 585},
                   {"material": "Feldspar", "weight": 720},
                   {"material": "LR28", "weight": 540}
                ],
                "reset_deliveries": true,
                "reset_history": true
             }'
```

### Step 7: Start simulator (optional)
```bash
curl -X POST http://localhost:8000/api/simulation/demo
```

### Step 8: Stop the app when done
In the terminal running Compose, press `Ctrl+C`, then run:

```bash
docker compose down
```

### Step 9: Remove containers/volumes for a totally clean restart (optional)
```bash
docker compose down -v
```

Use this only if you want to reset local persisted state such as cached container volumes.

 ---

## Detailed Setup & Runbook

### A. Local backend iteration (FastAPI)
1. Create and activate a Python 3.11 virtual environment:
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```
2. Install runtime + test dependencies:
   ```bash
   pip install -r backend/requirements-dev.txt
   ```
   If you need `/api/weight`, also install the optional vision stack:
   ```bash
   pip install -r backend/requirements-torch-cpu.txt
   pip install -r backend/requirements-vision.txt
   ```
3. Launch FastAPI locally:
   ```bash
   uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
   ```
   - API live at `http://localhost:8000`; Swagger docs at `/docs`, ReDoc at `/redoc`.
   - Set `DATABASE_URL` before starting if you prefer Postgres or another SQL backend.
   - Drop `sam_vit_b_01ec64.pth` into `backend/` or configure `SAM_CHECKPOINT_PATH`; also set `TORCH_HOME` if you plan to cache MiDaS weights offline.

### B. Docker-based demo stack
1. Build & start backend + frontend together:
   ```bash
   docker compose up --build
   ```
   - Backend on port `8000`; dashboard on `8080`.
   - Volumes allow local backend edits; restart required for dependency or model changes.
   - To enable `/api/weight` inside Docker, rebuild with `INSTALL_VISION_DEPS=1 docker compose up --build`.
2. Seed the baseline dashboard state (payload matches the Demo Playbook example above):
   ```bash
   curl -X POST http://localhost:8000/api/demo/seed \
        -H 'Content-Type: application/json' \
        -d '{
              "inventory": [
                {"material": "Super Strength 2", "weight": 950},
                {"material": "TN Stone", "weight": 620},
                {"material": "SMS Clay", "weight": 480},
                {"material": "Minspar", "weight": 810},
                {"material": "Sandspar", "weight": 585},
                {"material": "Feldspar", "weight": 720},
                {"material": "LR28", "weight": 540}
              ],
              "reset_deliveries": true,
              "reset_history": true
            }'
   ```
3. Start the simulator helper:
   ```bash
   curl -X POST http://localhost:8000/api/simulation/demo
   ```
   - Stop with `/api/simulation/demo/stop` and poll `/api/simulation/demo/status`.

### C. Data & vision operations
- Use `backend/load_shipments.py` to import `backend/data/shipments.json` (or another file); it canonicalizes aliases (e.g., `SMS → SMS Clay`, `F1 Feldspar → Feldspar`) before writing deliveries.
- Update `backend/data/orders.csv` to describe new outbound plans; `/api/orders` and `/api/recommendations` read directly from that CSV.
- The vision helpers (`segment-anything`, `opencv-python-headless`, `torch`) are optional. Install `backend/requirements-torch-cpu.txt` plus `backend/requirements-vision.txt`, or set `INSTALL_VISION_DEPS=1` for Docker builds, then ensure `sam_vit_b_01ec64.pth` + MiDaS cache are available before exercising `/api/weight`.

### D. Testing checklist
1. With `.venv` active:
   ```bash
   pytest backend/tests
   ```
2. Use `curl` or `httpx` to hit `/api/health`, `/api/materials`, or `/api/orders` for quick verification.
3. After changing SQL models, restart the backend so `init_db` rebuilds tables and reseeds history.

 ---

## Demo Playbook

Once containers are running:

1. **Seed baseline state** (optional but useful for demos)
   ```bash
   curl -X POST http://localhost:8000/api/demo/seed \
        -H 'Content-Type: application/json' \
        -d '{
              "inventory": [
                {"material": "Super Strength 2", "weight": 950},
                {"material": "TN Stone", "weight": 620},
                {"material": "SMS Clay", "weight": 480},
                {"material": "Minspar", "weight": 810},
                {"material": "Sandspar", "weight": 585},
                {"material": "Feldspar", "weight": 720},
                {"material": "LR28", "weight": 540}
              ],
              "reset_deliveries": true,
              "reset_history": true
            }'
   ```
   Feel free to customize the payload—this example mirrors the default dashboard baseline.

2. **Kick off the simulator**
   ```bash
   curl -X POST http://localhost:8000/api/simulation/demo
   ```
   Over the next ~30 seconds the backend:
   - Generates inbound trucks from the seeded shipment set.
   - Simulates hopper consumption based on randomized recipe ratios.
   - Updates alerts, inventory history, and recommendations in the database.

3. **Open the dashboard** – http://localhost:8080
   - **Raw Weight** tab: live bin totals, fill %, consumption/delivery variability, and 7‑day outlook.
   - **Upcoming Orders** panel: pulls from `backend/data/orders.csv`; pending tonnage feeds the recommendation engine.
   - **Truck Data** tab: searchable delivery log, status toggles, live date filters.
   - **Weight Estimation** tab: upload an image (`png/jpg`) to get automated mass estimation using Segment Anything + MiDaS (requires the `sam_vit_b_01ec64.pth` checkpoint to be present).

4. **Stop/reset the demo**
   ```bash
   curl -X POST http://localhost:8000/api/simulation/demo/stop
   ```
   Rerun seeding as needed to return to a clean state.

---

## API Reference (Summary)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Lightweight status check. |
| `/api/materials` | GET | Enriched material list with bin health metrics, usage/delivery standard deviation, and pending orders. |
| `/api/materials` | POST | Create a material (type, weight, humidity, density). |
| `/api/materials/{id}` | PUT | Update material attributes. |
| `/api/materials/{id}/adjust` | POST | Apply manual adjustments (`delta`) for cycle counts, scrap, etc. |
| `/api/deliveries` | GET | Delivery ledger (bill of lading records). |
| `/api/deliveries` | POST | Record a truck delivery; doubles as the Bill of Lading ingest endpoint. |
| `/api/orders` | GET | Planned outbound orders sourced from `backend/data/orders.csv`. |
| `/api/alerts` | GET | Critical low-bin alerts (lead-time aware). |
| `/api/recommendations?days=7` | GET | Seven-day inventory outlook with reorder guidance (considers lead time + pending orders). |
| `/api/report/inventory` | GET | Generates a PDF snapshot with inventory tables, charts, and recommendations. |
| `/api/weight` | POST | Accepts multipart upload (`file`) and `material` type; returns annotated image + mass tons (requires SAM/MiDaS weights). |
| `/api/demo/seed` | POST | Bulk seed materials, deliveries, and history (demo helper). |
| `/api/simulation/demo` | POST | Start the live simulator for `duration_seconds` (default 30). |
| `/api/simulation/demo/stop` | POST | Stop the simulator. |
| `/api/simulation/demo/status` | GET | Returns whether the simulator is running and time remaining. |

Full request/response schemas are always available in the Swagger UI (`/docs`).

---

## Data Inputs & Extensibility

- **Shipments** – `backend/data/shipments.json` seeds historical/completed deliveries for demos. Real deployments can stream deliveries into `/api/deliveries`.
- **Orders** – `backend/data/orders.csv` drives the “Upcoming Orders” widget and is factored into reorder suggestions. Update the CSV and call `/api/recommendations` to see new insight.
- **Material Densities** – `backend/main.py` includes `MATERIAL_DENSITIES` used by the vision-based weight estimation. Add or adjust keys as the portfolio evolves.
- **Computer Vision** – place `sam_vit_b_01ec64.pth` in `backend/` or set `SAM_CHECKPOINT_PATH`. Set `TORCH_HOME` if running offline with pre-downloaded MiDaS weights.

---

## Running Tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements-dev.txt
pytest backend/tests
```

Install `backend/requirements-torch-cpu.txt` plus `backend/requirements-vision.txt` as well if you want to exercise `/api/weight` during local testing.

The suite provisions an isolated SQLite DB for each run so tests remain deterministic.

---

## Deployment Notes

- **Portability** – The project ships with a `.dockerignore` to keep images lean; simply push to any registry and run Docker Compose on the target machine.
- **Scaling** – Swap SQLite for Postgres by setting `DATABASE_URL` (e.g., `postgresql+psycopg2://user:pass@host:5432/dbname`) before deploying.
- **CI Ready** – Add a simple workflow that runs `pip install -r backend/requirements-dev.txt` followed by `pytest backend/tests` to guard against regressions.

---

## LinkedIn‑Friendly Summary

> Delivered a fully containerized proof-of-concept for Portobello America that consolidates inbound logistics, live bin monitoring, and predictive ordering in one dashboard. The stack couples FastAPI + SQLAlchemy analytics with a static frontend, integrates computer-vision weight checks, and simulates plant operations end-to-end. Ready to demo via `docker compose up --build`.

Feel free to adapt the quote above for your post.

---

## Maintainers

- Dalton Sloan – [LinkedIn](https://www.linkedin.com/in/dalton-w-sloan/)
- Collaborators welcome! Open an issue or submit a pull request to extend the analytics, add new data connectors, or polish the frontend.
