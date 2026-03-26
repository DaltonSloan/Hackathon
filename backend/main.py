import asyncio
import math
import random
import statistics
from collections import deque, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Deque, Dict, Generator, List, Optional, Tuple
from uuid import uuid4
try:  # Allow both package and flat module imports
    from .img_model import ImageModelDependencyError, calc_weight  # type: ignore
except ImportError:  # pragma: no cover - used in Docker image where modules live flat
    from img_model import ImageModelDependencyError, calc_weight

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

try:
    from .database import SessionLocal, init_db  # type: ignore
    from .load_shipments import (
        DEFAULT_JSON as SHIPMENTS_DEFAULT_JSON,
        canonical_material_name,
        load_json_records,
    )
    from .utils import normalize_delivery_status, parse_delivery_timestamp
    from .models import Material, TruckDelivery, MaterialInventoryHistory
    from .orders import load_orders, OrderRecord  # type: ignore
    from .schemas import (
        BillOfLading,
        BillOfLadingSimulationConfig,
        DemoStatus,
        DemoSeedRequest,
        MaterialAlert,
        MaterialRead,
        MaterialRecommendation,
        MaterialCreate,
        MaterialUpdate,
        MaterialAdjustRequest,
        DeliveryCreateRequest,
        SensorEvent,
        TruckDeliveryRead,
        OrderSummary,
    )
except ImportError:  # pragma: no cover
    from database import SessionLocal, init_db  # type: ignore
    from load_shipments import (
        DEFAULT_JSON as SHIPMENTS_DEFAULT_JSON,
        canonical_material_name,
        load_json_records,
    )
    from utils import normalize_delivery_status, parse_delivery_timestamp
    from models import Material, TruckDelivery, MaterialInventoryHistory
    from orders import load_orders, OrderRecord  # type: ignore
    from schemas import (
        BillOfLading,
        BillOfLadingSimulationConfig,
        DemoStatus,
        DemoSeedRequest,
        MaterialAlert,
        MaterialRead,
        MaterialRecommendation,
        MaterialCreate,
        MaterialUpdate,
        MaterialAdjustRequest,
        DeliveryCreateRequest,
        SensorEvent,
        TruckDeliveryRead,
        OrderSummary,
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # Create tables and preload materials
    try:
        yield
    finally:
        await _stop_demo()

app = FastAPI(
    title="Raw Materials API",
    description="API for Portobello America's raw materials dashboard.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # open for hackathon simplicity
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=['X-Mass-Short-Ton']
)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


CARRIER_CHOICES = [
    "L&H Express",
    "Swift Logistics",
    "Portobello Fleet",
    "Clay Haulers Co.",
    "Blue Ridge Transit",
]

BIN_CAPACITY_TONS = 1200.0
REORDER_THRESHOLD_RATIO = 0.5
WARNING_THRESHOLD_RATIO = 0.65
ORDER_LEAD_TIME_DAYS = 4
RESTOCK_TARGET_RATIO = 0.95

DEMO_STATE: dict = {
    "task": None,
    "start_time": None,
    "duration": None,
    "delivery_queue": deque(),
}

# Demo loops use these constants; replace with real recipe or schedule once production ready.
DEMO_CONSUMPTION_TONS = 2.5  # TODO: replace with actual batch size (tons) derived from live recipe data
DEMO_CONSUMPTION_INTERVAL = 1.5  # seconds between simulated consumption batches
DEMO_DELIVERY_INTERVAL = 3.0  # seconds between simulated inbound deliveries
DEMO_DELIVERY_CONFIG = BillOfLadingSimulationConfig(
    min_trucks_per_day=60,  # TODO: replace with real truck arrival rates
    max_trucks_per_day=120,
    min_tons=18.0,  # TODO: replace with actual expected load ranges
    max_tons=32.0,
)

_CLOSED_ORDER_STATUSES = {"completed", "fulfilled", "delivered", "cancelled"}


def _orders_by_material() -> Dict[str, List[OrderRecord]]:
    orders_map: Dict[str, List[OrderRecord]] = defaultdict(list)
    for order in load_orders():
        key = canonical_material_name(order.material).strip().lower()
        orders_map[key].append(order)
    return orders_map


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _daily_consumption_deltas(db: Session, material_id: int, days: int = 7) -> List[float]:
    end = _utc_now()
    start = end - timedelta(days=days)
    history = (
        db.query(MaterialInventoryHistory)
        .filter(
            MaterialInventoryHistory.material_id == material_id,
            MaterialInventoryHistory.recorded_at >= start,
        )
        .order_by(MaterialInventoryHistory.recorded_at.asc())
        .all()
    )
    if len(history) < 2:
        return []
    deltas: List[float] = []
    for prev, curr in zip(history, history[1:]):
        delta = (prev.weight or 0.0) - (curr.weight or 0.0)
        if delta > 0:
            deltas.append(delta)
    return deltas


def _delivery_amounts(db: Session, material_id: int, lookback_days: int = 30) -> List[float]:
    cutoff = _utc_now() - timedelta(days=lookback_days)
    deliveries = (
        db.query(TruckDelivery)
        .filter(
            TruckDelivery.material_id == material_id,
            TruckDelivery.status == "completed",
        )
        .all()
    )
    amounts: List[float] = []
    for delivery in deliveries:
        delivered_at = _ensure_utc(delivery.delivery_time)
        if delivered_at < cutoff:
            continue
        try:
            incoming = float(delivery.incoming_weight or 0.0)
        except TypeError:
            incoming = 0.0
        amounts.append(round(incoming / 2000.0, 2))
    return amounts


def _material_metrics(material: Material, db: Session, orders_map: Optional[Dict[str, List[OrderRecord]]] = None) -> dict:
    if orders_map is None:
        orders_map = _orders_by_material()

    consumption_deltas = _daily_consumption_deltas(db, material.id)
    consumption_std = statistics.stdev(consumption_deltas) if len(consumption_deltas) > 1 else 0.0

    delivery_amounts = _delivery_amounts(db, material.id)
    delivery_std = statistics.stdev(delivery_amounts) if len(delivery_amounts) > 1 else 0.0

    key = material.type.strip().lower()
    material_orders = orders_map.get(key, [])
    open_orders_total = sum(
        order.required_tons
        for order in material_orders
        if order.status not in _CLOSED_ORDER_STATUSES
    )

    fill_ratio = material.weight / BIN_CAPACITY_TONS if BIN_CAPACITY_TONS else 0.0
    bins_filled = material.weight / BIN_CAPACITY_TONS if BIN_CAPACITY_TONS else 0.0
    return {
        "id": material.id,
        "type": material.type,
        "weight": material.weight,
        "humidity": material.humidity,
        "density": material.density,
        "bin_capacity": BIN_CAPACITY_TONS,
        "fill_ratio": max(fill_ratio, 0.0),
        "bins_filled": max(bins_filled, 0.0),
        "needs_reorder": material.weight <= BIN_CAPACITY_TONS * REORDER_THRESHOLD_RATIO,
        "consumption_std_tons": round(consumption_std, 2),
        "delivery_std_tons": round(delivery_std, 2),
        "open_orders_tons": round(open_orders_total, 2),
    }


def _record_history(db: Session, materials: List[Material]) -> None:
    """Persist an inventory snapshot for each material in *materials*."""
    timestamp = _utc_now()
    for material in materials:
        db.add(
            MaterialInventoryHistory(
                material_id=material.id,
                weight=material.weight,
                recorded_at=timestamp,
            )
        )


def _material_alerts(
    material: Material,
    db: Session,
    orders_map: Optional[Dict[str, List[OrderRecord]]] = None,
) -> List[MaterialAlert]:
    """Return alert records for *material*, including lead-time guidance when capacity is low."""
    metrics = _material_metrics(material, db, orders_map)
    alerts: List[MaterialAlert] = []
    ratio = metrics["fill_ratio"]

    if ratio <= REORDER_THRESHOLD_RATIO:
        alerts.append(
                MaterialAlert(
                    material_id=material.id,
                    material_type=material.type,
                    weight=material.weight,
                    fill_ratio=ratio,
                    bins_filled=metrics["bins_filled"],
                    alert_level="critical",
                    message=(
                        f"{material.type} bin below 50% capacity "
                        f"({material.weight:.1f} tons remaining; {(ratio * 100):.0f}% full). "
                        f"Lead time is {ORDER_LEAD_TIME_DAYS} days—place an order now."
                    ),
            )
        )

    return alerts


def _material_status(fill_ratio: float) -> Tuple[str, str]:
    """Map a fill ratio to a human-friendly status string and its accent color."""
    if fill_ratio <= REORDER_THRESHOLD_RATIO:
        return "Critical", "#dc2626"
    if fill_ratio <= WARNING_THRESHOLD_RATIO:
        return "Warning", "#f97316"
    return "Healthy", "#16a34a"


def _demo_running() -> bool:
    task = DEMO_STATE["task"]
    return task is not None and not task.done()


def _demo_seconds_remaining() -> Optional[float]:
    if not _demo_running():
        return None
    start = DEMO_STATE.get("start_time")
    duration = DEMO_STATE.get("duration")
    if not start or not duration:
        return None
    start_utc = _ensure_utc(start)
    elapsed = (_utc_now() - start_utc).total_seconds()
    return max(0.0, duration - elapsed)


def _generate_recommendations(db: Session, days: int = 7) -> List[MaterialRecommendation]:
    """Forecast depletion for each material using trailing history and return order advice."""
    lookback = _utc_now() - timedelta(days=days)
    recommendations: List[MaterialRecommendation] = []

    materials = db.query(Material).all()
    orders_map = _orders_by_material()
    for material in materials:
        evaluation_time = _utc_now()
        key = material.type.strip().lower()
        material_orders = orders_map.get(key, [])
        horizon_seconds = days * 86400
        orders_within_window = [
            order
            for order in material_orders
            if order.status not in _CLOSED_ORDER_STATUSES
            and 0.0 <= (order.requested_date - evaluation_time).total_seconds() <= horizon_seconds
        ]
        orders_weight_tons = sum(order.required_tons for order in orders_within_window)
        order_summaries = [
            OrderSummary(
                order_id=order.order_id,
                material=order.material,
                requested_date=order.requested_date,
                required_tons=round(order.required_tons, 2),
                status=order.status,
            )
            for order in sorted(orders_within_window, key=lambda item: item.requested_date)
        ]

        history = (
            db.query(MaterialInventoryHistory)
            .filter(
                MaterialInventoryHistory.material_id == material.id,
                MaterialInventoryHistory.recorded_at >= lookback,
            )
            .order_by(MaterialInventoryHistory.recorded_at.asc())
            .all()
        )

        if not history:
            history = (
                db.query(MaterialInventoryHistory)
                .filter(MaterialInventoryHistory.material_id == material.id)
                .order_by(MaterialInventoryHistory.recorded_at.desc())
                .limit(10)
                .all()
            )
            history = list(reversed(history))

        if len(history) < 2:
            recommendations.append(
                MaterialRecommendation(
                    material_id=material.id,
                    material_type=material.type,
                    current_weight=material.weight,
                    average_daily_consumption=0.0,
                    projected_weight_seven_days=material.weight,
                    days_until_reorder=None,
                    recommended_order_date=None,
                    recommended_order_tons=None,
                    rationale="Insufficient history to estimate consumption",
                    pending_orders_tons=round(orders_weight_tons, 2),
                    upcoming_orders=order_summaries,
                )
            )
            continue

        total_consumption = 0.0
        for prev, curr in zip(history, history[1:]):
            delta = prev.weight - curr.weight
            if delta > 0:
                total_consumption += delta

        first_ts = _ensure_utc(history[0].recorded_at)
        last_ts = _ensure_utc(history[-1].recorded_at)
        span_seconds = (last_ts - first_ts).total_seconds()
        span_days = max(span_seconds / 86400, 1.0)
        avg_daily = total_consumption / span_days if total_consumption > 0 else 0.0

        projected_weight = material.weight - avg_daily * days
        projected_weight -= orders_weight_tons
        reorder_threshold = BIN_CAPACITY_TONS * REORDER_THRESHOLD_RATIO
        lead_time = ORDER_LEAD_TIME_DAYS
        days_until_reorder: Optional[float] = None
        order_offset: Optional[float] = None

        if avg_daily > 0:
            days_until_reorder = max(
                0.0, (material.weight - reorder_threshold) / avg_daily
            )
            order_offset = max(days_until_reorder - lead_time, 0.0)

        recommended_tons = None
        recommended_date = None
        rationale = "Inventory stable; no reorder expected within outlook"

        if avg_daily <= 0.01:
            rationale = "No consumption detected; monitoring only"
        else:
            target_weight = BIN_CAPACITY_TONS * RESTOCK_TARGET_RATIO
            lead_consumption = avg_daily * lead_time
            projected_at_arrival = material.weight - lead_consumption
            refill_amount = max(0.0, target_weight - projected_at_arrival)

            if refill_amount > 0:
                recommended_tons = round(refill_amount, 2)
                max_capacity_delta = max(0.0, BIN_CAPACITY_TONS - material.weight)
                if max_capacity_delta > 0:
                    recommended_tons = min(recommended_tons, round(max_capacity_delta, 2))

            if days_until_reorder is not None:
                if days_until_reorder <= lead_time:
                    recommended_date = evaluation_time
                    rationale = (
                        f"Reorder threshold hit in {days_until_reorder:.1f} days. "
                        f"With a {lead_time}-day lead time, place an order immediately."
                    )
                elif order_offset is not None and order_offset <= days:
                    recommended_date = evaluation_time + timedelta(days=order_offset)
                    rationale = (
                        f"Reorder threshold expected in {days_until_reorder:.1f} days. "
                        f"Order in {order_offset:.1f} days ({recommended_date.date()}) to account for "
                        f"the {lead_time}-day lead time."
                    )
                elif projected_weight <= reorder_threshold:
                    recommended_date = evaluation_time + timedelta(days=days)
                    rationale = (
                        "Inventory projected to fall below threshold beyond current outlook; "
                        "begin planning for a replenishment."
                    )
                else:
                    recommended_date = None
                    recommended_tons = None
                    rationale = (
                        "Inventory remains above reorder threshold for the selected horizon."
                    )

        if orders_weight_tons > 0:
            rationale = (
                f"{rationale} Pending orders in next {days} days: {orders_weight_tons:.0f} tons."
            )

        recommendations.append(
            MaterialRecommendation(
                material_id=material.id,
                material_type=material.type,
                current_weight=material.weight,
                average_daily_consumption=round(avg_daily, 2),
                projected_weight_seven_days=round(projected_weight, 2),
                days_until_reorder=round(days_until_reorder, 2) if days_until_reorder is not None else None,
                recommended_order_date=recommended_date,
                recommended_order_tons=round(recommended_tons, 2) if recommended_tons is not None else None,
                rationale=rationale,
                pending_orders_tons=round(orders_weight_tons, 2),
                upcoming_orders=order_summaries,
            )
        )

    return recommendations


def _inventory_history_series(
    db: Session, materials: List[Material], days: int = 7
) -> Tuple[List[str], List[Tuple[str, List[Tuple[int, float]]]]]:
    """Return aligned daily history series for the last *days* days (inclusive)."""
    if not materials:
        return [], []

    end_date = _utc_now().date()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    date_window = [
        start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)
    ]
    labels = [date.strftime("%b %d") for date in date_window]
    history_series: List[Tuple[str, List[Tuple[int, float]]]] = []

    if not date_window:
        return labels, history_series

    start_dt = datetime.combine(date_window[0], datetime.min.time())
    end_dt = datetime.combine(date_window[-1], datetime.max.time())

    for material in materials:
        records = (
            db.query(MaterialInventoryHistory)
            .filter(
                MaterialInventoryHistory.material_id == material.id,
                MaterialInventoryHistory.recorded_at >= start_dt,
                MaterialInventoryHistory.recorded_at <= end_dt,
            )
            .order_by(MaterialInventoryHistory.recorded_at.asc())
            .all()
        )

        daily_snapshot: dict = {}
        for record in records:
            recorded_at = _ensure_utc(record.recorded_at)
            snapshot_date = recorded_at.date()
            if date_window[0] <= snapshot_date <= date_window[-1]:
                daily_snapshot[snapshot_date] = float(record.weight)

        points: List[Tuple[int, float]] = []
        last_value: Optional[float] = None
        for index, date in enumerate(date_window):
            value = daily_snapshot.get(date)
            if value is None:
                value = last_value if last_value is not None else float(material.weight)
            last_value = value
            points.append((index, round(value, 2)))

        history_series.append((material.type, points))

    return labels, history_series


def _build_inventory_report(
    db: Session,
    materials: List[Material],
    recommendations: List[MaterialRecommendation],
    history_labels: List[str],
    history_series: List[Tuple[str, List[Tuple[int, float]]]],
    orders_map: Optional[Dict[str, List[OrderRecord]]] = None,
) -> BytesIO:
    """Render a PDF snapshot with inventory tables, fill chart, trends, and recommendation outlook."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.widgets.markers import makeMarker
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    generated_at = _utc_now().strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph("Raw Materials Inventory Report", styles["Title"]))
    story.append(Paragraph(f"Generated on {generated_at}", styles["Normal"]))
    story.append(Spacer(1, 0.3 * inch))

    header_style = styles["Heading2"]
    story.append(Paragraph("Inventory Levels", header_style))
    story.append(Spacer(1, 0.1 * inch))

    table_data = [["Material", "Weight (tons)", "Fill %", "Status"]]
    fill_percentages: List[float] = []
    fill_colors = []
    material_names: List[str] = []

    for material in sorted(materials, key=lambda item: item.type.lower()):
        metrics = _material_metrics(material, db, orders_map)
        fill_pct = round(metrics["fill_ratio"] * 100, 1)
        status_label, status_hex = _material_status(metrics["fill_ratio"])
        status_color = colors.HexColor(status_hex)
        table_data.append(
            [
                metrics["type"],
                f"{metrics['weight']:.1f}",
                f"{fill_pct:.1f}%",
                status_label,
            ]
        )
        material_names.append(metrics["type"])
        fill_percentages.append(fill_pct)
        fill_colors.append(status_color)

    inventory_table = Table(
        table_data,
        colWidths=[2.2 * inch, 1.5 * inch, 1.3 * inch, 1.4 * inch],
        repeatRows=1,
    )
    inventory_table_style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 1), (2, -1), "RIGHT"),
            ("ALIGN", (3, 1), (3, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5f5")),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]
    )
    for index, status_color in enumerate(fill_colors, start=1):
        inventory_table_style.add("TEXTCOLOR", (-1, index), (-1, index), status_color)
    inventory_table.setStyle(inventory_table_style)

    story.append(inventory_table)
    story.append(Spacer(1, 0.3 * inch))

    if material_names:
        story.append(Paragraph("Inventory Fill Chart", header_style))
        story.append(Spacer(1, 0.1 * inch))
        chart_width = 5.5 * inch
        chart_height = 2.5 * inch
        drawing = Drawing(chart_width, chart_height)
        chart = VerticalBarChart()
        chart.x = 40
        chart.y = 30
        chart.height = chart_height - 60
        chart.width = chart_width - 60
        chart.data = [fill_percentages]
        chart.categoryAxis.categoryNames = material_names
        chart.categoryAxis.labels.angle = 45
        chart.categoryAxis.labels.boxAnchor = "ne"
        chart.categoryAxis.labels.dx = -10
        chart.categoryAxis.labels.dy = -20
        chart.valueAxis.valueMin = 0
        chart.valueAxis.valueMax = 100
        chart.valueAxis.valueStep = 10
        chart.barWidth = 12
        chart.groupSpacing = 12
        chart.strokeColor = colors.transparent
        chart.valueAxis.visibleGrid = True
        chart.valueAxis.gridStrokeColor = colors.HexColor("#cbd5f5")
        chart.valueAxis.gridStrokeWidth = 0.5
        for idx, bar_color in enumerate(fill_colors):
            chart.bars[(0, idx)].fillColor = bar_color
            chart.bars[(0, idx)].strokeWidth = 0
        drawing.add(chart)
        story.append(drawing)
        story.append(Spacer(1, 0.3 * inch))

    if history_series and history_labels:
        story.append(Paragraph("Inventory Trend (Last 7 Days)", header_style))
        story.append(Spacer(1, 0.1 * inch))
        chart_width = 5.5 * inch
        chart_height = 2.6 * inch
        trend_drawing = Drawing(chart_width, chart_height)
        line_plot = LinePlot()
        line_plot.x = 50
        line_plot.y = 40
        line_plot.height = chart_height - 70
        line_plot.width = chart_width - 90
        line_plot.data = [series for _, series in history_series]
        line_plot.xValueAxis.valueMin = 0
        line_plot.xValueAxis.valueMax = max(
            (point[0] for _, series in history_series for point in series), default=0
        )
        line_plot.xValueAxis.valueStep = 1
        line_plot.xValueAxis.labels.fontSize = 8
        line_plot.xValueAxis.labels.angle = 35
        line_plot.xValueAxis.labels.boxAnchor = "ne"
        line_plot.xValueAxis.labels.dy = -16
        line_plot.xValueAxis.labels.dx = -12
        line_plot.xValueAxis.labelTextFormat = lambda value: history_labels[int(value)] if 0 <= int(value) < len(history_labels) else ""

        max_weight = max((point[1] for _, series in history_series for point in series), default=0.0)
        if max_weight <= 0:
            max_weight = 100.0
        ceiling = max(50.0, math.ceil(max_weight / 50.0) * 50.0)
        line_plot.yValueAxis.valueMin = 0
        line_plot.yValueAxis.valueMax = ceiling
        line_plot.yValueAxis.valueStep = max(100.0, ceiling / 5.0)
        line_plot.yValueAxis.visibleGrid = True
        line_plot.yValueAxis.gridStrokeColor = colors.HexColor("#e2e8f0")
        line_plot.yValueAxis.gridStrokeWidth = 0.5
        line_plot.yValueAxis.labels.fontSize = 8
        line_plot.yValueAxis.labels.boxAnchor = "e"

        palette = [
            colors.HexColor("#2563eb"),
            colors.HexColor("#16a34a"),
            colors.HexColor("#f97316"),
            colors.HexColor("#9333ea"),
            colors.HexColor("#0ea5e9"),
            colors.HexColor("#f59e0b"),
            colors.HexColor("#ef4444"),
        ]

        for idx, _ in enumerate(history_series):
            stroke_color = palette[idx % len(palette)]
            line_plot.lines[idx].strokeColor = stroke_color
            line_plot.lines[idx].strokeWidth = 1.4
            marker = makeMarker("Circle")
            marker.size = 3
            marker.fillColor = stroke_color
            marker.strokeColor = stroke_color
            line_plot.lines[idx].symbol = marker

        trend_drawing.add(line_plot)

        legend = Legend()
        legend.x = line_plot.x + line_plot.width + 10
        legend.y = line_plot.y + line_plot.height - 20
        legend.alignment = "left"
        legend.fontSize = 7
        legend.boxAnchor = "nw"
        legend.columnMaximum = 3
        legend.deltax = 65
        legend.deltay = 6
        legend.strokeWidth = 0
        legend.colorNamePairs = [
            (line_plot.lines[idx].strokeColor, history_series[idx][0])
            for idx in range(len(history_series))
        ]
        trend_drawing.add(legend)

        story.append(trend_drawing)
        story.append(Spacer(1, 0.3 * inch))

    if recommendations:
        story.append(Paragraph("Recommended Ordering Outlook", header_style))
        story.append(Spacer(1, 0.1 * inch))
        rec_table_data = [
            [
                "Material",
                "Avg Daily (t)",
                "Days to Threshold",
                "Order Date",
                "Order Tons",
                "Notes",
            ]
        ]
        body_style = styles["BodyText"]
        for rec in sorted(recommendations, key=lambda item: item.days_until_reorder or 999):
            days_to_reorder = (
                f"{rec.days_until_reorder:.1f}" if rec.days_until_reorder is not None else "—"
            )
            order_date = (
                rec.recommended_order_date.strftime("%Y-%m-%d")
                if rec.recommended_order_date
                else "—"
            )
            order_tons = (
                f"{rec.recommended_order_tons:.1f}" if rec.recommended_order_tons else "—"
            )
            avg_daily = f"{rec.average_daily_consumption:.1f}"
            rec_table_data.append(
                [
                    rec.material_type,
                    avg_daily,
                    days_to_reorder,
                    order_date,
                    order_tons,
                    Paragraph(rec.rationale, body_style),
                ]
            )

        rec_table = Table(
            rec_table_data,
            colWidths=[1.6 * inch, 1.1 * inch, 1.2 * inch, 1.2 * inch, 1.1 * inch, 2.4 * inch],
            repeatRows=1,
        )
        rec_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("ALIGN", (1, 1), (4, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5f5")),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(rec_table)

    doc.build(story)
    buffer.seek(0)
    return buffer


def _generate_recipe(materials: List[Material], min_percent: float) -> List[float]:
    if not materials:
        return []
    # TODO: replace heuristic allocation with actual recipe percentages per material.
    # For production, read formula ratios from your MES/ERP and remove the random draw below.

    # Cap the minimum to avoid exceeding 100% while using random fallback
    min_cap = min(min_percent, 100 / len(materials))

    draws = [random.random() for _ in materials]
    total = sum(draws) or 1
    base = [(value / total) * 100 for value in draws]

    clipped = [max(min_cap, percent) for percent in base]
    scale = 100 / sum(clipped)
    scaled = [percent * scale for percent in clipped]

    # Fix rounding drift on the last element
    rounded = [round(percent, 2) for percent in scaled]
    drift = 100 - sum(rounded)
    rounded[-1] = round(rounded[-1] + drift, 2)
    return rounded


def _apply_recipe_batch(
    db: Session,
    total_tons: float,
    min_percent: float,
) -> List[SensorEvent]:
    materials = db.query(Material).all()
    if not materials or total_tons <= 0:
        return []

    percentages = _generate_recipe(materials, min_percent)
    events: List[SensorEvent] = []
    changed_materials: List[Material] = []

    for material, percent in zip(materials, percentages):
        draw = round(total_tons * (percent / 100), 2)
        if draw <= 0:
            continue

        delta = -draw
        material.apply_weight_change(delta)
        changed_materials.append(material)
        events.append(
            SensorEvent(
                material_id=material.id,
                material_type=material.type,
                delta=delta,
                remaining_weight=material.weight,
                timestamp=_utc_now(),
            )
        )

    db.commit()
    return events


def _generate_bol_payload(
    db: Session, config: BillOfLadingSimulationConfig
) -> Optional[BillOfLading]:
    materials = db.query(Material).all()
    if not materials:
        return None

    material: Optional[Material] = None
    target_ratio: Optional[float] = None

    queue: Deque[Tuple[int, float]] = DEMO_STATE.get("delivery_queue")  # type: ignore
    if queue:
        while queue:
            forced_id, forced_ratio = queue.popleft()
            candidate = next((item for item in materials if item.id == forced_id), None)
            if candidate:
                material = candidate
                target_ratio = forced_ratio
                break

    if material is None or target_ratio is None:
        selection = _select_demo_delivery_material(materials)
        if not selection:
            return None
        material, target_ratio = selection
    deficit_tons = max(BIN_CAPACITY_TONS * target_ratio - material.weight, 0.0)
    if deficit_tons <= 0:
        return None

    tons = min(max(deficit_tons, config.min_tons), config.max_tons)
    tons = round(tons, 2)
    net_lb = round(tons * 2000, 2)
    tare_lb = round(random.uniform(25000, 32000), 2)
    gross_lb = round(net_lb + tare_lb, 2)

    delivery_number = f"SIM-{int(_utc_now().timestamp())}-{random.randint(1000, 9999)}"
    carrier = random.choice(CARRIER_CHOICES)

    return BillOfLading(
        delivery_number=delivery_number,
        po_number=f"PO-{uuid4().hex[:6].upper()}",
        customer="Portobello Americas Inc.",
        product=material.type.replace(" ", "_").upper(),
        net_weight_lb=net_lb,
        gross_weight_lb=gross_lb,
        tare_weight_lb=tare_lb,
        carrier=carrier,
        delivery_date=_utc_now(),
        material_code=material.type,
    )


async def _demo_runner(duration_seconds: int) -> None:
    """Drive the scripted demo loop (consumption + deliveries) for *duration_seconds*."""
    loop = asyncio.get_running_loop()
    end_time = loop.time() + duration_seconds

    next_consumption = loop.time()
    next_delivery = loop.time()

    try:
        while loop.time() < end_time:
            now = loop.time()

            if now >= next_consumption:
                session = SessionLocal()
                try:
                    _apply_recipe_batch(
                        session,
                        total_tons=DEMO_CONSUMPTION_TONS,
                        min_percent=5.0,
                    )
                finally:
                    session.close()
                next_consumption += DEMO_CONSUMPTION_INTERVAL

            if now >= next_delivery:
                session = SessionLocal()
                try:
                    payload = _generate_bol_payload(session, DEMO_DELIVERY_CONFIG)
                    if payload:
                        _process_bill_of_lading(session, payload)
                finally:
                    session.close()
                next_delivery += DEMO_DELIVERY_INTERVAL

            await asyncio.sleep(0.25)
    finally:
        DEMO_STATE.update({"task": None, "start_time": None, "duration": None})


async def _stop_demo() -> None:
    task: Optional[asyncio.Task] = DEMO_STATE["task"]
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    DEMO_STATE.update({"task": None, "start_time": None, "duration": None})
    DEMO_STATE["delivery_queue"] = deque()


@app.get("/api/health")
def health_check():
    return {"status": "running"}

@app.get("/api/materials", response_model=List[MaterialRead])
def get_materials(db: Session = Depends(get_db)) -> List[MaterialRead]:
    materials = db.query(Material).all()
    orders_map = _orders_by_material()
    return [
        MaterialRead.model_validate(_material_metrics(material, db, orders_map))
        for material in materials
    ]


@app.post("/api/materials", response_model=MaterialRead, status_code=201)
def create_material(
    payload: MaterialCreate,
    db: Session = Depends(get_db),
) -> MaterialRead:
    """Provision a new raw material (hook this to master-data ingestion in production)."""
    existing = (
        db.query(Material)
        .filter(func.lower(Material.type) == payload.type.lower())
        .one_or_none()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Material already exists")

    material = Material(
        type=payload.type,
        weight=payload.weight,
        humidity=payload.humidity,
        density=payload.density,
    )
    db.add(material)
    db.flush()
    _record_history(db, [material])
    db.commit()
    db.refresh(material)
    orders_map = _orders_by_material()
    return MaterialRead.model_validate(_material_metrics(material, db, orders_map))


@app.put("/api/materials/{material_id}", response_model=MaterialRead)
def update_material(
    material_id: int,
    payload: MaterialUpdate,
    db: Session = Depends(get_db),
) -> MaterialRead:
    """Update material metadata or on-hand weight from your live system."""
    material = db.get(Material, material_id)
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    if payload.type and payload.type.lower() != material.type.lower():
        conflict = (
            db.query(Material)
            .filter(func.lower(Material.type) == payload.type.lower(), Material.id != material_id)
            .one_or_none()
        )
        if conflict:
            raise HTTPException(status_code=409, detail="Material name already in use")
        material.type = payload.type

    weight_changed = False
    if payload.weight is not None:
        material.weight = payload.weight
        weight_changed = True
    if payload.humidity is not None:
        material.humidity = payload.humidity
    if payload.density is not None:
        material.density = payload.density

    if weight_changed:
        _record_history(db, [material])

    db.commit()
    db.refresh(material)
    orders_map = _orders_by_material()
    return MaterialRead.model_validate(_material_metrics(material, db, orders_map))


@app.post("/api/materials/{material_id}/adjust", response_model=MaterialRead)
def adjust_material(
    material_id: int,
    payload: MaterialAdjustRequest,
    db: Session = Depends(get_db),
) -> MaterialRead:
    """Apply ad-hoc adjustments (e.g., manual counts, QA scrap)."""
    material = db.get(Material, material_id)
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    material.apply_weight_change(payload.delta)
    _record_history(db, [material])
    db.commit()
    db.refresh(material)
    orders_map = _orders_by_material()
    return MaterialRead.model_validate(_material_metrics(material, db, orders_map))


@app.get("/api/alerts", response_model=List[MaterialAlert])
def get_alerts(db: Session = Depends(get_db)) -> List[MaterialAlert]:
    materials = db.query(Material).all()
    alerts: List[MaterialAlert] = []
    orders_map = _orders_by_material()
    for material in materials:
        alerts.extend(_material_alerts(material, db, orders_map))
    return alerts


@app.get("/api/recommendations", response_model=List[MaterialRecommendation])
def get_recommendations(days: int = 7, db: Session = Depends(get_db)) -> List[MaterialRecommendation]:
    days = max(1, min(days, 30))
    return _generate_recommendations(db, days=days)


@app.get("/api/orders", response_model=List[OrderSummary])
def get_orders() -> List[OrderSummary]:
    orders = load_orders()
    return [
        OrderSummary(
            order_id=order.order_id,
            material=order.material,
            requested_date=order.requested_date,
            required_tons=round(order.required_tons, 2),
            status=order.status,
        )
        for order in sorted(orders, key=lambda item: item.requested_date)
    ]


@app.get("/api/report/inventory")
def export_inventory_report(db: Session = Depends(get_db)) -> StreamingResponse:
    """Stream a PDF report summarizing inventory levels and ordering outlook."""
    materials = db.query(Material).order_by(Material.type.asc()).all()
    recommendations = _generate_recommendations(db, days=7)
    history_labels, history_series = _inventory_history_series(db, materials, days=7)
    orders_map = _orders_by_material()
    pdf_buffer = _build_inventory_report(db, materials, recommendations, history_labels, history_series, orders_map)
    headers = {
        "Content-Disposition": "attachment; filename=inventory-report.pdf",
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        iter([pdf_buffer.getvalue()]),
        media_type="application/pdf",
        headers=headers,
    )


def _distribute_consumption(total: float, periods: int) -> List[float]:
    if total <= 0 or periods <= 0:
        return [0.0] * max(periods, 0)
    draws = [random.uniform(0.6, 1.4) for _ in range(periods)]
    scale = total / (sum(draws) or 1.0)
    values = [round(draw * scale, 2) for draw in draws]
    drift = round(total - sum(values), 2)
    values[-1] = round(values[-1] + drift, 2)
    return values


def _shape_demo_levels(materials: List[Material]) -> List[Material]:
    if not materials:
        return []
    ordered = sorted(materials, key=lambda material: material.id)
    profiles = [0.94, 0.82, 0.68, 0.48, 0.42, 0.59, 0.75]
    for index, material in enumerate(ordered):
        ratio = profiles[index] if index < len(profiles) else profiles[-1]
        target_weight = round(BIN_CAPACITY_TONS * ratio, 2)
        material.weight = max(0.0, min(target_weight, BIN_CAPACITY_TONS))
    return ordered


def _material_fill_ratio(material: Material) -> float:
    if BIN_CAPACITY_TONS <= 0:
        return 0.0
    return max(0.0, material.weight / BIN_CAPACITY_TONS)


def _select_demo_delivery_material(materials: List[Material]) -> Optional[Tuple[Material, float]]:
    if not materials:
        return None

    lows = [
        material
        for material in materials
        if _material_fill_ratio(material) <= REORDER_THRESHOLD_RATIO
    ]
    if lows:
        material = min(lows, key=_material_fill_ratio)
        target = random.uniform(0.9, 0.96)
        return material, target

    mediums = [
        material
        for material in materials
        if REORDER_THRESHOLD_RATIO < _material_fill_ratio(material) <= WARNING_THRESHOLD_RATIO
    ]
    if mediums and random.random() < 0.7:
        material = min(mediums, key=_material_fill_ratio)
        target = random.uniform(0.83, 0.9)
        return material, target

    highs = [
        material
        for material in materials
        if _material_fill_ratio(material) > WARNING_THRESHOLD_RATIO
    ]
    if highs and random.random() < 0.2:
        material = random.choice(highs)
        current_ratio = _material_fill_ratio(material)
        baseline = random.uniform(0.76, 0.85)
        target = max(baseline, current_ratio + 0.02)
        target = min(target, 0.95)
        return material, target

    return None


def _seed_demo_history(
    db: Session,
    materials: List[Material],
    *,
    days: int = 7,
    replace: bool = True,
) -> None:
    if not materials:
        return

    base_time = _utc_now() - timedelta(days=days)

    for material in materials:
        if replace:
            db.query(MaterialInventoryHistory).filter(
                MaterialInventoryHistory.material_id == material.id
            ).delete()
        current_weight = float(material.weight or 0.0)
        extra = random.uniform(80, 220)
        start_weight = min(
            BIN_CAPACITY_TONS * 0.98,
            max(current_weight + 40.0, current_weight + extra),
        )
        total_consumption = max(start_weight - current_weight, 0.0)
        if total_consumption <= 1e-2:
            total_consumption = max(60.0, current_weight * 0.25)
            start_weight = min(
                BIN_CAPACITY_TONS * 0.98, current_weight + total_consumption
            )

        consumption_series = _distribute_consumption(total_consumption, days)
        timestamp = base_time
        running_weight = start_weight

        for draw in consumption_series:
            db.add(
                MaterialInventoryHistory(
                    material_id=material.id,
                    weight=round(max(running_weight, 0.0), 2),
                    recorded_at=timestamp,
                )
            )
            running_weight = max(running_weight - draw, 0.0)
            timestamp += timedelta(days=1)

        db.add(
            MaterialInventoryHistory(
                material_id=material.id,
                weight=round(current_weight, 2),
                    recorded_at=_utc_now(),
            )
        )


def _add_future_delivery(
    db: Session,
    materials: List[Material],
    *,
    status: str,
    material: Optional[Material] = None,
    target_ratio: Optional[float] = None,
) -> None:
    if not materials and material is None:
        return
    if material is None:
        material = random.choice(materials)
    normalized_status = normalize_delivery_status(status, default="upcoming")
    horizon_hours = (4, 12) if normalized_status == "upcoming" else (12, 36)
    delivery_time = _utc_now() + timedelta(hours=random.randint(*horizon_hours))
    if target_ratio is None:
        target_ratio = 0.92 if normalized_status == "upcoming" else 0.78
    target_ratio = max(0.55, min(target_ratio, 0.97))
    target_weight = BIN_CAPACITY_TONS * target_ratio
    deficit_tons = max(target_weight - material.weight, 0.0)
    if deficit_tons <= 0:
        return
    incoming_tons = min(
        max(deficit_tons, DEMO_DELIVERY_CONFIG.min_tons),
        DEMO_DELIVERY_CONFIG.max_tons,
    )
    incoming_weight_lb = round(incoming_tons * 2000, 2)
    delivery = TruckDelivery(
        material_id=material.id,
        delivery_num=f"DEMO-{uuid4().hex[:6].upper()}-{normalized_status[:3].upper()}",
        incoming_weight=incoming_weight_lb,
        delivery_time=delivery_time,
        status=normalized_status,
    )
    db.add(delivery)


def _ensure_demo_deliveries(db: Session, materials: List[Material]) -> None:
    try:
        records = list(load_json_records(SHIPMENTS_DEFAULT_JSON))
    except FileNotFoundError:
        records = []
    except Exception:
        records = []

    existing_deliveries = db.query(TruckDelivery).all()
    existing_numbers = {delivery.delivery_num for delivery in existing_deliveries}

    for delivery in existing_deliveries:
        normalized = normalize_delivery_status(delivery.status)
        if delivery.status != normalized:
            delivery.status = normalized
    db.flush()
    material_index = {
        material.type.strip().lower(): material for material in materials
    }

    for entry in records:
        delivery_number = entry.get("deliveryNumber")
        if not delivery_number or delivery_number in existing_numbers:
            continue
        canonical = canonical_material_name(entry.get("material", "")).strip().lower()
        material = material_index.get(canonical)
        if not material:
            continue
        status = normalize_delivery_status(entry.get("status"), default="upcoming")
        raw_delivery_time = entry.get(
            "deliveryDateTime",
            _utc_now().strftime("%Y-%m-%d %H:%M"),
        )
        delivery_time = parse_delivery_timestamp(raw_delivery_time)
        db.add(
            TruckDelivery(
                material_id=material.id,
                delivery_num=delivery_number,
                incoming_weight=float(entry.get("incomingWeight", 0.0)),
                delivery_time=delivery_time,
                status=status,
            )
        )
        existing_numbers.add(delivery_number)

    db.flush()

    if not db.query(TruckDelivery).filter(TruckDelivery.status == "upcoming").first():
        _add_future_delivery(db, materials, status="upcoming")

    reorder_threshold = BIN_CAPACITY_TONS * REORDER_THRESHOLD_RATIO
    for material in materials:
        existing_upcoming_count = (
            db.query(TruckDelivery)
            .filter(
                TruckDelivery.material_id == material.id,
                TruckDelivery.status == "upcoming",
            )
            .count()
        )

        if material.weight <= reorder_threshold:
            required = 2
            deficit = max(0, required - existing_upcoming_count)
            for _ in range(deficit):
                _add_future_delivery(
                    db,
                    materials,
                    status="upcoming",
                    material=material,
                    target_ratio=0.9,
                )
        elif material.weight <= reorder_threshold * 1.1 and existing_upcoming_count == 0:
            _add_future_delivery(
                db,
                materials,
                status="upcoming",
                material=material,
                target_ratio=0.85,
            )

    focus_materials = {
        "sms clay": [
            {"count": 3, "target_ratio": 0.96},
            {"count": 5, "target_ratio": 0.9},
        ],
        "feldspar": [
            {"count": 3, "target_ratio": 0.95},
            {"count": 5, "target_ratio": 0.88},
        ],
    }
    for name, targets in focus_materials.items():
        material = next(
            (item for item in materials if item.type.strip().lower() == name),
            None,
        )
        if not material:
            continue
        for config in targets:
            target_count = max(0, int(config.get("count", 0)))
            if target_count <= 0:
                continue
            ratio_hint = float(config.get("target_ratio", 0.9))
            existing = (
                db.query(TruckDelivery)
                .filter(
                    TruckDelivery.material_id == material.id,
                    TruckDelivery.status == "upcoming",
                )
                .count()
            )
            for _ in range(max(0, target_count - existing)):
                _add_future_delivery(
                    db,
                    materials,
                    status="upcoming",
                    material=material,
                    target_ratio=ratio_hint,
                )


def _prepare_demo_delivery_queue() -> None:
    """Prime the demo delivery queue with priority loads for key materials."""
    session = SessionLocal()
    try:
        materials = session.query(Material).all()
        queue: Deque[Tuple[int, float]] = deque()
        focus_plan = [
            ("sms clay", 0.97, 4),
            ("feldspar", 0.95, 4),
        ]
        for name, ratio, count in focus_plan:
            material = next(
                (item for item in materials if item.type.strip().lower() == name),
                None,
            )
            if not material:
                continue
            for _ in range(count):
                queue.append((material.id, ratio))
        DEMO_STATE["delivery_queue"] = queue
    finally:
        session.close()

@app.post("/api/demo/seed", response_model=List[MaterialRead])
def seed_demo_inventory(
    payload: DemoSeedRequest,
    db: Session = Depends(get_db),
) -> List[MaterialRead]:
    if not payload.inventory:
        raise HTTPException(status_code=400, detail="Inventory list cannot be empty")

    normalized_inventory = {}
    canonical_lookup = {}
    for item in payload.inventory:
        if not item.material.strip():
            continue
        canonical = canonical_material_name(item.material).strip()
        key = canonical.lower()
        normalized_inventory[key] = item
        canonical_lookup[key] = canonical

    if not normalized_inventory:
        raise HTTPException(status_code=400, detail="No valid material names supplied")

    materials_by_name = {
        material.type.strip().lower(): material
        for material in db.query(Material).all()
    }

    updated_materials: List[Material] = []

    for key, item in normalized_inventory.items():
        canonical = canonical_lookup[key]
        material = materials_by_name.get(key)
        if material:
            material.type = canonical
            material.weight = item.weight
            if item.humidity is not None:
                material.humidity = item.humidity
            if item.density is not None:
                material.density = item.density
            updated_materials.append(material)
        else:
            material = Material(
                type=canonical,
                weight=item.weight,
                humidity=item.humidity or 0.0,
                density=item.density or 0.0,
            )
            db.add(material)
            updated_materials.append(material)
            materials_by_name[key] = material

    db.flush()

    all_materials = list(db.query(Material).order_by(Material.id.asc()).all())
    shaped_materials = _shape_demo_levels(all_materials)
    unique_materials = {material.id: material for material in updated_materials}
    for material in shaped_materials:
        unique_materials.setdefault(material.id, material)
    updated_materials = list(unique_materials.values())
    db.flush()

    if payload.reset_deliveries:
        db.query(TruckDelivery).delete()
        db.flush()

    if payload.reset_history:
        db.query(MaterialInventoryHistory).delete()
        db.flush()
        _seed_demo_history(db, all_materials, replace=False)
    else:
        needs_history: List[Material] = []
        for material in updated_materials:
            existing_points = (
                db.query(MaterialInventoryHistory)
                .filter(MaterialInventoryHistory.material_id == material.id)
                .count()
            )
            if existing_points < 2:
                needs_history.append(material)

        if needs_history:
            _seed_demo_history(db, needs_history, replace=True)

        needs_history_ids = {material.id for material in needs_history}
        remaining = [
            material
            for material in updated_materials
            if material.id not in needs_history_ids
        ]
        if remaining:
            _record_history(db, remaining)

    _ensure_demo_deliveries(db, all_materials)

    db.commit()

    all_materials = db.query(Material).order_by(Material.type.asc()).all()
    orders_map = _orders_by_material()
    return [
        MaterialRead.model_validate(_material_metrics(material, db, orders_map))
        for material in all_materials
    ]


@app.post(
    "/api/simulation/demo",
    response_model=DemoStatus,
    status_code=202,
)
async def start_demo(duration_seconds: int = 30) -> DemoStatus:
    if _demo_running():
        raise HTTPException(status_code=409, detail="Demo already running")

    await _stop_demo()

    _prepare_demo_delivery_queue()

    start_time = _utc_now()
    DEMO_STATE.update({"start_time": start_time, "duration": duration_seconds})
    DEMO_STATE["task"] = asyncio.create_task(_demo_runner(duration_seconds))

    return DemoStatus(
        running=True,
        seconds_remaining=float(duration_seconds),
        started_at=start_time,
        duration=duration_seconds,
    )


@app.post("/api/simulation/demo/stop", response_model=DemoStatus)
async def stop_demo() -> DemoStatus:
    await _stop_demo()
    return DemoStatus(running=False, seconds_remaining=None, started_at=None, duration=None)


@app.get("/api/simulation/demo/status", response_model=DemoStatus)
def demo_status() -> DemoStatus:
    running = _demo_running()
    return DemoStatus(
        running=running,
        seconds_remaining=_demo_seconds_remaining(),
        started_at=DEMO_STATE.get("start_time"),
        duration=DEMO_STATE.get("duration"),
    )


@app.get("/api/deliveries", response_model=List[TruckDeliveryRead])
def get_deliveries(db: Session = Depends(get_db)) -> List[TruckDeliveryRead]:
    return db.query(TruckDelivery).all()


@app.post("/api/deliveries", response_model=TruckDeliveryRead, status_code=201)
def create_delivery(
    payload: DeliveryCreateRequest,
    db: Session = Depends(get_db),
) -> TruckDeliveryRead:
    """Record a real truck delivery; wire this to your scale/TMS feed."""
    bol = BillOfLading(
        delivery_number=payload.delivery_num,
        po_number=None,
        customer=None,
        product=payload.material_code,
        net_weight_lb=payload.incoming_weight_lb,
        gross_weight_lb=None,
        tare_weight_lb=None,
        carrier=None,
        delivery_date=payload.delivery_time,
        material_code=payload.material_code,
    )
    delivery = _process_bill_of_lading(db, bol)
    if payload.status:
        desired_status = normalize_delivery_status(payload.status, default="completed")
        if delivery.status != desired_status:
            delivery.status = desired_status
            db.commit()
            db.refresh(delivery)
    return delivery


def _process_bill_of_lading(db: Session, payload: BillOfLading) -> TruckDelivery:
    material_code = canonical_material_name(payload.material_code).strip()
    material = (
        db.query(Material)
        .filter(func.lower(Material.type) == material_code.lower())
        .one_or_none()
    )
    if not material:
        material = Material(
            type=material_code,
            weight=0.0,
            humidity=0.0,
            density=0.0,
        )
        db.add(material)
        db.flush()  # assign id

    delivery = (
        db.query(TruckDelivery)
        .filter(TruckDelivery.delivery_num == payload.delivery_number)
        .one_or_none()
    )

    if delivery:
        delivery.incoming_weight = payload.net_weight_lb
        delivery.material_id = material.id
        delivery.delivery_time = payload.delivery_date
        delivery.status = "completed"
    else:
        delivery = TruckDelivery(
            delivery_num=payload.delivery_number,
            incoming_weight=payload.net_weight_lb,
            material_id=material.id,
            delivery_time=payload.delivery_date,
            status="completed",
        )
        db.add(delivery)

    material.apply_weight_change(payload.to_short_tons())
    _record_history(db, [material])

    db.commit()
    db.refresh(delivery)
    return delivery


@app.post(
    "/api/bol/import",
    response_model=TruckDeliveryRead,
    status_code=201,
)
def import_bill_of_lading(
    payload: BillOfLading,
    db: Session = Depends(get_db),
) -> TruckDeliveryRead:
    """Ingest a structured bill of lading payload and update material inventory."""
    delivery = _process_bill_of_lading(db, payload)
    return delivery


MATERIAL_DENSITIES = {
    "super strength 2": 9.2,
    "tn stone": 10.2,
    "sms": 11.8,
    "sms clay": 11.8,
    "3m ns 700": 11.2,
    "feldspar": 9.8,
    "f1 feldspar": 9.8,
    "green scrap": 7.4,
    "sandspar": 8.8,
    "minspar": 12.4,
    "lr28": 11.2,
    "fired scrap": 12.2,
}

@app.post("/api/weight")
async def estimate_image_weight(file: UploadFile = File(...), material: str = Form(...)):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Image estimation dependencies are not installed. "
                "Install the optional vision stack to use /api/weight."
            ),
        ) from exc

    material_key = material.lower().strip()
    if material_key not in MATERIAL_DENSITIES:
        raise HTTPException(status_code=400, detail=f"Unknown material: {material}")
    
    density_lbs_per_gal = MATERIAL_DENSITIES[material_key]
    
    contents = await file.read()
    np_arr = np.frombuffer(contents, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="Invalid image file.")
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    try:
        overlay, mass_tons = calc_weight(image, density_lbs_per_gal)
    except (FileNotFoundError, ImageModelDependencyError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _, buffer = cv2.imencode(".png", overlay)
    overlay_bytes = BytesIO(buffer.tobytes())
    return StreamingResponse(
        overlay_bytes,
        media_type="image/png",
        headers={"X-Mass-Short-Ton": f"{mass_tons:.2f}"}
    )
