
let barChartInstance = null; // Chart.js horizontal bar chart
let currentSymbol = 'NIFTY';
let currentExpiry = '';

let priceChartInstance = null;
let oiLightweightChartInstance = null; // Rename to avoid clash with Chart.js instance
let candleSeries = null;
let volumeSeries = null;
let peChgSeries = null;
let ceChgSeries = null;
let isSyncingRange = false;
let isSyncingCrosshair = false;
let lastCandlesArray = [];
let priceChartResizeObserver = null;
let oiChartResizeObserver = null;
let initialChartZoomSet = false;

// Option Reference Lines
let atmPriceLine = null;
let callWallPriceLine = null;
let putWallPriceLine = null;

// Option Dashboard State
let chartMode = 'index'; // 'index' | 'multistrike' | 'cumoi'
let oiBuildupMode = 'total'; // 'total' | 'change'
let multiStrikeLineMode = 'absolute'; // 'absolute' | 'change'
let volumeMode = 'volume'; // 'volume' | 'oidelta' | 'off'
let currentATMStrike = 0;
let availableStrikes = []; // parsed from the latest options chain
let checkedStrikes = {}; // e.g. { "23700": { ce: true, pe: true } }
let strikeSeriesList = {}; // maps key to LineSeries objects
let cumPeSeries = null;
let cumCeSeries = null;
let lastOiTimeseriesData = [];
let lastMultiStrikeData = [];

function setChartMode(mode) {
  chartMode = mode;
  
  // Update button active state
  document.getElementById('modeIndexBtn').className = 'ts-select' + (mode === 'index' ? ' active' : '');
  document.getElementById('modeMultiStrikeBtn').className = 'ts-select' + (mode === 'multistrike' ? ' active' : '');
  document.getElementById('modeCumOiBtn').className = 'ts-select' + (mode === 'cumoi' ? ' active' : '');
  
  // Set button background styles
  document.getElementById('modeIndexBtn').style.background = mode === 'index' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('modeIndexBtn').style.borderColor = mode === 'index' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('modeIndexBtn').style.color = mode === 'index' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  document.getElementById('modeMultiStrikeBtn').style.background = mode === 'multistrike' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('modeMultiStrikeBtn').style.borderColor = mode === 'multistrike' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('modeMultiStrikeBtn').style.color = mode === 'multistrike' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  document.getElementById('modeCumOiBtn').style.background = mode === 'cumoi' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('modeCumOiBtn').style.borderColor = mode === 'cumoi' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('modeCumOiBtn').style.color = mode === 'cumoi' ? 'var(--text-bright)' : 'var(--text-dim)';

  // Toggle Strike Selector and Line Type visibility
  document.getElementById('strikeSelectorContainer').style.display = mode === 'multistrike' ? 'flex' : 'none';
  document.getElementById('multiStrikeSubToggle').style.display = mode === 'multistrike' ? 'flex' : 'none';
  
  // Update active series options
  updateSeriesVisibility();
  
  // Fetch new data right away
  updateChartData();
}

function setMultiStrikeLineMode(mode) {
  multiStrikeLineMode = mode;
  
  document.getElementById('msToggleAbs').className = 'ts-select' + (mode === 'absolute' ? ' active' : '');
  document.getElementById('msToggleChg').className = 'ts-select' + (mode === 'change' ? ' active' : '');
  
  document.getElementById('msToggleAbs').style.background = mode === 'absolute' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('msToggleAbs').style.borderColor = mode === 'absolute' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('msToggleAbs').style.color = mode === 'absolute' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  document.getElementById('msToggleChg').style.background = mode === 'change' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('msToggleChg').style.borderColor = mode === 'change' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('msToggleChg').style.color = mode === 'change' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  updateChartData();
}

function setOiBuildupMode(mode) {
  oiBuildupMode = mode;
  
  document.getElementById('oiToggleTotal').className = 'ts-select' + (mode === 'total' ? ' active' : '');
  document.getElementById('oiToggleChange').className = 'ts-select' + (mode === 'change' ? ' active' : '');
  
  document.getElementById('oiToggleTotal').style.background = mode === 'total' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('oiToggleTotal').style.borderColor = mode === 'total' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('oiToggleTotal').style.color = mode === 'total' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  document.getElementById('oiToggleChange').style.background = mode === 'change' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('oiToggleChange').style.borderColor = mode === 'change' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('oiToggleChange').style.color = mode === 'change' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  // Request fresh Nifty live data to update the Chart.js horizontal bar chart immediately
  updateNiftyData();
}

function setVolumeMode(mode) {
  volumeMode = mode;
  
  // Update button active state
  document.getElementById('volToggleShow').className = 'ts-select' + (mode === 'volume' ? ' active' : '');
  document.getElementById('volToggleOiDelta').className = 'ts-select' + (mode === 'oidelta' ? ' active' : '');
  document.getElementById('volToggleOff').className = 'ts-select' + (mode === 'off' ? ' active' : '');
  
  // Set button styles
  document.getElementById('volToggleShow').style.background = mode === 'volume' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('volToggleShow').style.borderColor = mode === 'volume' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('volToggleShow').style.color = mode === 'volume' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  document.getElementById('volToggleOiDelta').style.background = mode === 'oidelta' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('volToggleOiDelta').style.borderColor = mode === 'oidelta' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('volToggleOiDelta').style.color = mode === 'oidelta' ? 'var(--text-bright)' : 'var(--text-dim)';
  
  document.getElementById('volToggleOff').style.background = mode === 'off' ? 'var(--blue-soft)' : 'transparent';
  document.getElementById('volToggleOff').style.borderColor = mode === 'off' ? 'rgba(59,130,246,0.3)' : 'transparent';
  document.getElementById('volToggleOff').style.color = mode === 'off' ? 'var(--text-bright)' : 'var(--text-dim)';

  // Toggle visibility of the volume series
  if (volumeSeries) {
    volumeSeries.applyOptions({ visible: mode !== 'off' });
  }
  
  // Refresh chart data
  updateChartData();
}


function updateSeriesVisibility() {
  if (!oiLightweightChartInstance) return;
  
  const isIndex = chartMode === 'index';
  if (peChgSeries) peChgSeries.applyOptions({ visible: isIndex });
  if (ceChgSeries) ceChgSeries.applyOptions({ visible: isIndex });
  
  const isCum = chartMode === 'cumoi';
  if (cumPeSeries) cumPeSeries.applyOptions({ visible: isCum });
  if (cumCeSeries) cumCeSeries.applyOptions({ visible: isCum });
  
  const isMulti = chartMode === 'multistrike';
  Object.keys(strikeSeriesList).forEach(key => {
    const parts = key.split('_');
    const strike = parts[0];
    const type = parts[1];
    const isChecked = checkedStrikes[strike] && checkedStrikes[strike][type];
    strikeSeriesList[key].applyOptions({ visible: isMulti && isChecked });
  });
}

function getStrikeColor(strike, type) {
  const diff = parseFloat(strike) - parseFloat(currentATMStrike);
  const isCE = type === 'ce';
  
  if (diff === 0) {
    return isCE ? 'rgb(239, 68, 68)' : 'rgb(16, 185, 129)'; // ATM: Red, Green
  } else if (diff === 50) {
    return isCE ? 'rgb(244, 63, 94)' : 'rgb(20, 184, 166)'; // Rose, Teal
  } else if (diff === -50) {
    return isCE ? 'rgb(249, 115, 22)' : 'rgb(59, 130, 246)'; // Orange, Blue
  } else if (diff === 100) {
    return isCE ? 'rgb(217, 70, 239)' : 'rgb(168, 85, 247)'; // Fuchsia, Purple
  } else if (diff === -100) {
    return isCE ? 'rgb(234, 179, 8)' : 'rgb(132, 204, 22)'; // Yellow, Lime
  } else if (diff > 0) {
    return isCE ? 'rgba(239, 68, 68, 0.55)' : 'rgba(16, 185, 129, 0.55)';
  } else {
    return isCE ? 'rgba(249, 115, 22, 0.55)' : 'rgba(59, 130, 246, 0.55)';
  }
}

function getOrCreateStrikeSeries(strike, type) {
  const key = `${strike}_${type}`;
  if (strikeSeriesList[key]) {
    return strikeSeriesList[key];
  }
  
  const color = getStrikeColor(strike, type);
  const title = `${strike} ${type.toUpperCase()}`;
  
  const series = oiLightweightChartInstance.addSeries(LightweightCharts.LineSeries, {
    color: color,
    lineWidth: 2,
    title: title,
    priceFormat: {
      type: 'custom',
      formatter: (val) => formatCompactNumber(val)
    },
    priceScaleId: 'oi-change-scale',
  });
  
  strikeSeriesList[key] = series;
  return series;
}

function renderStrikeSelector(atmStrike) {
  if (!atmStrike) return;
  atmStrike = parseFloat(atmStrike);
  
  const container = document.getElementById('strikeSelectorContainer');
  if (container.children.length > 0 && currentATMStrike === atmStrike) return;
  
  currentATMStrike = atmStrike;
  
  // List nearest 5 strikes (ATM-100, ATM-50, ATM, ATM+50, ATM+100)
  const strikes = [atmStrike - 100, atmStrike - 50, atmStrike, atmStrike + 50, atmStrike + 100];
  availableStrikes = strikes;
  
  container.innerHTML = '';
  
  strikes.forEach(s => {
    // Initialize default selections (ATM CE/PE and ATM-50 PE, ATM+50 CE)
    if (checkedStrikes[s] === undefined) {
      checkedStrikes[s] = {
        ce: s === atmStrike || s === atmStrike + 50,
        pe: s === atmStrike || s === atmStrike - 50
      };
    }
    
    // Create element
    const strikeDiv = document.createElement('div');
    strikeDiv.style.display = 'flex';
    strikeDiv.style.alignItems = 'center';
    strikeDiv.style.gap = '4px';
    strikeDiv.style.background = 'rgba(255,255,255,0.03)';
    strikeDiv.style.padding = '3px 8px';
    strikeDiv.style.borderRadius = '4px';
    strikeDiv.style.border = '1px solid var(--border)';
    strikeDiv.style.fontSize = '10px';
    strikeDiv.style.whiteSpace = 'nowrap';
    
    const labelText = s === atmStrike ? `*${s} (ATM)` : `${s}`;
    
    strikeDiv.innerHTML = `
      <span style="font-weight: 600; color: ${s === atmStrike ? 'var(--text-bright)' : 'var(--text-dim)'}; margin-right: 4px;">${labelText}</span>
      <label style="display: flex; align-items: center; gap: 2px; cursor: pointer; color: rgb(239, 68, 68);">
        <input type="checkbox" id="chk_${s}_ce" ${checkedStrikes[s].ce ? 'checked' : ''} onchange="toggleStrikeCheckbox(${s}, 'ce')">
        CE
      </label>
      <label style="display: flex; align-items: center; gap: 2px; cursor: pointer; color: rgb(16, 185, 129); margin-left: 4px;">
        <input type="checkbox" id="chk_${s}_pe" ${checkedStrikes[s].pe ? 'checked' : ''} onchange="toggleStrikeCheckbox(${s}, 'pe')">
        PE
      </label>
    `;
    container.appendChild(strikeDiv);
  });
}

function toggleStrikeCheckbox(strike, type) {
  if (!checkedStrikes[strike]) {
    checkedStrikes[strike] = { ce: false, pe: false };
  }
  
  const chk = document.getElementById(`chk_${strike}_${type}`);
  checkedStrikes[strike][type] = chk ? chk.checked : false;
  
  updateSeriesVisibility();
  updateChartData();
}

function getCandleForTime(time) {
  if (!lastCandlesArray || lastCandlesArray.length === 0) return null;
  const timeVal = (typeof time === 'object' && time !== null) ? (time.timestamp || time) : time;
  return lastCandlesArray.find(c => {
    const cTimeVal = (typeof c.time === 'object' && c.time !== null) ? (c.time.timestamp || c.time) : c.time;
    return cTimeVal === timeVal;
  });
}

// 1. Initialize standalone local Lightweight Charts (Dual-Pane)
function initLocalChart() {
  if (typeof LightweightCharts === 'undefined') {
    console.warn('LightweightCharts not loaded');
    return;
  }
  try {
    const priceContainer = document.getElementById('priceChartContainer');
    const oiContainer = document.getElementById('oiChartContainer');
    if (!priceContainer || !oiContainer) return;
    
    // Clear any previous chart instances
    priceContainer.innerHTML = '';
    oiContainer.innerHTML = '';
    
    // Clean up observers if they exist
    if (priceChartResizeObserver) priceChartResizeObserver.disconnect();
    if (oiChartResizeObserver) oiChartResizeObserver.disconnect();
    
    // Common theme configuration - Obsidian Cybernetic Glass
    const themeColors = {
      bgColor: '#09090b', // Cybernetic black background
      textColor: '#a1a1aa',
      gridColor: 'rgba(255, 255, 255, 0.02)', // Subtle dark grid lines
      upColor: '#10b981', // Vibrant emerald green for bullish
      downColor: '#f43f5e' // Vibrant rose red for bearish
    };

    // 1.1 Create Price Chart (Top Pane)
    priceChartInstance = LightweightCharts.createChart(priceContainer, {
      layout: {
        background: { type: 'solid', color: themeColors.bgColor },
        textColor: themeColors.textColor,
        fontFamily: 'Outfit, Inter, sans-serif',
      },
      grid: {
        vertLines: { color: themeColors.gridColor, style: 2 },
        horzLines: { color: themeColors.gridColor, style: 2 },
      },
      crosshair: { 
        mode: 1,
        vertLine: { color: 'rgba(255, 255, 255, 0.15)', style: 3 },
        horzLine: { color: 'rgba(255, 255, 255, 0.15)', style: 3 }
      },
      timeScale: {
        visible: false, // hide top time scale for aligned vertical stack
        barSpacing: 18, // Increased from 10 to zoom in
        minBarSpacing: 3,
        shiftVisibleRangeOnNewBar: true,
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.05)',
        autoScale: true,
        scaleMargins: { top: 0.15, bottom: 0.15 },
      }
    });

    candleSeries = priceChartInstance.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: themeColors.upColor,
      downColor: themeColors.downColor,
      borderVisible: false,
      wickUpColor: themeColors.upColor,
      wickDownColor: themeColors.downColor,
    });

    volumeSeries = priceChartInstance.addSeries(LightweightCharts.HistogramSeries, {
      color: 'rgba(59, 130, 246, 0.18)', // Sleek blue
      priceFormat: { type: 'volume' },
      priceScaleId: 'left',
    });

    priceChartInstance.priceScale('left').applyOptions({
      scaleMargins: { top: 0.75, bottom: 0 },
      visible: false,
    });

    // 1.2 Create OI Chart (Bottom Pane)
    oiLightweightChartInstance = LightweightCharts.createChart(oiContainer, {
      layout: {
        background: { type: 'solid', color: themeColors.bgColor },
        textColor: themeColors.textColor,
        fontFamily: 'Outfit, Inter, sans-serif',
      },
      grid: {
        vertLines: { color: themeColors.gridColor, style: 2 },
        horzLines: { color: themeColors.gridColor, style: 2 },
      },
      crosshair: { 
        mode: 1,
        vertLine: { color: 'rgba(255, 255, 255, 0.15)', style: 3 },
        horzLine: { color: 'rgba(255, 255, 255, 0.15)', style: 3 }
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.05)',
        timeVisible: true,
        secondsVisible: false,
        barSpacing: 18, // Increased from 10 to zoom in
        minBarSpacing: 3,
        shiftVisibleRangeOnNewBar: true,
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.05)',
        autoScale: true,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      }
    });

    // 2.1 Pre-create Index mode histograms (green/red)
    peChgSeries = oiLightweightChartInstance.addSeries(LightweightCharts.HistogramSeries, {
      color: 'rgba(16, 185, 129, 0.65)',
      priceFormat: {
        type: 'custom',
        formatter: (val) => formatCompactNumber(val)
      },
      priceScaleId: 'oi-change-scale',
    });

    ceChgSeries = oiLightweightChartInstance.addSeries(LightweightCharts.HistogramSeries, {
      color: 'rgba(244, 63, 94, 0.65)', // Vibrant rose
      priceFormat: {
        type: 'custom',
        formatter: (val) => formatCompactNumber(val)
      },
      priceScaleId: 'oi-change-scale',
    });

    // 2.2 Pre-create Cumulative trend area series with transparent gradients
    cumPeSeries = oiLightweightChartInstance.addSeries(LightweightCharts.AreaSeries, {
      lineColor: '#10b981',
      topColor: 'rgba(16, 185, 129, 0.15)',
      bottomColor: 'rgba(16, 185, 129, 0.0)',
      lineWidth: 3,
      title: 'Cum PE Chg (Bullish)',
      priceFormat: {
        type: 'custom',
        formatter: (val) => formatCompactNumber(val)
      },
      priceScaleId: 'oi-change-scale',
    });

    cumCeSeries = oiLightweightChartInstance.addSeries(LightweightCharts.AreaSeries, {
      lineColor: '#f43f5e',
      topColor: 'rgba(244, 63, 94, 0.15)',
      bottomColor: 'rgba(244, 63, 94, 0.0)',
      lineWidth: 3,
      title: 'Cum CE Chg (Bearish)',
      priceFormat: {
        type: 'custom',
        formatter: (val) => formatCompactNumber(val)
      },
      priceScaleId: 'oi-change-scale',
    });

    oiLightweightChartInstance.priceScale('oi-change-scale').applyOptions({
      scaleMargins: { top: 0.15, bottom: 0.15 },
      visible: true,
    });

    // Initial visibility state
    updateSeriesVisibility();

    // 1.3 Setup Synchronization Range
    priceChartInstance.timeScale().subscribeVisibleTimeRangeChange(range => {
      if (isSyncingRange) return;
      isSyncingRange = true;
      oiLightweightChartInstance.timeScale().setVisibleRange(range);
      isSyncingRange = false;
    });
    oiLightweightChartInstance.timeScale().subscribeVisibleTimeRangeChange(range => {
      if (isSyncingRange) return;
      isSyncingRange = true;
      priceChartInstance.timeScale().setVisibleRange(range);
      isSyncingRange = false;
    });



    // 1.4 Setup Crosshair Synchronization & HUD updates
    priceChartInstance.subscribeCrosshairMove(param => {
      if (isSyncingCrosshair) return;
      isSyncingCrosshair = true;
      if (param.time) {
        oiLightweightChartInstance.setCrosshairPosition(0, param.time, peChgSeries);
        updateHudLegend(param.time);
      } else {
        oiLightweightChartInstance.clearCrosshairPosition();
        updateHudLegend(null);
      }
      isSyncingCrosshair = false;
    });

    oiLightweightChartInstance.subscribeCrosshairMove(param => {
      if (isSyncingCrosshair) return;
      isSyncingCrosshair = true;
      if (param.time) {
        const c = getCandleForTime(param.time);
        if (c) {
          priceChartInstance.setCrosshairPosition(c.close, param.time, candleSeries);
        } else {
          priceChartInstance.clearCrosshairPosition();
        }
        updateHudLegend(param.time);
      } else {
        priceChartInstance.clearCrosshairPosition();
        updateHudLegend(null);
      }
      isSyncingCrosshair = false;
    });

    // 1.5 Setup Resize Observers
    priceChartResizeObserver = new ResizeObserver(entries => {
      if (priceChartInstance && entries[0]) {
        const { width, height } = entries[0].contentRect;
        priceChartInstance.resize(width, height);
      }
    });
    priceChartResizeObserver.observe(priceContainer);

    oiChartResizeObserver = new ResizeObserver(entries => {
      if (oiLightweightChartInstance && entries[0]) {
        const { width, height } = entries[0].contentRect;
        oiLightweightChartInstance.resize(width, height);
      }
    });
    oiChartResizeObserver.observe(oiContainer);

  } catch (e) {
    console.error("Failed to initialize Lightweight Charts:", e);
    // Display detailed JavaScript error trace inside tvChartContainer for troubleshooting
    const container = document.getElementById('tvChartContainer');
    if (container) {
      container.innerHTML = `
        <div style="padding: 20px; color: var(--red-text); font-family: monospace; font-size: 11px; background: #09090b; height: 100%; overflow-y: auto;">
          <strong>JavaScript Initialization Failed:</strong> ${e.message}<br>
          <pre style="margin-top: 10px; opacity: 0.85;">${e.stack}</pre>
          <hr style="margin: 15px 0; border: none; border-top: 1px solid var(--border);">
          <strong>Fallback Chart Loading...</strong>
          <div style="margin-top: 10px; width: 100%; height: 220px; display: flex; align-items: center; justify-content: center; overflow: hidden;">
            <img id="fallbackChartImage" src="/api/nifty/chart.png?interval=5minute" style="width: 100%; height: 100%; object-fit: contain; cursor: pointer;" title="Server-side chart (click to refresh)" onclick="refreshFallbackChart()">
          </div>
        </div>
      `;
    }
  }
}

// Refresh fallback image chart
function refreshFallbackChart() {
  const fallbackImg = document.getElementById('fallbackChartImage');
  if (fallbackImg) {
    const intervalSelect = document.getElementById('intervalSelect');
    const interval = intervalSelect ? intervalSelect.value : '5minute';
    fallbackImg.src = `/api/nifty/chart.png?interval=${interval}&t=${Date.now()}`;
  }
}

// Draw dynamic support/resistance reference lines (ATM, Call Wall, Put Wall)
function drawReferenceLines(atmStrike, chainSlice) {
  if (!candleSeries) return;
  
  // Remove existing price lines
  if (atmPriceLine) {
    try { candleSeries.removePriceLine(atmPriceLine); } catch(e) {}
    atmPriceLine = null;
  }
  if (callWallPriceLine) {
    try { candleSeries.removePriceLine(callWallPriceLine); } catch(e) {}
    callWallPriceLine = null;
  }
  if (putWallPriceLine) {
    try { candleSeries.removePriceLine(putWallPriceLine); } catch(e) {}
    putWallPriceLine = null;
  }
  
  if (!atmStrike) return;
  atmStrike = parseFloat(atmStrike);
  
  // 1. Draw ATM Strike line (Purple)
  atmPriceLine = candleSeries.createPriceLine({
    price: atmStrike,
    color: 'rgba(168, 85, 247, 0.75)',
    lineWidth: 1.5,
    lineStyle: 2, // Dashed
    axisLabelVisible: true,
    title: 'ATM Strike',
  });
  
  if (!chainSlice || chainSlice.length === 0) return;
  
  // 2. Find Call Wall (Highest CE OI) and Put Wall (Highest PE OI)
  let maxCeStrike = null;
  let maxCeOi = -1;
  let maxPeStrike = null;
  let maxPeOi = -1;
  
  chainSlice.forEach(item => {
    if (item.ce_oi > maxCeOi) {
      maxCeOi = item.ce_oi;
      maxCeStrike = item.strike;
    }
    if (item.pe_oi > maxPeOi) {
      maxPeOi = item.pe_oi;
      maxPeStrike = item.strike;
    }
  });
  
  // Draw Call Wall Line (Red)
  if (maxCeStrike) {
    callWallPriceLine = candleSeries.createPriceLine({
      price: maxCeStrike,
      color: 'rgba(244, 63, 94, 0.75)',
      lineWidth: 1.5,
      lineStyle: 2, // Dashed
      axisLabelVisible: true,
      title: 'Call Wall (Res)',
    });
  }
  
  // Draw Put Wall Line (Green)
  if (maxPeStrike) {
    putWallPriceLine = candleSeries.createPriceLine({
      price: maxPeStrike,
      color: 'rgba(16, 185, 129, 0.75)',
      lineWidth: 1.5,
      lineStyle: 2, // Dashed
      axisLabelVisible: true,
      title: 'Put Wall (Supp)',
    });
  }
}

// Update the Floating HUD Legend overlay
function updateHudLegend(time) {
  if (!lastCandlesArray || lastCandlesArray.length === 0) return;
  
  let candle = null;
  if (time) {
    candle = getCandleForTime(time);
  }
  if (!candle) {
    // Default to the latest candle
    candle = lastCandlesArray[lastCandlesArray.length - 1];
  }
  
  // 1. Update OHLCV values
  const hudOhlc = document.getElementById('hudOhlc');
  if (hudOhlc) {
    const o = candle.open.toFixed(2);
    const h = candle.high.toFixed(2);
    const l = candle.low.toFixed(2);
    const c = candle.close.toFixed(2);
    const isUp = candle.close >= candle.open;
    const colorClass = isUp ? 'pos-val' : 'neg-val';
    
    let vSpan = '';
    if (volumeMode === 'oidelta') {
      const peChg = candle.pe_oi_chg || 0;
      const ceChg = candle.ce_oi_chg || 0;
      const delta = peChg - ceChg;
      const sign = delta >= 0 ? '+' : '';
      const deltaClass = delta >= 0 ? 'pos-val' : 'neg-val';
      vSpan = `OID: <span class="${deltaClass}">${sign}${formatCompactNumber(delta)}</span>`;
    } else if (volumeMode === 'volume') {
      const v = formatCompactNumber(candle.volume || 0);
      vSpan = `V: <span style="color: var(--text-bright);">${v}</span>`;
    }
    
    hudOhlc.innerHTML = `
      O: <span class="${colorClass}">${o}</span> 
      H: <span class="${colorClass}">${h}</span> 
      L: <span class="${colorClass}">${l}</span> 
      C: <span class="${colorClass}">${c}</span> 
      ${vSpan ? vSpan : ''}
    `;
  }
  
  // 2. Update Derivatives Details based on mode
  const hudOiDetails = document.getElementById('hudOiDetails');
  if (!hudOiDetails) return;
  
  if (chartMode === 'index') {
    const peChg = candle.pe_oi_chg || 0;
    const ceChg = candle.ce_oi_chg || 0;
    const netChg = peChg - ceChg;
    const sign = netChg >= 0 ? '+' : '';
    const netClass = netChg >= 0 ? 'pos-val' : 'neg-val';
    const netText = netChg >= 0 ? 'BULLISH' : 'BEARISH';
    
    hudOiDetails.innerHTML = `
      <span>PE Chg: <span class="pos-val">+${formatCompactNumber(peChg)}</span></span>
      <span>CE Chg: <span class="neg-val">${ceChg >= 0 ? '+' : ''}${formatCompactNumber(ceChg)}</span></span>
      <span>Net OI Chg: <span class="${netClass}">${sign}${formatCompactNumber(netChg)} (${netText})</span></span>
    `;
  } else if (chartMode === 'cumoi') {
    const peCum = candle.pe_cum || 0;
    const ceCum = candle.ce_cum || 0;
    const netCum = peCum - ceCum;
    const sign = netCum >= 0 ? '+' : '';
    const netClass = netCum >= 0 ? 'pos-val' : 'neg-val';
    const netText = netCum >= 0 ? 'BULLISH' : 'BEARISH';
    
    hudOiDetails.innerHTML = `
      <span>Cum PE Chg: <span class="pos-val">${peCum >= 0 ? '+' : ''}${formatCompactNumber(peCum)}</span></span>
      <span>Cum CE Chg: <span class="neg-val">${ceCum >= 0 ? '+' : ''}${formatCompactNumber(ceCum)}</span></span>
      <span>Cum Net Chg: <span class="${netClass}">${sign}${formatCompactNumber(netCum)} (${netText})</span></span>
    `;
  } else if (chartMode === 'multistrike') {
    const tVal = (typeof candle.time === 'object' && candle.time !== null) ? candle.time.timestamp : candle.time;
    
    let strikeEntry = null;
    if (lastMultiStrikeData && lastMultiStrikeData.length > 0) {
      strikeEntry = lastMultiStrikeData.find(item => item.time === tVal);
      if (!strikeEntry) {
        let minDiff = Infinity;
        for (let i = 0; i < lastMultiStrikeData.length; i++) {
          const item = lastMultiStrikeData[i];
          const diff = Math.abs(item.time - tVal);
          if (diff < minDiff) {
            minDiff = diff;
            strikeEntry = item;
          }
        }
        if (minDiff > 900) strikeEntry = null;
      }
    }
    
    let htmlContent = '';
    const sortedStrikes = [...availableStrikes].sort((a, b) => a - b);
    let hasAnyChecked = false;
    
    sortedStrikes.forEach(s => {
      const strikeStr = s.toFixed(1);
      const isCEChecked = checkedStrikes[s] && checkedStrikes[s].ce;
      const isPEChecked = checkedStrikes[s] && checkedStrikes[s].pe;
      
      if (!isCEChecked && !isPEChecked) return;
      hasAnyChecked = true;
      
      let ceVal = '—';
      let peVal = '—';
      let initialCe = 0;
      let initialPe = 0;
      
      if (lastMultiStrikeData.length > 0 && lastMultiStrikeData[0].strikes) {
        const initData = lastMultiStrikeData[0].strikes[strikeStr] || lastMultiStrikeData[0].strikes[s.toString()] || lastMultiStrikeData[0].strikes[parseFloat(s).toString()];
        if (initData) {
          initialCe = initData.ce_oi || 0;
          initialPe = initData.pe_oi || 0;
        }
      }
      
      if (strikeEntry && strikeEntry.strikes) {
        const strikeData = strikeEntry.strikes[strikeStr] || strikeEntry.strikes[s.toString()] || strikeEntry.strikes[parseFloat(s).toString()];
        if (strikeData) {
          const rawCe = strikeData.ce_oi || 0;
          const rawPe = strikeData.pe_oi || 0;
          
          ceVal = formatCompactNumber(multiStrikeLineMode === 'change' ? (rawCe - initialCe) : rawCe);
          peVal = formatCompactNumber(multiStrikeLineMode === 'change' ? (rawPe - initialPe) : rawPe);
        }
      }
      
      const isATM = s === currentATMStrike;
      const strikeLabel = isATM ? `*${s}` : `${s}`;
      
      if (isCEChecked) {
        const color = getStrikeColor(s, 'ce');
        htmlContent += `<span style="border-left: 2px solid ${color}; padding-left: 4px; margin-right: 8px;">${strikeLabel} CE: <span style="color: var(--text-bright);">${ceVal}</span></span>`;
      }
      if (isPEChecked) {
        const color = getStrikeColor(s, 'pe');
        htmlContent += `<span style="border-left: 2px solid ${color}; padding-left: 4px; margin-right: 8px;">${strikeLabel} PE: <span style="color: var(--text-bright);">${peVal}</span></span>`;
      }
    });
    
    if (!hasAnyChecked) {
      htmlContent = `<span style="color: var(--text-dim);">No strikes checked. Select CE/PE strikes above.</span>`;
    }
    
    hudOiDetails.innerHTML = htmlContent;
  }
}

// Fetch Nifty spot candles and OI time-series data, and draw them
async function updateChartData() {
  const fallbackImg = document.getElementById('fallbackChartImage');
  if (fallbackImg) {
    refreshFallbackChart();
    return;
  }

  try {
    const intervalSelect = document.getElementById('intervalSelect');
    const interval = intervalSelect ? intervalSelect.value : '5minute';

    // Parse interval to seconds for options aggregation
    let intervalSeconds = 300;
    if (interval === '1minute') intervalSeconds = 60;
    else if (interval === '3minute') intervalSeconds = 180;
    else if (interval === '5minute') intervalSeconds = 300;
    else if (interval === '15minute') intervalSeconds = 900;
    else if (interval === '30minute') intervalSeconds = 1800;

    // 1. Fetch Nifty Spot Candles
    const candlesRes = await fetch(`/api/candles?symbol=NIFTY&interval=${interval}`);
    if (!candlesRes.ok) throw new Error("Failed to fetch candle data");
    const candlesJson = await candlesRes.json();
    const candlesData = candlesJson.candles || [];

    // Calculate and update spot price daily range
    if (candlesData.length > 0) {
      let dailyMin = Infinity;
      let dailyMax = -Infinity;
      candlesData.forEach(c => {
        if (c.low < dailyMin) dailyMin = c.low;
        if (c.high > dailyMax) dailyMax = c.high;
      });
      
      const spotLowEl = document.getElementById('spotLowVal');
      const spotHighEl = document.getElementById('spotHighVal');
      const spotFillEl = document.getElementById('spotRangeFill');
      
      if (spotLowEl && spotHighEl && spotFillEl && dailyMin !== Infinity && dailyMax !== -Infinity) {
        spotLowEl.innerText = dailyMin.toFixed(1);
        spotHighEl.innerText = dailyMax.toFixed(1);
        
        const spotPriceEl = document.getElementById('spotPrice');
        if (spotPriceEl) {
          const currentSpot = parseFloat(spotPriceEl.innerText);
          if (!isNaN(currentSpot) && (dailyMax - dailyMin) > 0) {
            const pct = ((currentSpot - dailyMin) / (dailyMax - dailyMin)) * 100;
            spotFillEl.style.width = Math.min(100, Math.max(0, pct)) + '%';
          }
        }
      }
    }

    // 2. Fetch Nifty OI Timeseries
    const tsRes = await fetch('/api/nifty/timeseries');
    if (!tsRes.ok) throw new Error("Failed to fetch timeseries data");
    const tsData = await tsRes.json();

    // The timeseries data is newest first, reverse it to chronological order
    const sortedTs = [...tsData].reverse();
    lastOiTimeseriesData = sortedTs;

    // 3. Setup datasets based on view mode
    const mainCandles = [];
    const volumeData = [];
    const peChgData = [];
    const ceChgData = [];
    const cumPeData = [];
    const cumCeData = [];

    // Find initial values for cumulative changes
    const initialCeOi = sortedTs[0] ? sortedTs[0].total_ce_oi : 0;
    const initialPeOi = sortedTs[0] ? sortedTs[0].total_pe_oi : 0;

    // Build helper function to find closest snapshot in sortedTs
    function findClosestSnap(timestamp) {
      if (sortedTs.length === 0) return null;
      let closest = sortedTs[0];
      let minDiff = Math.abs(Math.floor(new Date(closest.snap_ts).getTime() / 1000) - timestamp);
      
      for (let i = 1; i < sortedTs.length; i++) {
        const snap = sortedTs[i];
        const snapT = Math.floor(new Date(snap.snap_ts).getTime() / 1000);
        const diff = Math.abs(snapT - timestamp);
        if (diff < minDiff) {
          minDiff = diff;
          closest = snap;
        }
      }
      return minDiff < 900 ? closest : null;
    }

    for (let i = 0; i < candlesData.length; i++) {
      const c = candlesData[i];
      const t = c.time;

      let peVal = 0;
      let ceVal = 0;
      let peCumVal = 0;
      let ceCumVal = 0;

      const snapEnd = findClosestSnap(t);
      const snapStart = findClosestSnap(t - intervalSeconds);

      if (snapEnd) {
        peCumVal = snapEnd.total_pe_oi - initialPeOi;
        ceCumVal = snapEnd.total_ce_oi - initialCeOi;
        
        if (snapStart) {
          peVal = snapEnd.total_pe_oi - snapStart.total_pe_oi;
          ceVal = snapEnd.total_ce_oi - snapStart.total_ce_oi;
        } else {
          const snapEndIndex = sortedTs.indexOf(snapEnd);
          if (snapEndIndex > 0) {
            const prevSnap = sortedTs[snapEndIndex - 1];
            peVal = snapEnd.total_pe_oi - prevSnap.total_pe_oi;
            ceVal = snapEnd.total_ce_oi - prevSnap.total_ce_oi;
          }
        }
      }

      mainCandles.push({
        time: t,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close
      });

      if (volumeMode === 'oidelta') {
        const delta = peVal - ceVal;
        volumeData.push({
          time: t,
          value: Math.abs(delta),
          color: delta >= 0 ? 'rgba(16, 185, 129, 0.65)' : 'rgba(239, 68, 68, 0.65)'
        });
      } else {
        volumeData.push({
          time: t,
          value: c.volume || 0,
          color: c.close >= c.open ? 'rgba(16, 185, 129, 0.25)' : 'rgba(239, 68, 68, 0.25)'
        });
      }

      peChgData.push({ time: t, value: peVal });
      ceChgData.push({ time: t, value: ceVal });
      cumPeData.push({ time: t, value: peCumVal });
      cumCeData.push({ time: t, value: ceCumVal });

      // Store aligned metrics on candle object for crosshair / HUD reference lookup
      c.pe_oi_chg = peVal;
      c.ce_oi_chg = ceVal;
      c.pe_cum = peCumVal;
      c.ce_cum = ceCumVal;
    }

    lastCandlesArray = candlesData;

    if (candleSeries && mainCandles.length > 0) {
      candleSeries.setData(mainCandles);
    }

    if (volumeSeries && volumeData.length > 0) {
      volumeSeries.setData(volumeData);
    }

    if (chartMode === 'index') {
      if (peChgSeries && peChgData.length > 0) {
        peChgSeries.setData(peChgData);
      }
      if (ceChgSeries && ceChgData.length > 0) {
        ceChgSeries.setData(ceChgData);
      }
    } else if (chartMode === 'cumoi') {
      if (cumPeSeries && cumPeData.length > 0) {
        cumPeSeries.setData(cumPeData);
      }
      if (cumCeSeries && cumCeData.length > 0) {
        cumCeSeries.setData(cumCeData);
      }
    } else if (chartMode === 'multistrike') {
      // 4. Fetch Multi-Strike OI Lines
      if (availableStrikes.length > 0) {
        const strikesRes = await fetch(`/api/nifty/multi-strike-oi?strikes=${availableStrikes.join(',')}`);
        if (strikesRes.ok) {
          const strikesData = await strikesRes.json();
          lastMultiStrikeData = strikesData;
          
          availableStrikes.forEach(s => {
            const strikeStr = s.toFixed(1);
            
            // Get initial values at open
            let initialCe = 0;
            let initialPe = 0;
            if (strikesData.length > 0 && strikesData[0].strikes) {
              const initData = strikesData[0].strikes[strikeStr] || strikesData[0].strikes[s.toString()] || strikesData[0].strikes[parseFloat(s).toString()];
              if (initData) {
                initialCe = initData.ce_oi || 0;
                initialPe = initData.pe_oi || 0;
              }
            }
            
            // CE Series
            if (checkedStrikes[s] && checkedStrikes[s].ce) {
              const ceSeries = getOrCreateStrikeSeries(s, 'ce');
              const ceLineData = [];
              strikesData.forEach(item => {
                const rawVal = (item.strikes[strikeStr] || item.strikes[s.toString()] || item.strikes[parseFloat(s).toString()])?.ce_oi || 0;
                const finalVal = multiStrikeLineMode === 'change' ? (rawVal - initialCe) : rawVal;
                ceLineData.push({ time: item.time, value: finalVal });
              });
              if (ceLineData.length > 0) ceSeries.setData(ceLineData);
            }
            
            // PE Series
            if (checkedStrikes[s] && checkedStrikes[s].pe) {
              const peSeries = getOrCreateStrikeSeries(s, 'pe');
              const peLineData = [];
              strikesData.forEach(item => {
                const rawVal = (item.strikes[strikeStr] || item.strikes[s.toString()] || item.strikes[parseFloat(s).toString()])?.pe_oi || 0;
                const finalVal = multiStrikeLineMode === 'change' ? (rawVal - initialPe) : rawVal;
                peLineData.push({ time: item.time, value: finalVal });
              });
              if (peLineData.length > 0) peSeries.setData(peLineData);
            }
          });
        }
      }
    }
    
    // Zoom in on initial load (last 60 candles)
    if (!initialChartZoomSet && mainCandles.length > 0) {
      initialChartZoomSet = true;
      const totalLen = mainCandles.length;
      const visibleCount = 60; // Show last 60 candles by default (too far out fix)
      priceChartInstance.timeScale().setVisibleLogicalRange({
        from: Math.max(0, totalLen - visibleCount),
        to: totalLen - 1
      });
    }

    
    // Refresh the HUD legend for the latest candle state
    updateHudLegend(null);
    
  } catch (error) {
    console.error("Error in updateChartData:", error);
  }
}

// standalone Moneyness buckets updater
function updateOiMoneynessBuckets(chainSlice, atmStrike) {
  if (!chainSlice || !atmStrike) return;
  const atm = parseFloat(atmStrike);
  
  // Initialize sums
  let atmCe = 0, atmPe = 0;
  let nearCe = 0, nearPe = 0;
  let deepCe = 0, deepPe = 0;
  
  chainSlice.forEach(item => {
    const strike = parseFloat(item.strike);
    const ceChg = item.ce_oi_chg_intraday || 0;
    const peChg = item.pe_oi_chg_intraday || 0;
    
    if (strike === atm) {
      atmCe += ceChg;
      atmPe += peChg;
    } else if (strike === atm + 50) {
      nearCe += ceChg;
    } else if (strike === atm - 50) {
      nearPe += peChg;
    } else if (strike >= atm + 100) {
      deepCe += ceChg;
    } else if (strike <= atm - 100) {
      deepPe += peChg;
    }
  });
  
  const atmStEl = document.getElementById('monAtmStrike');
  if (atmStEl) atmStEl.innerText = atm.toFixed(0);
  
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.innerText = formatCompactNumber(val);
  };
  
  setVal('monAtmCe', atmCe);
  setVal('monAtmPe', atmPe);
  setVal('monNearCe', nearCe);
  setVal('monNearPe', nearPe);
  setVal('monDeepCe', deepCe);
  setVal('monDeepPe', deepPe);
  
  // Normalize bars
  const maxVal = Math.max(
    1,
    Math.abs(atmCe), Math.abs(atmPe),
    Math.abs(nearCe), Math.abs(nearPe),
    Math.abs(deepCe), Math.abs(deepPe)
  );
  
  const setBar = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.style.width = (Math.abs(val) / maxVal * 100) + '%';
  };
  
  setBar('monAtmCeBar', atmCe);
  setBar('monAtmPeBar', atmPe);
  setBar('monNearCeBar', nearCe);
  setBar('monNearPeBar', nearPe);
  setBar('monDeepCeBar', deepCe);
  setBar('monDeepPeBar', deepPe);
  
  // Custom descriptor tags
  const setDesc = (id, ce, pe, type) => {
    const el = document.getElementById(id);
    if (!el) return;
    
    const absCe = Math.abs(ce);
    const absPe = Math.abs(pe);
    
    if (absCe === 0 && absPe === 0) {
      el.innerText = 'Balanced';
      el.style.color = 'var(--text-dim)';
      return;
    }
    
    if (type === 'deep') {
      if (absCe > absPe * 1.15) {
        el.innerText = 'Call Convex Bets';
        el.style.color = 'var(--red-text)';
      } else if (absPe > absCe * 1.15) {
        el.innerText = 'Put Convex Bets';
        el.style.color = 'var(--green-text)';
      } else {
        el.innerText = 'Balanced Convexity';
        el.style.color = 'var(--text-dim)';
      }
    } else {
      if (absCe > absPe * 1.15) {
        el.innerText = 'Call Writing (Income)';
        el.style.color = 'var(--red-text)';
      } else if (absPe > absCe * 1.15) {
        el.innerText = 'Put Writing (Income)';
        el.style.color = 'var(--green-text)';
      } else {
        el.innerText = 'Balanced Writing';
        el.style.color = 'var(--text-dim)';
      }
    }
  };
  
  setDesc('monAtmLabel', atmCe, atmPe, 'atm');
  setDesc('monNearLabel', nearCe, nearPe, 'near');
  setDesc('monDeepLabel', deepCe, deepPe, 'deep');
}

// 2. Fetch and Update Nifty Live Data
async function updateNiftyData() {
  try {
    const response = await fetch('/api/nifty/data');
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const data = await response.json();
    
    // Hide Global Loader on any successful or even partial fetch (if 200 returned)
    document.getElementById('loadingOverlay').classList.add('hide');
    
    // Update Stats Card values with strong null-guards
    const spotPrice = data.spot_price;
    const chgPct = data.chg_pct;
    
    // Spot Price Ticker
    const spotEl = document.getElementById('spotPrice');
    if (spotEl && spotPrice !== undefined && spotPrice !== null) {
      const prevLtp = parseFloat(spotEl.innerText);
      spotEl.innerText = spotPrice.toFixed(2);
      
      // Apply micro-flash animations on price tick change
      if (!isNaN(prevLtp)) {
        spotEl.classList.remove('flash-up', 'flash-down');
        void spotEl.offsetWidth; // Trigger reflow to restart animation
        if (spotPrice > prevLtp) {
          spotEl.classList.add('flash-up');
        } else if (spotPrice < prevLtp) {
          spotEl.classList.add('flash-down');
        }
      }
    }
    
    // Daily Change Percentage
    const chgEl = document.getElementById('spotChg');
    if (chgEl && spotPrice !== undefined && spotPrice !== null && data.prev_close) {
      const chgVal = (spotPrice - data.prev_close);
      const sign = chgVal >= 0 ? '+' : '';
      chgEl.innerText = `${sign}${chgVal.toFixed(2)} (${sign}${chgPct.toFixed(2)}%)`;
      chgEl.className = 'stat-sub ' + (chgVal >= 0 ? 'pos-val' : 'neg-val');
    }
    
    // Buildup gauge circular status update
    const gaugeFill = document.getElementById('buildupGaugeFill');
    const gaugeText = document.getElementById('buildupGaugeText');
    if (gaugeFill && gaugeText) {
      const buildupText = data.buildup || 'NEUTRAL';
      gaugeText.innerText = buildupText.replace('_', ' ');
      
      // stroke-dasharray is 157. full filled is 0, empty is 157
      if (buildupText === 'LONG_BUILD' || buildupText === 'SHORT_COVER') {
        gaugeFill.style.stroke = 'var(--green)';
        gaugeFill.style.strokeDashoffset = '0';
        gaugeFill.style.filter = 'drop-shadow(0 0 4px var(--green))';
        gaugeText.style.color = 'var(--green-text)';
      } else if (buildupText === 'SHORT_BUILD' || buildupText === 'LONG_UNWIND') {
        gaugeFill.style.stroke = 'var(--red)';
        gaugeFill.style.strokeDashoffset = '0';
        gaugeFill.style.filter = 'drop-shadow(0 0 4px var(--red))';
        gaugeText.style.color = 'var(--red-text)';
      } else {
        gaugeFill.style.stroke = 'var(--text-vdim)';
        gaugeFill.style.strokeDashoffset = '90'; // partially empty circle
        gaugeFill.style.filter = 'none';
        gaugeText.style.color = 'var(--text-dim)';
      }
    }
    
    // PCR values & spectrum slider pointer
    const pcrVal = data.pcr;
    document.getElementById('pcrValue').innerText = (pcrVal !== undefined && pcrVal !== null) ? pcrVal.toFixed(2) : '—';
    
    const pcrPointer = document.getElementById('pcrPointer');
    if (pcrPointer && pcrVal !== undefined && pcrVal !== null) {
      // Clamp PCR between 0.4 and 1.6
      const clampedPcr = Math.min(1.6, Math.max(0.4, pcrVal));
      const pct = ((clampedPcr - 0.4) / 1.2) * 100;
      pcrPointer.style.left = pct + '%';
    }

    const pcrSigBadge = document.getElementById('pcrSigBadge');
    if (pcrSigBadge) {
      const pcrSig = data.pcr_sig || 'NEUTRAL';
      pcrSigBadge.innerText = pcrSig.replace('_', ' ');
      pcrSigBadge.className = 'stat-sub ' + (pcrSig === 'BULLISH' || pcrSig === 'MILDLY_BULL' ? 'pos-val' : pcrSig === 'BEARISH' || pcrSig === 'MILDLY_BEAR' ? 'neg-val' : 'text-dim');
    }
    
    // Max Pain
    const maxPain = data.max_pain;
    document.getElementById('maxPainValue').innerText = (maxPain !== undefined && maxPain !== null) ? maxPain.toFixed(0) : '—';
    
    // Expiry & ATM
    document.getElementById('expiryValue').innerText = formatDateString(data.expiry);
    document.getElementById('atmStrikeValue').innerText = `ATM Strike: ${data.atm_strike !== undefined ? data.atm_strike : '—'}`;
    
    // Cache age
    const cacheAge = data.cache_age;
    document.getElementById('cacheBadge').innerText = `CACHE AGE: ${(cacheAge !== undefined && cacheAge !== null) ? cacheAge.toFixed(1) : '0.0'}s`;
    
    // Renders the horizontal option chain bar chart
    if (data.chain) {
      renderStrikeSelector(data.atm_strike);
      renderOIChart(data.chain, data.atm_strike);
      drawReferenceLines(data.atm_strike, data.chain);
      
      // Update Moneyness Buckets (convexity vs income bets)
      updateOiMoneynessBuckets(data.chain, data.atm_strike);
    }
    
  } catch (error) {
    console.error("Failed fetching live Nifty dashboard data: ", error);
    document.getElementById('loadingOverlay').classList.add('hide');
    
    document.getElementById('connText').innerText = "Reconnecting...";
    document.getElementById('connText').style.color = "var(--text-dim)";
  }
}

// Renders the vertical option interest bar chart (Sensibull Histogram)
function renderOIChart(chainSlice, atmStrike) {
  try {
    if (typeof Chart === 'undefined') {
      throw new Error("Chart.js library is not loaded");
    }
    if (!chainSlice || chainSlice.length === 0) return;
    
    // Arrange strikes left-to-right horizontally (ascending order)
    const sortedChain = [...chainSlice].sort((a, b) => a.strike - b.strike);
    
    const labels = sortedChain.map(item => {
      return item.strike === atmStrike ? `* ${item.strike} (ATM)` : `${item.strike}`;
    });
    
    const isChangeMode = (oiBuildupMode === 'change');
    const ceData = sortedChain.map(item => isChangeMode ? item.ce_oi_chg_intraday : item.ce_oi);
    const peData = sortedChain.map(item => isChangeMode ? item.pe_oi_chg_intraday : item.pe_oi);
    
    const canvas = document.getElementById('oiChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    // Generate beautiful linear gradients
    const callGradient = ctx.createLinearGradient(0, 0, 0, 250);
    callGradient.addColorStop(0, 'rgba(244, 63, 94, 0.65)');   // Rose
    callGradient.addColorStop(1, 'rgba(244, 63, 94, 0.04)');
    
    const putGradient = ctx.createLinearGradient(0, 0, 0, 250);
    putGradient.addColorStop(0, 'rgba(16, 185, 129, 0.65)');  // Emerald
    putGradient.addColorStop(1, 'rgba(16, 185, 129, 0.04)');
    
    if (barChartInstance) {
      barChartInstance.data.labels = labels;
      barChartInstance.data.datasets[0].label = isChangeMode ? 'Call (CE) OI Change' : 'Call (CE) Open Interest';
      barChartInstance.data.datasets[0].data = ceData;
      barChartInstance.data.datasets[0].backgroundColor = callGradient;
      
      barChartInstance.data.datasets[1].label = isChangeMode ? 'Put (PE) OI Change' : 'Put (PE) Open Interest';
      barChartInstance.data.datasets[1].data = peData;
      barChartInstance.data.datasets[1].backgroundColor = putGradient;
      
      barChartInstance.update();
    } else {
      barChartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [
            {
              label: isChangeMode ? 'Call (CE) OI Change' : 'Call (CE) Open Interest',
              data: ceData,
              backgroundColor: callGradient,
              hoverBackgroundColor: 'rgba(244, 63, 94, 0.85)',
              borderColor: 'rgba(244, 63, 94, 0.4)',
              borderWidth: 1,
              borderRadius: 4,
              barThickness: 8
            },
            {
              label: isChangeMode ? 'Put (PE) OI Change' : 'Put (PE) Open Interest',
              data: peData,
              backgroundColor: putGradient,
              hoverBackgroundColor: 'rgba(16, 185, 129, 0.85)',
              borderColor: 'rgba(16, 185, 129, 0.4)',
              borderWidth: 1,
              borderRadius: 4,
              barThickness: 8
            }
          ]
        },
        options: {
          indexAxis: 'x', // Vertical Sensibull-style bars
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'top',
              labels: {
                color: '#a1a1aa',
                boxWidth: 12,
                boxHeight: 12,
                font: {
                  family: 'Outfit',
                  size: 11,
                  weight: '600'
                }
              }
            },
            tooltip: {
              backgroundColor: 'rgba(8, 8, 12, 0.95)',
              titleFont: { family: 'Outfit', size: 12, weight: 'bold' },
              bodyFont: { family: 'Outfit', size: 12 },
              borderColor: 'rgba(59, 130, 246, 0.25)',
              borderWidth: 1,
              padding: 10,
              displayColors: true,
              callbacks: {
                label: function(context) {
                  let label = context.dataset.label || '';
                  if (label) {
                    label += ': ';
                  }
                  if (context.raw !== null) {
                    label += formatCompactNumber(context.raw);
                  }
                  return label;
                }
              }
            }
          },
          scales: {
            x: {
              grid: {
                display: false
              },
              ticks: {
                color: '#e4e4e7',
                font: {
                  family: 'Outfit',
                  size: 10,
                  weight: function(context) {
                    const label = context.chart.data.labels[context.index];
                    return label && label.includes('*') ? 'bold' : '500';
                  }
                }
              }
            },
            y: {
              grid: {
                color: 'rgba(255, 255, 255, 0.02)'
              },
              ticks: {
                color: '#71717a',
                font: { family: 'Outfit', size: 10 },
                callback: function(value) {
                  return formatCompactNumber(value);
                }
              }
            }
          }
        }
      });
    }
  } catch (e) {
    console.error("Failed to render OI Chart:", e);
    const wrapper = document.querySelector('.oi-chart-wrapper');
    if (wrapper) {
      wrapper.innerHTML = 
        `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-dim);font-size:12px;text-align:center;padding:16px;">
          OI Chart failed to load (offline or script blocked)
        </div>`;
    }
  }
}

// Formatting helpers
function formatDateString(str) {
  if (!str) return '—';
  try {
    const date = new Date(str);
    return date.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  } catch (e) {
    return str;
  }
}

function formatCompactNumber(number) {
  if (number === null || number === undefined) return '—';
  const absNum = Math.abs(number);
  let formatted = absNum;
  if (absNum >= 1e7) {
    formatted = (absNum / 1e7).toFixed(2) + 'Cr';
  } else if (absNum >= 1e5) {
    formatted = (absNum / 1e5).toFixed(2) + 'L';
  } else if (absNum >= 1e3) {
    formatted = (absNum / 1e3).toFixed(1) + 'K';
  } else {
    formatted = absNum.toFixed(0);
  }
  return number < 0 ? `-${formatted}` : formatted;
}

// Fetch and Update Nifty OI Timeseries (Fyers Grid)
async function updateNiftyTimeseries() {
  try {
    const response = await fetch('/api/nifty/timeseries');
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const data = await response.json();
    
    // Dynamically filter timeseries based on selected interval
    const intervalSelect = document.getElementById('intervalSelect');
    const intervalVal = intervalSelect ? intervalSelect.value : '5minute';
    let filterMinutes = 5;
    if (intervalVal === '1minute') filterMinutes = 1;
    else if (intervalVal === '3minute') filterMinutes = 3;
    else if (intervalVal === '5minute') filterMinutes = 5;
    else if (intervalVal === '15minute') filterMinutes = 15;
    else if (intervalVal === '30minute') filterMinutes = 30;

    const filteredData = [];
    for (let i = 0; i < data.length; i++) {
      const curr = data[i];
      const dt = new Date(curr.snap_ts);
      const minutes = dt.getMinutes();
      if (minutes % filterMinutes === 0) {
        filteredData.push(curr);
      }
    }

    document.getElementById('tsCountBadge').innerText = `ROWS: ${filteredData.length}`;
    
    const tbody = document.getElementById('tsTableBody');
    tbody.innerHTML = '';
    
    if (filteredData.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-dim); padding: 24px;">No timeseries data available today</td></tr>`;
      return;
    }

    // Scan for max PE-CE sellers difference to normalize bar widths
    let maxDiff = 1;
    filteredData.forEach(item => {
      const ceVal = item.total_ce_oi || 0;
      const peVal = item.total_pe_oi || 0;
      const diff = Math.abs(ceVal - peVal);
      if (diff > maxDiff) maxDiff = diff;
    });
    
    for (let i = 0; i < filteredData.length; i++) {
      const curr = filteredData[i];
      const prev = filteredData[i + 1]; // T-1 filtered row
      
      const timeStr = formatIsoTime(curr.snap_ts);
      
      // 1. LTP
      const ltpVal = curr.spot_ltp || 0;
      let ltpSub = '—';
      if (prev && prev.spot_ltp) {
        const diff = ltpVal - prev.spot_ltp;
        const pct = (diff / prev.spot_ltp) * 100;
        const sign = diff >= 0 ? '+' : '';
        const colorClass = diff >= 0 ? 'pos-val' : 'neg-val';
        ltpSub = `<span class="${colorClass}">${sign}${diff.toFixed(2)} (${sign}${pct.toFixed(2)}%)</span>`;
      }
      
      // 2. Total OI
      const totalOi = curr.total_oi || 0;
      let totalOiSub = '—';
      if (prev && prev.total_oi) {
        const diff = totalOi - prev.total_oi;
        const pct = (diff / prev.total_oi) * 100;
        const sign = diff >= 0 ? '+' : '';
        const colorClass = diff >= 0 ? 'pos-val' : 'neg-val';
        totalOiSub = `<span class="${colorClass}">${sign}${formatCompactNumber(diff)} (${sign}${pct.toFixed(2)}%)</span>`;
      }
      
      // 3. Total Call OI
      const ceOi = curr.total_ce_oi || 0;
      let ceOiSub = '—';
      if (prev && prev.total_ce_oi) {
        const diff = ceOi - prev.total_ce_oi;
        const pct = (diff / prev.total_ce_oi) * 100;
        const sign = diff >= 0 ? '+' : '';
        const colorClass = diff >= 0 ? 'pos-val' : 'neg-val';
        ceOiSub = `<span class="${colorClass}">${sign}${formatCompactNumber(diff)} (${sign}${pct.toFixed(2)}%)</span>`;
      }
      
      // 4. Total Put OI
      const peOi = curr.total_pe_oi || 0;
      let peOiSub = '—';
      if (prev && prev.total_pe_oi) {
        const diff = peOi - prev.total_pe_oi;
        const pct = (diff / prev.total_pe_oi) * 100;
        const sign = diff >= 0 ? '+' : '';
        const colorClass = diff >= 0 ? 'pos-val' : 'neg-val';
        peOiSub = `<span class="${colorClass}">${sign}${formatCompactNumber(diff)} (${sign}${pct.toFixed(2)}%)</span>`;
      }
      
      // 5. Order Flow Strength Delta calculation (Call OI - Put OI)
      // Positive diffOi means Call sellers (bearish) are stronger, negative means Put sellers (bullish) are stronger
      const diffOi = ceOi - peOi;
      const absDiff = Math.abs(diffOi);
      const barPct = (absDiff / maxDiff) * 50; // Max width is 50% for each side of midline
      
      let orderFlowHtml = '';
      const barStyle = `width: ${barPct.toFixed(1)}%;`;
      
      if (diffOi < 0) { // PE > CE -> Put sellers (Bullish) stronger
        orderFlowHtml = `
          <div style="display: flex; flex-direction: column; align-items: center; gap: 2px;">
            <div class="orderflow-bar-container">
              <div class="orderflow-midline"></div>
              <div class="orderflow-fill orderflow-fill-pe" style="${barStyle}"></div>
            </div>
            <div style="font-size: 9px; font-weight: 700; color: var(--green-text);">PE Stronger (+${formatCompactNumber(-diffOi)})</div>
          </div>
        `;
      } else if (diffOi > 0) { // CE > PE -> Call sellers (Bearish) stronger
        orderFlowHtml = `
          <div style="display: flex; flex-direction: column; align-items: center; gap: 2px;">
            <div class="orderflow-bar-container">
              <div class="orderflow-midline"></div>
              <div class="orderflow-fill orderflow-fill-ce" style="${barStyle}"></div>
            </div>
            <div style="font-size: 9px; font-weight: 700; color: var(--red-text);">CE Stronger (-${formatCompactNumber(diffOi)})</div>
          </div>
        `;
      } else { // Balanced
        orderFlowHtml = `
          <div style="display: flex; flex-direction: column; align-items: center; gap: 2px;">
            <div class="orderflow-bar-container">
              <div class="orderflow-midline"></div>
            </div>
            <div style="font-size: 9px; font-weight: 700; color: var(--text-dim);">BALANCED</div>
          </div>
        `;
      }
      
      // Highlight the active live row with a breathing status dot blinker
      const isLiveRow = (i === 0);
      const rowStatusDot = isLiveRow ? `<span class="live-row-blinker" title="Active Live Interval"></span>` : '';
      const rowSubLabel = isLiveRow ? 'LIVE' : 'Interval';
      const rowSubColor = isLiveRow ? 'color: var(--blue); font-weight: 700;' : 'color: var(--text-vdim);';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="text-align: left; padding-left: 12px;">
          <div class="ts-cell-primary" style="color: var(--text-bright); font-weight: 600; font-family: monospace;">${rowStatusDot}${timeStr}</div>
          <div class="ts-cell-sub" style="${rowSubColor}">${rowSubLabel}</div>
        </td>
        <td>
          <div class="ts-cell-primary" style="font-family: monospace;">${ltpVal.toFixed(2)}</div>
          <div class="ts-cell-sub">${ltpSub}</div>
        </td>
        <td>
          <div class="ts-cell-primary" style="font-family: monospace;">${formatCompactNumber(totalOi)}</div>
          <div class="ts-cell-sub">${totalOiSub}</div>
        </td>
        <td>
          <div class="ts-cell-primary" style="font-family: monospace;">${formatCompactNumber(ceOi)}</div>
          <div class="ts-cell-sub">${ceOiSub}</div>
        </td>
        <td>
          <div class="ts-cell-primary" style="font-family: monospace;">${formatCompactNumber(peOi)}</div>
          <div class="ts-cell-sub">${peOiSub}</div>
        </td>
        <td style="text-align: center; padding-right: 12px; width: 130px;">
          ${orderFlowHtml}
        </td>
      `;
      tbody.appendChild(tr);
    }
  } catch (error) {
    console.error("Failed fetching Nifty timeseries data: ", error);
  }
}

function formatIsoTime(isoStr) {
  if (!isoStr) return '—';
  try {
    const parts = isoStr.split('T');
    if (parts.length === 2) {
      let t = parts[1];
      // Strip timezone offset (e.g. +05:30 or Z or -08:00)
      t = t.split('+')[0].split('-')[0].split('Z')[0];
      return t;
    }
    const date = new Date(isoStr);
    return date.toTimeString().split(' ')[0];
  } catch (e) {
    return isoStr;
  }
}

// 4. Initialize and set intervals
window.addEventListener('DOMContentLoaded', () => {
  initLocalChart();
  updateNiftyData();
  updateNiftyTimeseries();
  updateChartData();
  
  // Real-time stats updates (2 seconds)
  setInterval(updateNiftyData, 2000);
  
  // Historical timeseries updates (10 seconds)
  setInterval(updateNiftyTimeseries, 10000);

  // Chart data updates (5 seconds)
  setInterval(updateChartData, 5000);

  // Trigger immediate updates on interval change
  const intervalSelect = document.getElementById('intervalSelect');
  if (intervalSelect) {
    intervalSelect.addEventListener('change', () => {
      updateChartData();
      updateNiftyTimeseries();
    });
  }
});
</script>
