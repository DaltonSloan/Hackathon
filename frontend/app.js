function showTab(index) {
    document.querySelectorAll('.tab').forEach((tab, i) => {
        tab.classList.toggle('active', i === index);
    });
    document.querySelectorAll('.content').forEach((content, i) => {
        content.classList.toggle('active', i === index);
    });
}

// Client-side caches for the dashboard views; data is refreshed every 15 seconds.
let shipmentsData = [];
let defaultShipmentDateBounds = { min: null, max: null };
let materialsData = [];
let activeAlerts = [];
let isPollingShipments = false;
let recommendationsData = [];
let ordersData = [];
let uploadedFile = null;

const API_BASE_URL =
    window.API_BASE_URL ||
    `${window.location.protocol}//${window.location.hostname}:${window.location.port === '8080' || !window.location.port ? '8000' : window.location.port}`;

async function extractErrorMessage(response, fallbackMessage) {
    const contentType = response.headers.get('content-type') || '';

    try {
        if (contentType.includes('application/json')) {
            const payload = await response.json();
            const detail = payload?.detail || payload?.error;
            if (detail) {
                return String(detail);
            }
        }

        const text = await response.text();
        if (text) {
            return text;
        }
    } catch (error) {
        console.debug('Unable to parse error response:', error);
    }

    return fallbackMessage;
}

function formatDisplayDateTime(isoString) {
    if (!isoString) {
        return '—';
    }
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) {
        return isoString;
    }
    return date.toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function extractDateKey(isoString) {
    if (!isoString) {
        return '';
    }
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) {
        return '';
    }
    return date.toISOString().slice(0, 10);
}

function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return value;
    }
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function formatTons(value) {
    const normalized = Number(value) || 0;
    return `${normalized.toLocaleString()} tons`;
}

function classifyMaterialByBin(material) {
    const fillPercentage = Math.max(0, Math.min(100, Math.round((material.fill_ratio || 0) * 100)));
    if (material.needs_reorder) {
        return { label: 'Reorder Needed', tone: 'red', fillPercentage };
    }
    if (fillPercentage >= 80) {
        return { label: 'Healthy Stock', tone: 'green', fillPercentage };
    }
    return { label: 'Moderate Stock', tone: 'yellow', fillPercentage };
}

function getCurrentDate() {
    const today = new Date();
    const year = today.getFullYear();
    const month = String(today.getMonth() + 1).padStart(2, '0');
    const day = String(today.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function setDateInputs(start, end) {
    const startInput = document.getElementById('startDate');
    const endInput = document.getElementById('endDate');
    const rangeInput = document.getElementById('dateRange');

    startInput.value = start;
    endInput.value = end;

    if (start === end) {
        rangeInput.value = start;
    } else {
        rangeInput.value = `${start} to ${end}`;
    }

    resizeDateInput();
}

async function loadShipments() {
    if (isPollingShipments) {
        return;
    }
    isPollingShipments = true;

    try {
        const [deliveriesResponse, materialsResponse, alertsResponse, recommendationsResponse, ordersResponse] = await Promise.all([
            // In production, these calls are fed by live MES/TMS integrations.
            fetch(`${API_BASE_URL}/api/deliveries`),
            fetch(`${API_BASE_URL}/api/materials`),
            fetch(`${API_BASE_URL}/api/alerts`),
            fetch(`${API_BASE_URL}/api/recommendations?days=7`),
            fetch(`${API_BASE_URL}/api/orders`)
        ]);

        if (!deliveriesResponse.ok) {
            throw new Error(`Failed to load deliveries: ${deliveriesResponse.status}`);
        }

        const deliveries = await deliveriesResponse.json();
        materialsData = materialsResponse.ok ? await materialsResponse.json() : [];
        activeAlerts = alertsResponse.ok ? await alertsResponse.json() : [];
        recommendationsData = recommendationsResponse.ok ? await recommendationsResponse.json() : [];
        ordersData = ordersResponse.ok ? await ordersResponse.json() : [];

        renderMaterials();
        renderAlerts();
        renderRecommendations();
        renderOrders();

        populateMaterialDropdown();

        const materialMap = new Map(materialsData.map(material => [material.id, material.type]));

        shipmentsData = deliveries.map((delivery, index) => {
            const status = (delivery.status || 'upcoming').toLowerCase();
            const incomingWeight = Number(delivery.incoming_weight ?? delivery.expected_weight ?? 0);

            let deliveryIso = '';
            if (delivery.delivery_time) {
                const parsed = new Date(delivery.delivery_time);
                deliveryIso = Number.isNaN(parsed.getTime()) ? String(delivery.delivery_time) : parsed.toISOString();
            }

            return {
                id: index,
                deliveryNumber: delivery.delivery_num,
                material: materialMap.get(delivery.material_id) || `Material ${delivery.material_id}`,
                expectedWeight: incomingWeight,
                actualWeight: status === 'completed'
                    ? incomingWeight
                    : Number(delivery.actual_weight ?? delivery.material_weight ?? 0) || 0,
                deliveryDateTime: deliveryIso,
                displayDateTime: formatDisplayDateTime(deliveryIso),
                dateKey: extractDateKey(deliveryIso),
                status
            };
        });

        if (shipmentsData.length > 0) {
            const shipmentDates = shipmentsData
                .map(shipment => shipment.dateKey)
                .filter(Boolean)
                .sort();

            defaultShipmentDateBounds.min = shipmentDates[0];
            defaultShipmentDateBounds.max = shipmentDates[shipmentDates.length - 1];
        }
        
        // Always default to current day
        const today = getCurrentDate();
        setDateInputs(today, today);

        renderShipments();
    } catch (error) {
        console.error('Failed to load dashboard data:', error);
        shipmentsData = [];
        const today = getCurrentDate();
        defaultShipmentDateBounds = { min: today, max: today };
        setDateInputs(today, today);
        renderShipments();
        materialsData = [];
        renderMaterials();
        activeAlerts = [];
        renderAlerts();
        recommendationsData = [];
        renderRecommendations();
        ordersData = [];
        renderOrders();
    } finally {
        isPollingShipments = false;
    }
}

function renderShipments() {
    const tbody = document.getElementById('truckTableBody');
    const startDate = document.getElementById('startDate').value;
    const endDate = document.getElementById('endDate').value;
    tbody.innerHTML = '';
    
    // Filter shipments by date range and sort by status (completed first, then recency)
    const filteredShipments = shipmentsData
        .filter(shipment => {
            if (!shipment.dateKey) {
                return true;
            }
            return shipment.dateKey >= startDate && shipment.dateKey <= endDate;
        })
        .sort((a, b) => {
            if (a.status !== b.status) {
                return a.status === 'completed' ? -1 : 1;
            }
            const aTime = a.deliveryDateTime || '';
            const bTime = b.deliveryDateTime || '';
            return aTime > bTime ? -1 : aTime < bTime ? 1 : 0;
        });
    
    filteredShipments.forEach(shipment => {
        const row = document.createElement('tr');
        row.className = `status-${shipment.status}`;
        const expectedDisplay = Number.isFinite(shipment.expectedWeight)
            ? Number(shipment.expectedWeight).toLocaleString()
            : '—';
        const actualDisplay = Number.isFinite(shipment.actualWeight) && shipment.actualWeight > 0
            ? Number(shipment.actualWeight).toLocaleString()
            : (shipment.status === 'completed' ? expectedDisplay : '—');
        const expectedCell = expectedDisplay === '—' ? '—' : `${expectedDisplay} lbs`;
        const actualCell = actualDisplay === '—' ? '—' : `${actualDisplay} lbs`;
        const statusLabel = shipment.status.charAt(0).toUpperCase() + shipment.status.slice(1);
        row.innerHTML = `
            <td>${shipment.deliveryNumber}</td>
            <td>${shipment.material}</td>
            <td>${expectedCell}</td>
            <td>${actualCell}</td>
            <td>${shipment.displayDateTime}</td>
            <td><span class="status-${shipment.status} status-clickable" data-shipment-id="${shipment.id}">${statusLabel}</span></td>
        `;
        tbody.appendChild(row);
    });
}

function showDatePicker() {
    document.getElementById('datePicker').style.display = 'block';
}

function onStartDateChange() {
    const startDate = document.getElementById('startDate').value;
    const endDateInput = document.getElementById('endDate');
    
    // If start date surpasses end date, update end date to match start date
    if (startDate > endDateInput.value) {
        endDateInput.value = startDate;
    }
    
    updateDateRange();
}

function onEndDateChange() {
    const startDateInput = document.getElementById('startDate');
    const endDate = document.getElementById('endDate').value;
    
    // If end date predates start date, update start date to match end date
    if (endDate < startDateInput.value) {
        startDateInput.value = endDate;
    }
    
    updateDateRange();
}

function resizeDateInput() {
    const input = document.getElementById('dateRange');
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    context.font = '16px Roboto, sans-serif';
    const textWidth = context.measureText(input.value || input.placeholder).width;
    input.style.width = Math.max(textWidth + 30, 100) + 'px';
}

function updateDateRange() {
    const startDate = document.getElementById('startDate').value;
    const endDate = document.getElementById('endDate').value;
    const dateRangeInput = document.getElementById('dateRange');
    
    if (startDate === endDate) {
        dateRangeInput.value = startDate;
    } else {
        dateRangeInput.value = `${startDate} to ${endDate}`;
    }
    
    resizeDateInput();
    renderShipments();
}

function renderMaterials() {
    const cardsContainer = document.getElementById('materialCards');
    const totalWeightLabel = document.getElementById('totalWeight');

    if (!cardsContainer || !totalWeightLabel) {
        return;
    }

    cardsContainer.innerHTML = '';

    if (!Array.isArray(materialsData) || materialsData.length === 0) {
        totalWeightLabel.textContent = 'Total Weight: 0 tons';
        return;
    }

    let runningTotal = 0;

    materialsData.forEach((material) => {
        const weight = Number(material.weight) || 0;
        runningTotal += weight;

        const { label, tone, fillPercentage } = classifyMaterialByBin(material);

        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
            <div class="row">
                <div class="card-header ${tone}">${material.type}</div>
            </div>
            <div class="card-number">${formatTons(weight)}</div>
            <div class="card-subtitle">Fill: ${fillPercentage}% (${Number(material.bins_filled ?? 0).toFixed(2)} bins)</div>
            <div class="card-metrics">σ usage: ${Number(material.consumption_std_tons ?? 0).toFixed(1)} t · σ receipts: ${Number(material.delivery_std_tons ?? 0).toFixed(1)} t</div>
            <div class="card-metrics">Open orders: ${Number(material.open_orders_tons ?? 0).toLocaleString()} t</div>
            <div class="card-indicator ${tone}">${label}</div>
        `;
        cardsContainer.appendChild(card);
    });

    totalWeightLabel.textContent = `Total Weight: ${runningTotal.toLocaleString()} tons`;
}

function renderAlerts() {
    const warningBanner = document.getElementById('iconWarning');
    const warningText = document.getElementById('warningText');

    if (!warningBanner || !warningText) {
        return;
    }

    if (!Array.isArray(activeAlerts) || activeAlerts.length === 0) {
        warningBanner.hidden = true;
        warningBanner.style.display = 'none';
        warningText.innerHTML = '';
        return;
    }

    const filteredAlerts = activeAlerts.filter(alert => alert.alert_level === 'critical');
    if (filteredAlerts.length === 0) {
        warningBanner.hidden = true;
        warningBanner.style.display = 'none';
        warningText.innerHTML = '';
        return;
    }

    const alertMarkup = filteredAlerts
        .map(alert => `<div class="alert-line alert-${alert.alert_level}">${alert.message}</div>`)
        .join('');

    warningText.innerHTML = alertMarkup;
    warningBanner.hidden = false;
    warningBanner.style.display = 'flex';
}

function renderRecommendations() {
    const list = document.getElementById('recommendationsList');
    if (!list) {
        return;
    }

    list.innerHTML = '';

    if (!Array.isArray(recommendationsData) || recommendationsData.length === 0) {
        list.innerHTML = '<div class="recommendation-item"><span>No order actions needed this week.</span></div>';
        return;
    }

    recommendationsData.forEach((rec) => {
        const item = document.createElement('div');
        item.className = 'recommendation-item';

        const left = document.createElement('div');
        left.innerHTML = `<strong>${rec.material_type}</strong><div class="recommendation-meta">Current: ${formatTons(rec.current_weight)} · Avg daily: ${rec.average_daily_consumption.toLocaleString()} tons</div>`;

        const right = document.createElement('div');
        if (rec.recommended_order_tons) {
            const eta = rec.recommended_order_date ? new Date(rec.recommended_order_date).toLocaleDateString() : 'ASAP';
            right.innerHTML = `<strong>${rec.recommended_order_tons.toLocaleString()} tons</strong><div class="recommendation-meta">Order by ${eta}</div>`;
        } else {
            right.innerHTML = `<div class="recommendation-meta">${rec.rationale}</div>`;
        }

        if (rec.pending_orders_tons && rec.pending_orders_tons > 0) {
            const badge = document.createElement('div');
            badge.className = 'badge badge-warning';
            badge.textContent = `Pending ${rec.pending_orders_tons.toLocaleString()} t`;
            right.appendChild(badge);
        }

        item.appendChild(left);
        item.appendChild(right);
        list.appendChild(item);
    });
}

function renderOrders() {
    const list = document.getElementById('ordersList');
    if (!list) {
        return;
    }

    list.innerHTML = '';

    if (!Array.isArray(ordersData) || ordersData.length === 0) {
        list.innerHTML = '<div class="orders-empty">No orders scheduled.</div>';
        return;
    }

    const sorted = [...ordersData].sort((a, b) => new Date(a.requested_date) - new Date(b.requested_date));
    sorted.forEach((order) => {
        const row = document.createElement('div');
        row.className = 'orders-row';
        row.innerHTML = `
            <div class="orders-col">
                <div class="orders-title">${order.material}</div>
                <div class="orders-meta">${order.order_id}</div>
            </div>
            <div class="orders-col">${Number(order.required_tons ?? 0).toLocaleString()} t</div>
            <div class="orders-col">${formatDate(order.requested_date)}</div>
            <div class="orders-col"><span class="badge badge-${order.status}">${order.status}</span></div>
        `;
        list.appendChild(row);
    });
}

// Close date picker when clicking outside
document.addEventListener('click', function(event) {
    const datePicker = document.getElementById('datePicker');
    const dateRange = document.getElementById('dateRange');
    const container = document.querySelector('.date-filter-container');
    
    if (container && !container.contains(event.target)) {
        datePicker.style.display = 'none';
    }
});

function setToday() {
    const today = getCurrentDate();
    setDateInputs(today, today);
    renderShipments();
}

function setWeek() {
    const today = new Date();
    const endDate = new Date(today);
    endDate.setDate(today.getDate() + 6);
    
    const start = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    const end = `${endDate.getFullYear()}-${String(endDate.getMonth() + 1).padStart(2, '0')}-${String(endDate.getDate()).padStart(2, '0')}`;
    
    setDateInputs(start, end);
    renderShipments();
}

function clearRange() {
    if (defaultShipmentDateBounds.min && defaultShipmentDateBounds.max) {
        setDateInputs(defaultShipmentDateBounds.min, defaultShipmentDateBounds.max);
    } else {
        const today = getCurrentDate();
        setDateInputs(today, today);
    }
    document.getElementById('datePicker').style.display = 'none';
    renderShipments();
}

function searchTruck() {
    const input = document.getElementById('truckSearch').value.toLowerCase();
    const table = document.getElementById('truckTable');
    const rows = table.getElementsByTagName('tr');
    
    for (let i = 1; i < rows.length; i++) {
        const truckNumber = rows[i].getElementsByTagName('td')[0].textContent.toLowerCase();
        rows[i].style.display = truckNumber.includes(input) ? '' : 'none';
    }
}

function toggleStatus(shipmentId) {
    const shipment = shipmentsData.find(s => s.id === shipmentId);
    if (shipment) {
        shipment.status = shipment.status === 'completed' ? 'upcoming' : 'completed';
        renderShipments();
        updateJsonFile();
    }
}

function updateJsonFile() {
    console.log('Updated shipments data:', shipmentsData);
    // In a real application, this would send data to a backend API
    // For now, we'll just log the updated data
}

function filterStatus(status) {
    const rows = document.querySelectorAll('#truckTableBody tr');
    rows.forEach((row) => {
        if (status === 'all') {
            row.style.display = '';
        } else {
            row.style.display = row.classList.contains(`status-${status}`) ? '' : 'none';
        }
    });
}



function populateMaterialDropdown() {
    const select = document.getElementById('materialSelect');
    const selectionBlock = document.getElementById('materialSelection');
    if (!select || !selectionBlock) {
        return;
    }

    select.innerHTML = '<option value="">Select a material...</option>';

    if (Array.isArray(materialsData) && materialsData.length > 0) {
        materialsData.forEach((material) => {
            const option = document.createElement('option');
            option.value = material.type;
            option.textContent = material.type;
            select.appendChild(option);
        });
        selectionBlock.hidden = false;
    } else {
        selectionBlock.hidden = true;
    }

    checkSubmitButton();
}

function handleImageUpload(event) {
    const files = event.target.files || event.dataTransfer?.files;
    if (!files || files.length === 0) {
        return;
    }

    uploadedFile = files[0];
    const uploadArea = document.getElementById('uploadArea');
    if (uploadedFile && uploadArea) {
        const reader = new FileReader();
        reader.onload = (e) => {
            uploadArea.innerHTML = `<img src="${e.target.result}" alt="Selected image" style="max-width: 100%; max-height: 220px; border-radius: 12px;">`;
        };
        reader.readAsDataURL(uploadedFile);
    }

    const selectionBlock = document.getElementById('materialSelection');
    if (selectionBlock) {
        selectionBlock.hidden = false;
    }

    checkSubmitButton();
}

function checkSubmitButton() {
    const analyzeButton = document.getElementById('btnAnalyzeImage');
    const materialSelect = document.getElementById('materialSelect');

    if (!analyzeButton) {
        return;
    }

    const hasImage = uploadedFile !== null;
    const hasMaterial = !!(materialSelect && materialSelect.value);
    analyzeButton.disabled = !(hasImage && hasMaterial);
}

async function analyzeImage() {
    const analyzeButton = document.getElementById('btnAnalyzeImage');
    if (!analyzeButton || analyzeButton.disabled || !uploadedFile) {
        return;
    }

    const uploadSection = document.getElementById('uploadSection');
    const loadingSection = document.getElementById('loadingSection');
    const analysisSection = document.getElementById('analysisSection');
    const newImageSection = document.getElementById('newImageSection');
    const resultNumber = document.getElementById('resultNumber');
    const resultDetails = document.getElementById('resultDetails');
    const materialSelect = document.getElementById('materialSelect');
    const uploadedImage = document.getElementById('uploadedImage');
    let previewUrl = '';



    analyzeButton.disabled = true;
    if (materialSelect) {
        materialSelect.disabled = true;
    }


    if (uploadSection) {
        uploadSection.style.display = 'none';
    }
    if (loadingSection) {
        loadingSection.hidden = false;
    }

    if (uploadedImage) {
        previewUrl = URL.createObjectURL(uploadedFile);
        uploadedImage.src = previewUrl;
    }

    try {
        const formData = new FormData();
        formData.append('file', uploadedFile);
        formData.append('material', materialSelect.value);

        const response = await fetch(`${API_BASE_URL}/api/weight`, {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            throw new Error(
                await extractErrorMessage(
                    response,
                    `Image analysis failed (${response.status}).`
                )
            );
        }

        const massHeader = response.headers.get('X-Mass-Short-Ton');
        const blob = await response.blob();
        const imageUrl = URL.createObjectURL(blob);

        if (uploadedImage) {
            uploadedImage.src = imageUrl;
        }

        if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
            previewUrl = '';
        }

        if (resultNumber) {
            resultNumber.textContent = massHeader ? `${Number(massHeader).toFixed(2)} tons` : '-- tons';
        }
        if (resultDetails) {
            resultDetails.textContent = 'Estimated short tons from uploaded image.';
        }
    } catch (error) {
        console.error('Failed to analyze image:', error);
        if (resultNumber) {
            resultNumber.textContent = '-- tons';
        }
        if (resultDetails) {
            resultDetails.textContent = error instanceof Error ? error.message : 'Image analysis failed. Please try again.';
        }
    } finally {
        if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
        }
        if (loadingSection) {
            loadingSection.hidden = true;
        }
        if (analysisSection) {
            analysisSection.style.display = 'flex';
        }
        if (newImageSection) {
            newImageSection.style.display = 'block';
        }
        analyzeButton.disabled = false;
        if (materialSelect) {
            materialSelect.disabled = false;
        }

    }
}


function resetImageUpload() {
    uploadedFile = null;
    const uploadSection = document.getElementById('uploadSection');
    const analysisSection = document.getElementById('analysisSection');
    const newImageSection = document.getElementById('newImageSection');
    const materialSelect = document.getElementById('materialSelect');
    const imageUpload = document.getElementById('imageUpload');
    const uploadArea = document.getElementById('uploadArea');

    if (imageUpload) {
        imageUpload.value = '';
    }
    if (uploadArea) {
        uploadArea.innerHTML = `
            <div class="upload-text">Click to upload image or drag and drop</div>
            <div class="upload-subtext">Supported formats: JPG, PNG, GIF</div>
        `;
    }
    if (uploadSection) {
        uploadSection.style.display = 'block';
    }
    if (analysisSection) {
        analysisSection.style.display = 'none';
    }
    if (newImageSection) {
        newImageSection.style.display = 'none';
    }
    if (materialSelect) {
        materialSelect.disabled = false;
        materialSelect.value = '';
    }

    checkSubmitButton();
}

async function exportInventoryReport() {
    const button = document.getElementById('btnPDFExport');
    if (!button) {
        return;
    }

    // Request the server-side PDF summary and trigger a download when it returns.
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = 'Generating...';

    try {
        const response = await fetch('http://localhost:8000/api/report/inventory');
        if (!response.ok) {
            throw new Error(`Export failed with status ${response.status}`);
        }

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const link = document.createElement('a');
        const timestamp = new Date().toISOString().split('T')[0];
        link.href = url;
        link.download = `inventory-report-${timestamp}.pdf`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        window.URL.revokeObjectURL(url);
    } catch (error) {
        console.error('Failed to export PDF:', error);
        alert('Unable to export the PDF report right now. Please try again shortly.');
    } finally {
        button.disabled = false;
        button.textContent = originalText;
    }
}
window.addEventListener('load', () => {
    document.getElementById('btnPDFExport')?.addEventListener('click', exportInventoryReport);

    document.getElementById('btnAnalyzeImage')?.addEventListener('click', analyzeImage);
    document.getElementById('btnNewImage')?.addEventListener('click', resetImageUpload);
    document.getElementById('materialSelect')?.addEventListener('change', checkSubmitButton);

    const imageUpload = document.getElementById('imageUpload');
    imageUpload?.addEventListener('change', handleImageUpload);
    const uploadArea = document.getElementById('uploadArea');
    if (uploadArea) {
        uploadArea.addEventListener('dragover', (event) => {
            event.preventDefault();
            uploadArea.classList.add('dragover');
        });
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.classList.remove('dragover');
        });
        uploadArea.addEventListener('drop', (event) => {
            event.preventDefault();
            uploadArea.classList.remove('dragover');
            handleImageUpload(event);
        });
    }

    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => showTab(parseInt(tab.dataset.tabIndex)));
    });

    document.getElementById('truckSearch')?.addEventListener('keyup', searchTruck);

    document.getElementById('dateRange')?.addEventListener('click', showDatePicker);
    document.getElementById('startDate')?.addEventListener('change', onStartDateChange);
    document.getElementById('endDate')?.addEventListener('change', onEndDateChange);

    document.getElementById('btnToday')?.addEventListener('click', setToday);
    document.getElementById('btnWeek')?.addEventListener('click', setWeek);
    document.getElementById('btnClear')?.addEventListener('click', clearRange);

    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', (event) => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            filterStatus(event.target.dataset.status);
        });
    });

    document.getElementById('truckTableBody')?.addEventListener('click', (event) => {
        if (event.target.classList.contains('status-clickable')) {
            const shipmentId = parseInt(event.target.dataset.shipmentId);
            toggleStatus(shipmentId);
        }
    });

    loadShipments();
    setInterval(loadShipments, 15000);

    populateMaterialDropdown();
    checkSubmitButton();
});
