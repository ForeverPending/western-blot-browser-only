const BACKEND_URL = (CONFIG.BACKEND_URL || "").replace(/\/$/, "");
const SESSION_STORAGE_KEY = "western-blot:browser-session-id";
const ALLOWED_TABULAR_EXTENSIONS = new Set(["csv", "tsv", "xls", "xlsx"]);
const ALLOWED_ZIP_MIME_TYPES = new Set(["", "application/zip", "application/x-zip-compressed", "application/octet-stream"]);
const DEFAULT_API_TIMEOUT_MS = 60000;
const SETUP_API_TIMEOUT_MS = 15000;
const BLOB_UPLOAD_TIMEOUT_MS = 240000;
const ZIP_PROCESSING_TIMEOUT_MS = 270000;
const IMAGE_RENDER_TIMEOUT_MS = 120000;
const SIGNAL_EXTRACTION_TIMEOUT_MS = 120000;
const PPTX_EXPORT_TIMEOUT_MS = 180000;
const DEFAULT_ZIP_UPLOAD_BYTES = 250 * 1024 * 1024;
const DEFAULT_TABULAR_UPLOAD_BYTES = 25 * 1024 * 1024;
const JPEG_MIME_TYPE = "image/jpeg";
const CHART_JPEG_QUALITY = 0.95;
const PPTX_JPEG_QUALITY = 0.92;
const PPTX_IMAGE_SLIDE_TYPE = "images";
const PPTX_GRAPH_SLIDE_TYPE = "graphs";
const MAX_CANVAS_BOXES = 200;
const MAX_WORKBOOK_EXPORT_ROWS = 50000;
const SIGNAL_NUMBER_PATTERN = /^[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
let runtimeConfigPromise = null;

function browserSessionId() {
  let sessionId = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!sessionId) {
    sessionId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  }
  return sessionId;
}

const activeSessionId = browserSessionId();

function apiUrl(path) {
  return `${BACKEND_URL}${path}`;
}

function themeToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function themeFont(weight, size, familyToken = "--font-sans") {
  const fallback = familyToken === "--font-mono" ? "monospace" : "system-ui, sans-serif";
  return `${weight} ${size}px ${themeToken(familyToken) || fallback}`;
}

function chartSeriesColors() {
  return Array.from({ length: 6 }, (_, index) =>
    themeToken(`--chart-series-${index + 1}`)
  );
}
const state = {
  mode: "shared",
  sharedSamples: [],
  sharedControl: createDatasetState(),
  sharedAnalyses: [],
  activeSampleIndex: 0,
  pairedSets: [],
  pairedAnalyses: [],
  comparisonCustomGroups: [],
};

const blotState = {
  blots: [],
  activeBlotIndex: null,
  scans: {},  // key: blotId, value: array of {proteinName, channel, backgroundAxis, lanes: [{name, signal}]}
  scanById: {},
};

function delay(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

async function fetchWithTimeout(url, fetchOptions, timeoutMs) {
  const headers = new Headers(fetchOptions.headers || {});
  headers.set("X-Blot-Session", activeSessionId);
  let timeoutId = null;
  let abortController = null;
  let removeAbortListener = null;
  const requestOptions = { ...fetchOptions, headers };

  if (timeoutMs > 0) {
    abortController = new AbortController();
    if (fetchOptions.signal) {
      if (fetchOptions.signal.aborted) abortController.abort();
      const abortFromSource = () => abortController.abort();
      fetchOptions.signal.addEventListener("abort", abortFromSource, { once: true });
      removeAbortListener = () => fetchOptions.signal.removeEventListener("abort", abortFromSource);
    }
    timeoutId = window.setTimeout(() => abortController.abort(), timeoutMs);
    requestOptions.signal = abortController.signal;
  }

  try {
    return await fetch(url, {
      ...requestOptions,
    });
  } catch (error) {
    if (abortController?.signal.aborted && timeoutMs > 0) {
      throw new Error(`Request timed out after ${Math.ceil(timeoutMs / 1000)} seconds.`);
    }
    throw error;
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
    if (removeAbortListener) removeAbortListener();
  }
}

async function apiFetch(url, options = {}) {
  const {
    timeoutMs = DEFAULT_API_TIMEOUT_MS,
    retry = 0,
    retryDelayMs = 500,
    ...fetchOptions
  } = options;

  for (let attempt = 0; ; attempt += 1) {
    try {
      return await fetchWithTimeout(url, fetchOptions, timeoutMs);
    } catch (error) {
      if (attempt >= retry || fetchOptions.signal?.aborted) throw error;
      await delay(retryDelayMs * (attempt + 1));
    }
  }
}

async function apiErrorMessage(response, fallback) {
  try {
    const data = await response.json();
    return typeof data?.error === "string" && data.error.trim()
      ? data.error.trim()
      : fallback;
  } catch (_error) {
    return fallback;
  }
}

// Fetches JSON and converts every unsuccessful response into one consistent error.
async function apiJson(url, options = {}, fallback = "Request failed.") {
  let response;
  try {
    response = await apiFetch(url, options);
  } catch (error) {
    throw new Error(error?.message || fallback);
  }
  let data;
  try {
    data = await response.json();
  } catch (_error) {
    throw new Error(fallback);
  }
  if (!response.ok || data?.error) throw new Error(data?.error || fallback);
  return data;
}

async function runtimeConfig() {
  if (!runtimeConfigPromise) {
    runtimeConfigPromise = CONFIG.USE_VERCEL_BLOB_UPLOADS
      ? apiJson(apiUrl("/client-config"), { timeoutMs: SETUP_API_TIMEOUT_MS, retry: 1 }, "Could not load deployment config.")
      : Promise.resolve({});
    runtimeConfigPromise = runtimeConfigPromise.catch(error => {
      runtimeConfigPromise = null;
      throw error;
    });
  }
  return runtimeConfigPromise;
}

const els = {
  analysisMode: document.querySelector("#analysisMode"),
  sharedCountWrap: document.querySelector("#sharedCountWrap"),
  pairCountWrap: document.querySelector("#pairCountWrap"),
  comparisonChartWrap: document.querySelector("#comparisonChartWrap"),
  sampleCount: document.querySelector("#sampleCount"),
  pairCount: document.querySelector("#pairCount"),
  comparisonChartType: document.querySelector("#comparisonChartType"),
  sampleInputs: document.querySelector("#sampleInputs"),
  pairInputs: document.querySelector("#pairInputs"),
  sharedWorkflow: document.querySelector("#sharedWorkflow"),
  comparisonWorkflow: document.querySelector("#comparisonWorkflow"),
  sharedResults: document.querySelector("#sharedResults"),
  comparisonResults: document.querySelector("#comparisonResults"),
  controlFile: document.querySelector("#controlFile"),
  controlFileName: document.querySelector("#controlFileName"),
  controlProteinRow: document.querySelector("#controlProteinRow"),
  controlProtein: document.querySelector("#controlProtein"),
  controlMappingGrid: document.querySelector("#controlMappingGrid"),
  controlSheet: document.querySelector("#controlSheet"),
  controlLaneColumn: document.querySelector("#controlLaneColumn"),
  controlSignalColumn: document.querySelector("#controlSignalColumn"),
  normalizationLaneWrap: document.querySelector("#normalizationLaneWrap"),
  normalizationLane: document.querySelector("#normalizationLane"),
  analysisReadiness: document.querySelector("#analysisReadiness"),
  groupControlsPanel: document.querySelector("#groupControlsPanel"),
  groupModeWrap: document.querySelector("#groupModeWrap"),
  groupSizeWrap: document.querySelector("#groupSizeWrap"),
  groupMode: document.querySelector("#groupMode"),
  groupSize: document.querySelector("#groupSize"),
  groupToggleWrap: document.querySelector("#groupToggleWrap"),
  enableGroupedGraphs: document.querySelector("#enableGroupedGraphs"),
  analyzeButton: document.querySelector("#analyzeButton"),
  sampleTabsPanel: document.querySelector("#sampleTabsPanel"),
  sampleTabs: document.querySelector("#sampleTabs"),
  chartTitle: document.querySelector("#chartTitle"),
  downloadChartButton: document.querySelector("#downloadChartButton"),
  downloadCsvButton: document.querySelector("#downloadCsvButton"),
  downloadWorkbookButton: document.querySelector("#downloadWorkbookButton"),
  foldChart: document.querySelector("#foldChart"),
  resultTableBody: document.querySelector("#resultTableBody"),
  labelPanel: document.querySelector("#labelPanel"),
  labelEditor: document.querySelector("#labelEditor"),
  groupPanel: document.querySelector("#groupPanel"),
  customGroupingPanel: document.querySelector("#customGroupingPanel"),
  customGroups: document.querySelector("#customGroups"),
  addCustomGroupButton: document.querySelector("#addCustomGroupButton"),
  groupSummary: document.querySelector("#groupSummary"),
  blockGraphs: document.querySelector("#blockGraphs"),
  comparisonLabelPanel: document.querySelector("#comparisonLabelPanel"),
  comparisonLabelEditor: document.querySelector("#comparisonLabelEditor"),
  comparisonChartPanel: document.querySelector("#comparisonChartPanel"),
  comparisonChartTitle: document.querySelector("#comparisonChartTitle"),
  comparisonChart: document.querySelector("#comparisonChart"),
  downloadComparisonChartButton: document.querySelector("#downloadComparisonChartButton"),
  downloadComparisonWorkbookButton: document.querySelector("#downloadComparisonWorkbookButton"),
  comparisonGroupPanel: document.querySelector("#comparisonGroupPanel"),
  comparisonCustomGroupingPanel: document.querySelector("#comparisonCustomGroupingPanel"),
  comparisonCustomGroups: document.querySelector("#comparisonCustomGroups"),
  addComparisonCustomGroupButton: document.querySelector("#addComparisonCustomGroupButton"),
  comparisonGroupSummary: document.querySelector("#comparisonGroupSummary"),
  comparisonGroupedCharts: document.querySelector("#comparisonGroupedCharts"),
};

let sharedSampleControls = [];
let pairControls = [];

els.analysisMode.addEventListener("change", switchMode);
els.sampleCount.addEventListener("input", () => {
  invalidateAnalyses();
  renderSharedSampleInputs(clampInteger(Number(els.sampleCount.value), 1, 12, 1));
});
els.pairCount.addEventListener("input", () => {
  invalidateAnalyses();
  renderPairInputs(clampInteger(Number(els.pairCount.value), 2, 12, 2));
});
els.comparisonChartType.addEventListener("change", () => {
  renderComparisonChart();
  renderWorkflowState();
});
els.controlFile.addEventListener("change", (event) => loadSharedControlFile(event));
els.controlSheet.addEventListener("change", () => {
  if (selectDatasetSheet(state.sharedControl, sharedControlControls(), els.controlSheet.value)) {
    invalidateAnalyses();
    refreshNormalizationLanes();
  }
});
els.controlLaneColumn.addEventListener("change", () => {
  invalidateAnalyses();
  refreshNormalizationLanes();
});
els.controlSignalColumn.addEventListener("change", () => {
  invalidateAnalyses();
  refreshNormalizationLanes();
});
els.analyzeButton.addEventListener("click", runAnalysis);
els.normalizationLane.addEventListener("change", () => {
  invalidateAnalyses();
  renderWorkflowState();
});
els.downloadChartButton.addEventListener("click", () => downloadCanvasJpeg(els.foldChart, currentSharedChartFilename()));
els.downloadCsvButton.addEventListener("click", downloadSharedCsv);
els.downloadWorkbookButton.addEventListener("click", downloadSharedWorkbook);
els.downloadComparisonChartButton.addEventListener("click", () => downloadCanvasJpeg(els.comparisonChart, "common-lane-comparison.jpg"));
els.downloadComparisonWorkbookButton.addEventListener("click", downloadComparisonWorkbook);
els.enableGroupedGraphs.addEventListener("change", () => {
  renderCurrentGrouping();
  renderWorkflowState();
});
els.groupMode.addEventListener("change", () => {
  renderCurrentGrouping();
  renderWorkflowState();
});
els.groupSize.addEventListener("input", renderCurrentGrouping);
els.labelEditor.addEventListener("input", updateSharedLaneLabel);
els.sampleTabs.addEventListener("click", selectSampleTab);
els.customGroups.addEventListener("input", updateCustomGroups);
els.customGroups.addEventListener("change", updateCustomGroups);
els.addCustomGroupButton.addEventListener("click", addCustomGroup);
els.comparisonLabelEditor.addEventListener("input", updateComparisonLaneLabel);
els.comparisonCustomGroups.addEventListener("input", updateComparisonCustomGroups);
els.comparisonCustomGroups.addEventListener("change", updateComparisonCustomGroups);
els.addComparisonCustomGroupButton.addEventListener("click", addComparisonCustomGroup);
els.blockGraphs.addEventListener("click", downloadGeneratedChart);
els.comparisonGroupedCharts.addEventListener("click", downloadGeneratedChart);
document.addEventListener("click", handleDocumentClick);
document.addEventListener("change", handleDocumentChange);
document.querySelector("#generatePptxButton")?.addEventListener("click", generatePptx);

renderSharedSampleInputs(1);
renderPairInputs(2);
drawEmptyChart(els.foldChart);
renderWorkflowState();

function createDatasetState() {
  return {
    workbook: null,
    sheetName: "",
    rows: [],
    headers: [],
  };
}

function createAnalysisState(name, title, results, metadata = {}) {
  return {
    name,
    title,
    results,
    normalizationLane: metadata.normalizationLane || "",
    loadingControlAdjusted: metadata.loadingControlAdjusted !== false,
    sources: Array.isArray(metadata.sources) ? metadata.sources : [],
    customGroups: createDefaultCustomGroups(results, analysisRowKey),
  };
}

function sharedControlControls() {
  return {
    fileName: els.controlFileName,
    sheet: els.controlSheet,
    lane: els.controlLaneColumn,
    signal: els.controlSignalColumn,
  };
}

function switchMode() {
  state.mode = els.analysisMode.value;
  const isComparison = state.mode === "comparison";
  els.sharedWorkflow.hidden = isComparison;
  els.sharedCountWrap.hidden = isComparison;
  els.comparisonWorkflow.hidden = !isComparison;
  els.pairCountWrap.hidden = !isComparison;
  els.comparisonChartWrap.hidden = !isComparison;
  refreshNormalizationLanes();
  renderWorkflowState();
  if (isComparison) renderComparisonChart();
}

function renderSharedSampleInputs(count) {
  while (state.sharedSamples.length < count) state.sharedSamples.push(createDatasetState());
  state.sharedSamples = state.sharedSamples.slice(0, count);

  els.sampleInputs.innerHTML = state.sharedSamples
    .map((_, index) => sampleCardHtml(index))
    .join("");

  sharedSampleControls = state.sharedSamples.map((_, index) => ({
    file: document.querySelector(`[data-shared-sample-file="${index}"]`),
    fileName: document.querySelector(`[data-shared-sample-file-name="${index}"]`),
    protein: document.querySelector(`[data-shared-sample-protein="${index}"]`),
    sheet: document.querySelector(`[data-shared-sample-sheet="${index}"]`),
    lane: document.querySelector(`[data-shared-sample-lane="${index}"]`),
    signal: document.querySelector(`[data-shared-sample-signal="${index}"]`),
  }));

  sharedSampleControls.forEach((controls, index) => {
    controls.file.addEventListener("change", (event) => loadSharedSampleFile(event, index));
    controls.sheet.addEventListener("change", () => {
      if (selectDatasetSheet(state.sharedSamples[index], controls, controls.sheet.value)) {
        invalidateAnalyses();
        refreshNormalizationLanes();
      }
    });
    controls.lane.addEventListener("change", () => {
      invalidateAnalyses();
      refreshNormalizationLanes();
    });
    controls.signal.addEventListener("change", () => {
      invalidateAnalyses();
      refreshNormalizationLanes();
    });
    hydrateDatasetControls(state.sharedSamples[index], controls);
  });

  refreshNormalizationLanes();
}

function sampleCardHtml(index) {
  return `
    <div class="sample-card">
      <h3>Sample ${index + 1}</h3>

      <div class="source-toggle">
        <button class="source-button active" type="button"
          data-source-target="sampleFile${index}"
          data-source-group="sample-${index}"
          data-source-role="sample" data-source-index="${index}" data-source-mode="file">
          Upload file
        </button>
        <button class="source-button" type="button"
          data-source-target="sampleBlot${index}"
          data-source-group="sample-${index}"
          data-source-role="sample" data-source-index="${index}" data-source-mode="blot">
          Use blot
        </button>
      </div>

      <div id="sampleFile${index}" class="source-panel">
        <label class="file-drop">
          <input data-shared-sample-file="${index}" type="file" accept=".xlsx,.xls,.csv,.tsv" />
          <span data-shared-sample-file-name="${index}">${escapeHtml(state.sharedSamples[index]?.fileLabel || "Choose Excel or CSV file")}</span>
        </label>
        <div class="field-row" data-dataset-details="sample-${index}"${datasetDetailsHiddenAttr(state.sharedSamples[index])}>
          <label>
            Protein name
            <input data-shared-sample-protein="${index}" type="text" placeholder="e.g. pERK" value="${escapeHtml(state.sharedSamples[index]?.proteinName || "")}" />
          </label>
        </div>
        <div class="mapping-grid" data-dataset-details="sample-${index}"${datasetDetailsHiddenAttr(state.sharedSamples[index])}>
          <label>
            Sheet
            <select data-shared-sample-sheet="${index}"></select>
          </label>
          <label>
            Lane column
            <select data-shared-sample-lane="${index}"></select>
          </label>
          <label>
            Signal column
            <select data-shared-sample-signal="${index}"></select>
          </label>
        </div>
      </div>

      <div id="sampleBlot${index}" class="source-panel" hidden>
        ${blotSourceControlsHtml(`sample-${index}`, "sample", index)}
      </div>
    </div>
  `;
}

function renderPairInputs(count) {
  while (state.pairedSets.length < count) {
    state.pairedSets.push({ sample: createDatasetState(), control: createDatasetState() });
  }
  state.pairedSets = state.pairedSets.slice(0, count);

  els.pairInputs.innerHTML = state.pairedSets
    .map((_, index) => pairCardHtml(index))
    .join("");

  pairControls = state.pairedSets.map((_, index) => ({
    sample: {
      file: document.querySelector(`[data-pair-sample-file="${index}"]`),
      fileName: document.querySelector(`[data-pair-sample-file-name="${index}"]`),
      label: document.querySelector(`[data-pair-sample-label="${index}"]`),
      sheet: document.querySelector(`[data-pair-sample-sheet="${index}"]`),
      lane: document.querySelector(`[data-pair-sample-lane="${index}"]`),
      signal: document.querySelector(`[data-pair-sample-signal="${index}"]`),
    },
    control: {
      file: document.querySelector(`[data-pair-control-file="${index}"]`),
      fileName: document.querySelector(`[data-pair-control-file-name="${index}"]`),
      sheet: document.querySelector(`[data-pair-control-sheet="${index}"]`),
      lane: document.querySelector(`[data-pair-control-lane="${index}"]`),
      signal: document.querySelector(`[data-pair-control-signal="${index}"]`),
    },
  }));

  pairControls.forEach((controls, index) => {
    controls.sample.file.addEventListener("change", (event) => loadPairFile(event, index, "sample"));
    controls.control.file.addEventListener("change", (event) => loadPairFile(event, index, "control"));
    controls.sample.sheet.addEventListener("change", () => {
      if (selectDatasetSheet(state.pairedSets[index].sample, controls.sample, controls.sample.sheet.value)) {
        invalidateAnalyses();
        refreshNormalizationLanes();
      }
    });
    controls.control.sheet.addEventListener("change", () => {
      if (selectDatasetSheet(state.pairedSets[index].control, controls.control, controls.control.sheet.value)) {
        invalidateAnalyses();
        refreshNormalizationLanes();
      }
    });
    controls.sample.lane.addEventListener("change", () => {
      invalidateAnalyses();
      refreshNormalizationLanes();
    });
    controls.sample.signal.addEventListener("change", () => {
      invalidateAnalyses();
      refreshNormalizationLanes();
    });
    controls.control.lane.addEventListener("change", () => {
      invalidateAnalyses();
      refreshNormalizationLanes();
    });
    controls.control.signal.addEventListener("change", () => {
      invalidateAnalyses();
      refreshNormalizationLanes();
    });
    hydrateDatasetControls(state.pairedSets[index].sample, controls.sample);
    hydrateDatasetControls(state.pairedSets[index].control, controls.control);
  });

  refreshNormalizationLanes();
}

function pairCardHtml(index) {
  return `
    <div class="pair-card">
      <h3>Pair ${index + 1}</h3>
      <div class="field-row">
        <label>
          Graph label
          <input data-pair-sample-label="${index}" type="text" value="${escapeHtml(state.pairedSets[index]?.label || `Sample ${index + 1}`)}" />
        </label>
      </div>
      <div class="pair-columns">
        <div>
          <span class="badge sample">Sample</span>

          <div class="source-toggle">
            <button class="source-button active" type="button"
              data-source-group="pair-sample-${index}"
              data-source-role="pair-sample" data-source-index="${index}" data-source-mode="file">
              Upload file
            </button>
            <button class="source-button" type="button"
              data-source-group="pair-sample-${index}"
              data-source-role="pair-sample" data-source-index="${index}" data-source-mode="blot">
              Use blot
            </button>
          </div>

          <div id="pairSampleFile${index}" class="source-panel">
            <label class="file-drop">
              <input data-pair-sample-file="${index}" type="file" accept=".xlsx,.xls,.csv,.tsv" />
              <span data-pair-sample-file-name="${index}">${escapeHtml(state.pairedSets[index]?.sample?.fileLabel || "Choose sample file")}</span>
            </label>
            <div class="mapping-grid" data-dataset-details="pair-sample-${index}"${datasetDetailsHiddenAttr(state.pairedSets[index]?.sample)}>
              <label>Sheet<select data-pair-sample-sheet="${index}"></select></label>
              <label>Lane column<select data-pair-sample-lane="${index}"></select></label>
              <label>Signal column<select data-pair-sample-signal="${index}"></select></label>
            </div>
          </div>

          <div id="pairSampleBlot${index}" class="source-panel" hidden>
            ${blotSourceControlsHtml(`pair-sample-${index}`, "pair-sample", index)}
          </div>
        </div>
        <div>
          <span class="badge control">Control</span>

          <div class="source-toggle">
            <button class="source-button active" type="button"
              data-source-group="pair-control-${index}"
              data-source-role="pair-control" data-source-index="${index}" data-source-mode="file">
              Upload file
            </button>
            <button class="source-button" type="button"
              data-source-group="pair-control-${index}"
              data-source-role="pair-control" data-source-index="${index}" data-source-mode="blot">
              Use blot
            </button>
          </div>

          <div id="pairControlFile${index}" class="source-panel">
            <label class="file-drop">
              <input data-pair-control-file="${index}" type="file" accept=".xlsx,.xls,.csv,.tsv" />
              <span data-pair-control-file-name="${index}">${escapeHtml(state.pairedSets[index]?.control?.fileLabel || "Choose control file")}</span>
            </label>
            <div class="mapping-grid" data-dataset-details="pair-control-${index}"${datasetDetailsHiddenAttr(state.pairedSets[index]?.control)}>
              <label>Sheet<select data-pair-control-sheet="${index}"></select></label>
              <label>Lane column<select data-pair-control-lane="${index}"></select></label>
              <label>Signal column<select data-pair-control-signal="${index}"></select></label>
            </div>
          </div>

          <div id="pairControlBlot${index}" class="source-panel" hidden>
            ${blotSourceControlsHtml(`pair-control-${index}`, "pair-control", index)}
          </div>
        </div>
      </div>
    </div>
  `;
}

async function loadSharedSampleFile(event, index) {
  if (await loadDatasetFile(event, state.sharedSamples[index], sharedSampleControls[index])) {
    invalidateAnalyses();
    refreshNormalizationLanes();
    renderWorkflowState();
  }
}

async function loadSharedControlFile(event) {
  if (await loadDatasetFile(event, state.sharedControl, sharedControlControls())) {
    invalidateAnalyses();
    refreshNormalizationLanes();
    renderWorkflowState();
  }
}

async function loadPairFile(event, index, role) {
  if (await loadDatasetFile(event, state.pairedSets[index][role], pairControls[index][role])) {
    invalidateAnalyses();
    refreshNormalizationLanes();
    renderWorkflowState();
  }
}

async function loadDatasetFile(event, dataset, controls) {
  const file = event.target.files?.[0];
  if (!file) return false;

  try {
    const workbook = await readTabularFile(file);
    const sheetName = workbook.sheetNames[0];
    if (!sheetName) throw new Error("The file does not contain any readable sheets.");
    const rows = workbook.getRows(sheetName).filter((row) =>
      Object.values(row).some((value) => String(value).trim() !== ""),
    );
    const headers = workbook.getHeaders?.(sheetName) || collectHeaders(rows);
    validateTabularHeaders(headers);

    dataset.fileLabel = file.name;
    controls.fileName.textContent = file.name;
    dataset.workbook = workbook;
    dataset.sheetName = sheetName;
    dataset.rows = rows;
    dataset.headers = headers;
    populateSheetSelect(controls.sheet, dataset.workbook);
    controls.sheet.value = sheetName;
    populateColumnSelects(controls, dataset.headers);
    return true;
  } catch (error) {
    clearDataset(dataset, controls);
    invalidateAnalyses();
    renderWorkflowState();
    setWorkflowMessage(`Could not read file. ${error.message}`, "error");
    event.target.value = "";
    return false;
  }
}

async function readTabularFile(file) {
  const extension = file.name.split(".").pop().toLowerCase();
  const maxBytes = Number(CONFIG.MAX_TABULAR_UPLOAD_BYTES || DEFAULT_TABULAR_UPLOAD_BYTES);
  if (!ALLOWED_TABULAR_EXTENSIONS.has(extension)) {
    throw new Error("Use a CSV, TSV, XLS, or XLSX file.");
  }
  if (file.size > maxBytes) {
    throw new Error(`File is too large. Maximum size is ${Math.floor(maxBytes / 1024 / 1024)} MB.`);
  }

  if (extension === "csv" || extension === "tsv") {
    const text = await file.text();
    const delimiter = extension === "tsv" ? "\t" : ",";
    return csvToWorkbook(text, delimiter);
  }

  if (!window.XLSX) {
    throw new Error("The Excel parser has not loaded yet. Check the network connection and retry.");
  }

  const bytes = await file.arrayBuffer();
  const workbook = XLSX.read(bytes, { type: "array" });
  return {
    sheetNames: workbook.SheetNames,
    getHeaders(sheetName) {
      const headerRows = XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], { header: 1, blankrows: false });
      const headerRow = headerRows.find((row) => row.some((value) => String(value ?? "").trim() !== "")) || [];
      return headerRow.map((header) => String(header ?? "").trim());
    },
    getRows(sheetName) {
      return XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], { defval: "" });
    },
  };
}

function csvToWorkbook(text, delimiter) {
  const trimmedText = String(text ?? "").trim();
  if (!trimmedText) throw new Error("The file is empty.");

  const lines = trimmedText.split(/\r?\n/);
  const headers = splitDelimitedLine(lines[0], delimiter).map((header) => header.trim());
  validateTabularHeaders(headers);
  const rows = lines.slice(1).map((line) => {
    const values = splitDelimitedLine(line, delimiter);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
  return {
    sheetNames: ["Data"],
    getHeaders() {
      return headers;
    },
    getRows() {
      return rows;
    },
  };
}

function splitDelimitedLine(line, delimiter) {
  const output = [];
  let cell = "";
  let quoted = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    const next = line[index + 1];
    if (char === '"' && next === '"') {
      cell += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (char === delimiter && !quoted) {
      output.push(cell);
      cell = "";
    } else {
      cell += char;
    }
  }

  output.push(cell);
  return output;
}

function validateTabularHeaders(headers) {
  if (!headers.length || headers.every((header) => header === "")) {
    throw new Error("The file needs a header row.");
  }

  const seen = new Set();
  const duplicates = new Set();
  headers.forEach((header) => {
    if (!header) throw new Error("Column headers cannot be blank.");
    const key = header.toLowerCase();
    if (seen.has(key)) duplicates.add(header);
    seen.add(key);
  });
  if (duplicates.size) {
    throw new Error(`Column headers must be unique. Duplicate headers: ${[...duplicates].join(", ")}.`);
  }
}

function populateSheetSelect(select, workbook) {
  select.innerHTML = workbook.sheetNames
    .map((sheetName) => `<option value="${escapeHtml(sheetName)}">${escapeHtml(sheetName)}</option>`)
    .join("");
}

function selectDatasetSheet(dataset, controls, sheetName) {
  if (!dataset.workbook) return false;
  if (!dataset.workbook.sheetNames.includes(sheetName)) {
    setWorkflowMessage("The selected sheet is no longer available in this workbook.", "error");
    return false;
  }
  try {
    const rows = dataset.workbook.getRows(sheetName).filter((row) =>
      Object.values(row).some((value) => String(value).trim() !== ""),
    );
    const headers = dataset.workbook.getHeaders?.(sheetName) || collectHeaders(rows);
    validateTabularHeaders(headers);
    dataset.sheetName = sheetName;
    dataset.rows = rows;
    dataset.headers = headers;
    populateColumnSelects(controls, dataset.headers);
    return true;
  } catch (error) {
    renderWorkflowState();
    setWorkflowMessage(`Could not read sheet. ${error.message}`, "error");
    return false;
  }
}

function hydrateDatasetControls(dataset, controls) {
  if (!dataset.workbook) return;
  populateSheetSelect(controls.sheet, dataset.workbook);
  controls.sheet.value = dataset.sheetName || dataset.workbook.sheetNames[0];
  populateColumnSelects(controls, dataset.headers);
}

function clearDataset(dataset, controls) {
  dataset.workbook = null;
  dataset.sheetName = "";
  dataset.rows = [];
  dataset.headers = [];
  dataset.fileLabel = "";
  dataset.proteinName = "";
  if (controls?.fileName) controls.fileName.textContent = "Choose Excel or CSV file";
  if (controls?.protein) controls.protein.value = "";
  if (controls?.sheet) controls.sheet.innerHTML = "";
  if (controls?.lane) controls.lane.innerHTML = "";
  if (controls?.signal) controls.signal.innerHTML = "";
}

function collectHeaders(rows) {
  const headers = new Set();
  rows.forEach((row) => Object.keys(row).forEach((key) => headers.add(key)));
  return [...headers];
}

function populateColumnSelects(controls, headers) {
  const guesses = guessColumns(headers);
  setOptions(controls.lane, headers, guesses.lane);
  setOptions(controls.signal, headers, guesses.signal);
}

function setOptions(select, values, selected) {
  select.innerHTML = values
    .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
    .join("");
  if (selected && values.includes(selected)) select.value = selected;
}

function guessColumns(headers) {
  return {
    lane: findExactHeader(headers, "name") ?? findHeader(headers, ["lane", "well", "sample"]) ?? findHeader(headers, ["name"]) ?? headers[0],
    signal: findHeader(headers, ["signal", "intensity", "density", "volume", "band"]) ?? headers[1],
  };
}

function findExactHeader(headers, term) {
  return headers.find((header) => header.trim().toLowerCase() === term);
}

function findHeader(headers, terms) {
  return headers.find((header) => {
    const normalized = header.toLowerCase();
    return terms.some((term) => normalized.includes(term));
  });
}

function refreshNormalizationLanes() {
  const readiness = workflowReadiness();
  updateNormalizationOptions(readiness.commonLanes);
  renderWorkflowState(readiness);
}

function updateNormalizationOptions(lanes) {
  const previous = els.normalizationLane.value;
  els.normalizationLane.innerHTML = lanes
    .map((lane) => `<option value="${escapeHtml(lane)}">${escapeHtml(lane)}</option>`)
    .join("");

  if (lanes.includes(previous)) {
    els.normalizationLane.value = previous;
  } else if (lanes.length) {
    els.normalizationLane.value = lanes[0];
  }
}

function extractLaneNames(dataset, controls) {
  const laneColumn = controls?.lane?.value;
  if (!dataset?.rows?.length || !laneColumn) return [];
  return dataset.rows.map((row, index) => normalizeLaneName(row[laneColumn], index)).filter(Boolean);
}

function hiddenAttr(hidden) {
  return hidden ? " hidden" : "";
}

function datasetHasWorkbook(dataset) {
  return Boolean(dataset?.workbook && Array.isArray(dataset.rows) && dataset.headers?.length);
}

function datasetDetailsHiddenAttr(dataset) {
  return hiddenAttr(!datasetHasWorkbook(dataset));
}

function hasAvailableBlotScans() {
  return blotState.blots.some((blot) => scansForBlot(blot.id).length > 0);
}

function blotSourceControlsHtml(sourceKey, refreshRole, refreshIndex) {
  const hasScans = hasAvailableBlotScans();
  return `
    <p class="workflow-message" data-blot-empty-message="${sourceKey}"${hiddenAttr(hasScans)}>
      Create and save a blot scan before using this source.
    </p>
    <div class="blot-source-fields" data-blot-source-fields="${sourceKey}"${hiddenAttr(!hasScans)}>
      <div class="field-row">
        <label>
          Blot
          <select data-blot-source-blot="${sourceKey}" data-refresh-scan-role="${refreshRole}" data-refresh-scan-index="${refreshIndex}">
            <option value="">-- Select blot --</option>
          </select>
        </label>
      </div>
      <div class="field-row">
        <label>
          Protein scan
          <select data-blot-source-scan="${sourceKey}">
            <option value="">-- Select scan --</option>
          </select>
        </label>
      </div>
    </div>
  `;
}

function setWorkflowMessage(message, tone = "neutral") {
  if (!els.analysisReadiness) return;
  els.analysisReadiness.textContent = message || "";
  els.analysisReadiness.classList.toggle("success", tone === "success");
  els.analysisReadiness.classList.toggle("error", tone === "error");
}

function renderWorkflowState(readiness = workflowReadiness()) {
  updateDatasetDetailVisibility();
  updateBlotSourceFieldVisibility();
  updateNormalizationOptions(readiness.commonLanes);

  const hasSharedResults = state.mode === "shared" && state.sharedAnalyses.length > 0;
  const hasComparisonResults = state.mode === "comparison" && state.pairedAnalyses.length > 0;
  const hasCurrentResults = hasSharedResults || hasComparisonResults;

  if (els.normalizationLaneWrap) els.normalizationLaneWrap.hidden = !readiness.dataReady;
  els.sharedResults.hidden = !hasSharedResults;
  els.comparisonResults.hidden = !hasComparisonResults;
  els.groupControlsPanel.hidden = !hasCurrentResults;
  if (els.groupModeWrap) els.groupModeWrap.hidden = !hasCurrentResults;
  if (els.groupSizeWrap) els.groupSizeWrap.hidden = !hasCurrentResults;
  if (els.groupToggleWrap) els.groupToggleWrap.hidden = !hasCurrentResults;

  const tone = readiness.ready ? "success" : "error";
  setWorkflowMessage(readiness.message, tone);
}

function updateDatasetDetailVisibility() {
  if (els.controlProteinRow) els.controlProteinRow.hidden = !datasetHasWorkbook(state.sharedControl);
  if (els.controlMappingGrid) els.controlMappingGrid.hidden = !datasetHasWorkbook(state.sharedControl);

  state.sharedSamples.forEach((dataset, index) => {
    document.querySelectorAll(`[data-dataset-details="sample-${index}"]`).forEach((element) => {
      element.hidden = !datasetHasWorkbook(dataset);
    });
  });

  state.pairedSets.forEach((pair, index) => {
    ["sample", "control"].forEach((role) => {
      document.querySelectorAll(`[data-dataset-details="pair-${role}-${index}"]`).forEach((element) => {
        element.hidden = !datasetHasWorkbook(pair[role]);
      });
    });
  });
}

function updateBlotSourceFieldVisibility() {
  const hasScans = hasAvailableBlotScans();
  document.querySelectorAll("[data-blot-source-fields]").forEach((element) => {
    element.hidden = !hasScans;
  });
  document.querySelectorAll("[data-blot-empty-message]").forEach((element) => {
    element.hidden = hasScans;
  });
}

function workflowReadiness() {
  const result = state.mode === "comparison"
    ? comparisonWorkflowReadiness()
    : sharedWorkflowReadiness();
  if (!result.dataReady) return result;

  const laneSets = result.sources.map((source) => new Set(source.lanes));
  const commonLanes = laneSets.length
    ? [...laneSets[0]].filter((lane) => laneSets.every((set) => set.has(lane)))
    : [];
  result.commonLanes = commonLanes;

  if (!commonLanes.length) {
    return {
      ...result,
      ready: false,
      message: "No lane is present in every required source. Check lane names before analyzing.",
    };
  }

  const adjustmentText = state.mode === "shared" && result.loadingControlAdjusted === false
    ? " without loading control adjustment"
    : "";
  return {
    ...result,
    ready: true,
    message: `Ready to analyze ${commonLanes.length} shared lane${commonLanes.length === 1 ? "" : "s"}${adjustmentText}.`,
  };
}

function sharedWorkflowReadiness() {
  const sources = [];
  const control = optionalSharedControlReadiness();
  if (!control.ready) return workflowBlocked(control.message);
  if (control.source) sources.push(control.source);

  for (let index = 0; index < state.sharedSamples.length; index += 1) {
    const sample = sourceReadiness({
      role: "sample",
      index,
      label: `Sample ${index + 1}`,
      dataset: state.sharedSamples[index],
      controls: sharedSampleControls[index],
    });
    if (!sample.ready) return workflowBlocked(sample.message);
    sources.push(sample.source);
  }

  return {
    dataReady: true,
    ready: false,
    message: "",
    sources,
    commonLanes: [],
    loadingControlAdjusted: Boolean(control.source),
  };
}

function optionalSharedControlReadiness() {
  const label = "Loading control";
  if (isBlotSourceActive("control", 0)) {
    const blotSelect = document.querySelector(`[data-blot-source-blot="control-0"]`);
    const scanSelect = document.querySelector(`[data-blot-source-scan="control-0"]`);
    if (!blotSelect?.value || scanSelect?.value === "") {
      return { ready: true, source: null };
    }
    return blotSourceReadiness("control", 0, label);
  }

  if (!datasetHasWorkbook(state.sharedControl)) {
    return { ready: true, source: null };
  }
  return fileSourceReadiness(state.sharedControl, sharedControlControls(), label);
}

function comparisonWorkflowReadiness() {
  const sources = [];
  for (let index = 0; index < state.pairedSets.length; index += 1) {
    for (const role of ["sample", "control"]) {
      const status = sourceReadiness({
        role: `pair-${role}`,
        index,
        label: `Pair ${index + 1} ${role}`,
        dataset: state.pairedSets[index]?.[role],
        controls: pairControls[index]?.[role],
      });
      if (!status.ready) return workflowBlocked(status.message);
      sources.push(status.source);
    }
  }

  return { dataReady: true, ready: false, message: "", sources, commonLanes: [] };
}

function workflowBlocked(message) {
  return {
    dataReady: false,
    ready: false,
    message,
    sources: [],
    commonLanes: [],
  };
}

function sourceReadiness({ role, index, label, dataset, controls }) {
  if (isBlotSourceActive(role, index)) {
    return blotSourceReadiness(role, index, label);
  }
  return fileSourceReadiness(dataset, controls, label);
}

function fileSourceReadiness(dataset, controls, label) {
  if (!datasetHasWorkbook(dataset)) {
    return { ready: false, message: `Upload ${label.toLowerCase()} data to continue.` };
  }
  if (!controls?.lane?.value || !controls?.signal?.value) {
    return { ready: false, message: `Choose lane and signal columns for ${label.toLowerCase()}.` };
  }
  try {
    const rows = buildRows(dataset, controls, label);
    if (!rows.length) {
      return { ready: false, message: `${label} needs readable lanes and signals.` };
    }
    assertUniqueLanes(rows, label);
    return sourceReady(label, rows);
  } catch (error) {
    return { ready: false, message: error.message };
  }
}

function blotSourceReadiness(role, index, label) {
  if (!hasAvailableBlotScans()) {
    return { ready: false, message: "Create and save a blot scan before using blot data." };
  }
  const blotSelect = document.querySelector(`[data-blot-source-blot="${role}-${index}"]`);
  const scanSelect = document.querySelector(`[data-blot-source-scan="${role}-${index}"]`);
  if (!blotSelect?.value || scanSelect?.value === "") {
    return { ready: false, message: `Select a blot and scan for ${label.toLowerCase()}.` };
  }
  const dataset = scanToDataset(blotSelect.value, scanSelect.value);
  if (!dataset) {
    return { ready: false, message: `${label}'s selected blot scan is no longer available.` };
  }
  return fileSourceReadiness(dataset, blotDatasetControls(), label);
}

function sourceReady(label, rows) {
  return {
    ready: true,
    source: {
      label,
      rows,
      lanes: rows.map((row) => row.lane),
    },
  };
}

function invalidateAnalyses() {
  state.sharedAnalyses = [];
  state.pairedAnalyses = [];
  state.comparisonCustomGroups = [];
  state.activeSampleIndex = 0;

  els.sampleTabsPanel.hidden = true;
  els.sampleTabs.innerHTML = "";
  els.downloadChartButton.disabled = true;
  els.downloadCsvButton.disabled = true;
  els.downloadWorkbookButton.disabled = true;
  els.downloadComparisonChartButton.disabled = true;
  els.downloadComparisonWorkbookButton.disabled = true;
  els.labelPanel.hidden = true;
  els.groupPanel.hidden = true;
  els.comparisonLabelPanel.hidden = true;
  els.comparisonChartPanel.hidden = true;
  els.comparisonGroupPanel.hidden = true;
  els.comparisonCustomGroupingPanel.hidden = true;
  document.getElementById("pptxPanel").hidden = true;

  els.chartTitle.textContent = "Upload files to begin";
  els.comparisonChartTitle.textContent = "Comparison graph";
  els.resultTableBody.innerHTML = `<tr><td colspan="4">No analysis yet.</td></tr>`;
  drawEmptyChart(els.foldChart);
  drawEmptyChart(els.comparisonChart);
}

function blotDatasetControls() {
  return { lane: { value: "Name" }, signal: { value: "Signal" } };
}

function isBlotSourceActive(role, index) {
  const panel = document.getElementById(sourcePanelId(role, "blot", index));
  return Boolean(panel && !panel.hidden);
}

function sourcePanelId(role, mode, index) {
  const camelCaseRole = role.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
  const capitalizedMode = mode[0].toUpperCase() + mode.slice(1);
  return `${camelCaseRole}${capitalizedMode}${index}`;
}

function getBlotSourceDataset(role, index) {
  if (!isBlotSourceActive(role, index)) return null;

  const blotSelect = document.querySelector(`[data-blot-source-blot="${role}-${index}"]`);
  const scanSelect = document.querySelector(`[data-blot-source-scan="${role}-${index}"]`);
  if (!blotSelect?.value || scanSelect?.value === "") return null;

  return scanToDataset(blotSelect.value, scanSelect.value);
}

function getPairAnalysisSource(index, role) {
  if (isBlotSourceActive(`pair-${role}`, index)) {
    const blotSelect = document.querySelector(`[data-blot-source-blot="pair-${role}-${index}"]`);
    const scanSelect = document.querySelector(`[data-blot-source-scan="pair-${role}-${index}"]`);
    const blotDataset = getBlotSourceDataset(`pair-${role}`, index);
    if (!blotDataset) {
      throw new Error(`Pair ${index + 1} ${role} needs a selected blot scan.`);
    }
    return {
      dataset: blotDataset,
      controls: blotDatasetControls(),
      label: blotDataset.proteinName || `Pair ${index + 1} ${role}`,
      sourceType: "blot",
      blotId: blotSelect?.value || "",
      scanId: scanSelect?.value || "",
    };
  }

  return {
    dataset: state.pairedSets[index][role],
    controls: pairControls[index][role],
    label: role === "sample"
      ? pairControls[index].sample.label.value.trim() || `Sample ${index + 1}`
      : `Pair ${index + 1} control`,
    sourceType: "file",
  };
}

function runAnalysis() {
  renderWorkflowState();
  const readiness = workflowReadiness();
  if (!readiness.ready) {
    setWorkflowMessage(readiness.message, "error");
    return;
  }
  if (state.mode === "comparison") {
    runComparisonAnalysis();
  } else {
    runSharedAnalysis();
  }
}

function renderCurrentGrouping() {
  if (state.mode === "comparison") {
    renderComparisonChart();
  } else {
    renderGroupedGraphs();
  }
}

function runSharedAnalysis() {
  try {
    const controlSource = getSharedControlSource();
    const controlRows = controlSource ? buildSharedRows(controlSource, controlSource.label) : null;
    if (controlSource && !controlRows.length) throw new Error("The control needs readable lanes and signals.");
    const normalizationLane = els.normalizationLane.value;

    state.sharedAnalyses = state.sharedSamples.map((sampleDataset, index) =>
      buildSharedAnalysis(sampleDataset, index, controlSource, controlRows, normalizationLane)
    );

    state.activeSampleIndex = Math.min(state.activeSampleIndex, state.sharedAnalyses.length - 1);
    renderSampleTabs();
    renderActiveSharedAnalysis();
    renderWorkflowState();
  } catch (error) {
    showSharedError(error.message);
    renderWorkflowState();
    setWorkflowMessage(error.message, "error");
  }
}

function getSharedControlSource() {
  const blotSelect = document.querySelector(`[data-blot-source-blot="control-0"]`);
  const scanSelect = document.querySelector(`[data-blot-source-scan="control-0"]`);
  const usingBlot = blotSelect && !document.getElementById("controlBlot0")?.hidden;

  if (!usingBlot) {
    if (!datasetHasWorkbook(state.sharedControl)) return null;
    return {
      dataset: state.sharedControl,
      controls: sharedControlControls(),
      label: els.controlProtein.value.trim() || "Loading control",
      sourceType: "file",
    };
  }

  if (!blotSelect.value || scanSelect.value === "") {
    return null;
  }
  const dataset = scanToDataset(blotSelect.value, scanSelect.value);
  if (!dataset) throw new Error("The selected loading control scan is no longer available.");
  return {
    dataset,
    controls: blotDatasetControls(),
    label: dataset.proteinName,
    sourceType: "blot",
    blotId: blotSelect.value,
    scanId: scanSelect.value,
  };
}

function getSharedSampleSource(sampleDataset, index) {
  const blotSelect = document.querySelector(`[data-blot-source-blot="sample-${index}"]`);
  const scanSelect = document.querySelector(`[data-blot-source-scan="sample-${index}"]`);
  const usingBlot = blotSelect && !document.getElementById(`sampleBlot${index}`)?.hidden;

  if (!usingBlot) {
    const controls = sharedSampleControls[index];
    return {
      dataset: sampleDataset,
      controls,
      label: controls.protein.value.trim() || `Sample ${index + 1}`,
      sourceType: "file",
    };
  }

  if (!blotSelect.value || scanSelect.value === "") {
    throw new Error(`Sample ${index + 1} needs a selected blot scan.`);
  }
  const dataset = scanToDataset(blotSelect.value, scanSelect.value);
  if (!dataset) throw new Error(`Sample ${index + 1}'s selected blot scan is no longer available.`);
  return {
    dataset,
    controls: blotDatasetControls(),
    label: dataset.proteinName,
    sourceType: "blot",
    blotId: blotSelect.value,
    scanId: scanSelect.value,
  };
}

function buildSharedRows(source, label) {
  return buildRows(source.dataset, source.controls, label);
}

function analysisSourceSnapshot({ mode, role, index, source, rows }) {
  return {
    mode,
    role,
    index,
    label: source.label || role,
    sourceType: source.sourceType || "file",
    fileLabel: source.dataset?.fileLabel || "",
    sheetName: source.dataset?.sheetName || "",
    blotId: source.blotId || "",
    scanId: source.scanId || "",
    rows: rows.map((row, rowIndex) => ({
      rowIndex: rowIndex + 1,
      lane: row.lane,
      displayLane: row.displayLane || row.lane,
      signal: row.signal,
    })),
  };
}

function buildSharedAnalysis(sampleDataset, index, controlSource, controlRows, normalizationLane) {
  const sampleSource = getSharedSampleSource(sampleDataset, index);
  const sampleRows = buildSharedRows(sampleSource, sampleSource.label);
  if (!sampleRows.length) throw new Error(`Sample ${index + 1} needs readable lanes and signals.`);

  sampleDataset.proteinName = sampleSource.label;
  const controlLabel = controlSource?.label || "";
  const title = controlSource
    ? `${sampleSource.label} - ${controlLabel} fold change`
    : `${sampleSource.label} fold change`;
  const sources = [
    analysisSourceSnapshot({ mode: "Shared control", role: "Sample", index: index + 1, source: sampleSource, rows: sampleRows }),
  ];
  if (controlSource) {
    sources.unshift(analysisSourceSnapshot({ mode: "Shared control", role: "Control", index: 1, source: controlSource, rows: controlRows }));
  }
  return createAnalysisState(
    sampleSource.label,
    title,
    computeFoldChange(sampleRows, controlRows, normalizationLane, sampleSource.label, controlLabel),
    {
      normalizationLane,
      loadingControlAdjusted: Boolean(controlSource),
      sources,
    },
  );
}

function runComparisonAnalysis() {
  try {
    const normalizationLane = els.normalizationLane.value;
    state.pairedAnalyses = state.pairedSets.map((pair, index) => {
      const sampleSource = getPairAnalysisSource(index, "sample");
      const controlSource = getPairAnalysisSource(index, "control");
      const sampleRows = buildRows(sampleSource.dataset, sampleSource.controls, `Pair ${index + 1} sample`);
      const controlRows = buildRows(controlSource.dataset, controlSource.controls, `Pair ${index + 1} control`);
      if (!sampleRows.length || !controlRows.length) {
        throw new Error(`Pair ${index + 1} needs both sample and control lanes/signals.`);
      }

      const label = pairControls[index].sample.label.value.trim() || `Sample ${index + 1}`;
      pair.label = label;
      return createAnalysisState(
        label,
        label,
        computeFoldChange(sampleRows, controlRows, normalizationLane, `${label} sample`, `${label} control`),
        {
          normalizationLane,
          sources: [
            analysisSourceSnapshot({ mode: "Pairs", role: "Sample", index: index + 1, source: { ...sampleSource, label }, rows: sampleRows }),
            analysisSourceSnapshot({ mode: "Pairs", role: "Control", index: index + 1, source: controlSource, rows: controlRows }),
          ],
        },
      );
    });
    state.comparisonCustomGroups = createDefaultCustomGroups(buildComparisonRows(), comparisonRowKey);

    renderComparisonLabelEditor();
    const chartOk = renderComparisonChart();
    els.comparisonLabelPanel.hidden = false;
    els.comparisonChartPanel.hidden = false;
    els.downloadComparisonChartButton.disabled = !chartOk;
    els.downloadComparisonWorkbookButton.disabled = !chartOk;
    renderWorkflowState();
  } catch (error) {
    state.pairedAnalyses = [];
    els.comparisonChartTitle.innerHTML = `<span class="error-text">${escapeHtml(error.message)}</span>`;
    els.comparisonLabelPanel.hidden = true;
    els.comparisonChartPanel.hidden = false;
    els.downloadComparisonChartButton.disabled = true;
    els.downloadComparisonWorkbookButton.disabled = true;
    drawEmptyChart(els.comparisonChart);
    renderWorkflowState();
    setWorkflowMessage(error.message, "error");
  }
}

function buildRows(dataset, controls, label = dataset?.fileLabel || "Dataset") {
  const laneColumn = controls?.lane?.value;
  const signalColumn = controls?.signal?.value;
  if (!dataset?.rows?.length || !laneColumn || !signalColumn) return [];

  const invalidRows = [];
  const rows = dataset.rows.map((row, index) => {
    const lane = normalizeLaneName(row[laneColumn], index);
    const signal = parseSignal(row[signalColumn]);
    if (!Number.isFinite(signal)) invalidRows.push(index + 1);
    return { lane, displayLane: lane, signal };
  });

  if (invalidRows.length) {
    throw new Error(`${label} has invalid signal values in row${invalidRows.length === 1 ? "" : "s"} ${formatRowList(invalidRows)}.`);
  }

  return rows.filter((row) => row.lane);
}

function assertUniqueLanes(rows, label) {
  const seen = new Set();
  const duplicates = new Set();
  rows.forEach((row) => {
    if (seen.has(row.lane)) duplicates.add(row.lane);
    seen.add(row.lane);
  });
  if (duplicates.size) {
    throw new Error(`${label} has duplicate lane names: ${[...duplicates].join(", ")}.`);
  }
}

function computeFoldChange(sampleRows, controlRows, normalizationLane, sampleLabel = "Sample", controlLabel = "Control") {
  assertUniqueLanes(sampleRows, sampleLabel);
  const hasControlRows = Array.isArray(controlRows) && controlRows.length > 0;
  if (hasControlRows) assertUniqueLanes(controlRows, controlLabel);

  const sampleMap = new Map(sampleRows.map((row) => [row.lane, row]));
  const controlMap = hasControlRows ? new Map(controlRows.map((row) => [row.lane, row])) : new Map();
  const baselineSample = sampleMap.get(normalizationLane);
  const baselineControl = hasControlRows ? controlMap.get(normalizationLane) : null;

  if (!baselineSample || (hasControlRows && !baselineControl)) {
    throw new Error(hasControlRows
      ? "The normalization lane must exist in every sample and control file."
      : "The normalization lane must exist in every sample file.");
  }
  if (baselineSample.signal === 0 || (hasControlRows && baselineControl.signal === 0)) {
    throw new Error(hasControlRows
      ? "The normalization lane must have non-zero sample and control signals."
      : "The normalization lane must have a non-zero sample signal.");
  }

  return sampleRows.map((sampleRow) => {
    const samplePercent = (sampleRow.signal / baselineSample.signal) * 100;
    if (!hasControlRows) {
      return {
        lane: sampleRow.lane,
        displayLane: sampleRow.displayLane,
        sampleSignal: sampleRow.signal,
        controlSignal: null,
        samplePercent,
        controlPercent: null,
        foldChange: samplePercent / 100,
      };
    }

    const controlRow = controlMap.get(sampleRow.lane);
    if (!controlRow) throw new Error(`No matching control lane found for "${sampleRow.lane}".`);

    const controlPercent = (controlRow.signal / baselineControl.signal) * 100;
    if (controlPercent === 0) throw new Error(`Control lane "${sampleRow.lane}" has a zero normalized signal.`);

    return {
      lane: sampleRow.lane,
      displayLane: sampleRow.displayLane,
      sampleSignal: sampleRow.signal,
      controlSignal: controlRow.signal,
      samplePercent,
      controlPercent,
      foldChange: samplePercent / controlPercent,
    };
  });
}

function renderSampleTabs() {
  els.sampleTabsPanel.hidden = state.sharedAnalyses.length <= 1;
  els.sampleTabs.innerHTML = state.sharedAnalyses
    .map(
      (analysis, index) => `
        <button class="tab-button" type="button" data-sample-tab="${index}" aria-selected="${index === state.activeSampleIndex}">
          ${escapeHtml(analysis.name)}
        </button>
      `,
    )
    .join("");
}

function selectSampleTab(event) {
  const button = event.target.closest("[data-sample-tab]");
  if (!button) return;
  state.activeSampleIndex = Number(button.dataset.sampleTab);
  renderSampleTabs();
  renderActiveSharedAnalysis();
}

function activeAnalysis() {
  return state.sharedAnalyses[state.activeSampleIndex];
}

function renderActiveSharedAnalysis() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  els.chartTitle.textContent = analysis.title;
  drawBarChart(els.foldChart, analysis.results, analysis.title);
  renderResultTable(analysis.results);
  renderLabelEditor(analysis.results);
  renderGroupedGraphs();
  els.downloadChartButton.disabled = false;
  els.downloadCsvButton.disabled = false;
  els.downloadWorkbookButton.disabled = false;
  els.labelPanel.hidden = false;
  document.getElementById("pptxPanel").hidden = false;
  renderWorkflowState();
}

function drawEmptyChart(canvas) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--surface-subtle");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--text-muted");
  ctx.font = themeFont(600, 22);
  ctx.textAlign = "center";
  ctx.fillText("Your chart will appear here", canvas.width / 2, canvas.height / 2);
}

function drawChartMessage(canvas, message) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--surface-subtle");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--danger");
  ctx.font = themeFont(600, 16);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(message, canvas.width / 2, canvas.height / 2);
}

function drawBarChart(canvas, rows, title) {
  const ctx = canvas.getContext("2d");
  const padding = { top: 42, right: 32, bottom: 92, left: 72 };
  const plotWidth = canvas.width - padding.left - padding.right;
  const plotHeight = canvas.height - padding.top - padding.bottom;
  const maxValue = Math.max(1.2, ...rows.map((row) => row.foldChange)) * 1.16;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--surface");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGrid(ctx, canvas, padding, plotHeight, maxValue);

  const band = plotWidth / Math.max(rows.length, 1);
  const barWidth = Math.min(56, band * 0.56);
  rows.forEach((row, index) => {
    const x = padding.left + band * index + (band - barWidth) / 2;
    const barHeight = (row.foldChange / maxValue) * plotHeight;
    const y = padding.top + plotHeight - barHeight;
    ctx.fillStyle = themeToken("--chart-series-1");
    ctx.fillRect(x, y, barWidth, barHeight);
    drawValueLabel(ctx, row.foldChange, x + barWidth / 2, y - 12);
    drawAngledLabel(ctx, row.displayLane || row.lane, x + barWidth / 2, padding.top + plotHeight + 18);
  });

  drawAxes(ctx, canvas, padding, plotHeight);
  drawTitle(ctx, canvas, title);
}

function drawGrid(ctx, canvas, padding, plotHeight, maxValue) {
  ctx.strokeStyle = themeToken("--border");
  ctx.lineWidth = 1;
  ctx.fillStyle = themeToken("--text-muted");
  ctx.font = themeFont(500, 13, "--font-mono");
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= 5; index += 1) {
    const value = (maxValue / 5) * index;
    const y = padding.top + plotHeight - (value / maxValue) * plotHeight;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(canvas.width - padding.right, y);
    ctx.stroke();
    ctx.fillText(value.toFixed(1), padding.left - 10, y);
  }
}

function drawAxes(ctx, canvas, padding, plotHeight) {
  ctx.strokeStyle = themeToken("--text");
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, padding.top + plotHeight);
  ctx.lineTo(canvas.width - padding.right, padding.top + plotHeight);
  ctx.stroke();
}

function drawTitle(ctx, canvas, title) {
  ctx.fillStyle = themeToken("--text");
  ctx.font = themeFont(600, 19);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillText(title, canvas.width / 2, 8);
}

function drawValueLabel(ctx, value, x, y) {
  ctx.fillStyle = themeToken("--text");
  ctx.font = themeFont(600, 12, "--font-mono");
  ctx.textAlign = "center";
  ctx.fillText(value.toFixed(2), x, y);
}

function drawAngledLabel(ctx, text, x, y) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(-Math.PI / 5);
  ctx.textAlign = "right";
  ctx.fillStyle = themeToken("--text-secondary");
  ctx.font = themeFont(600, 12);
  ctx.fillText(text, 0, 0);
  ctx.restore();
}

function renderResultTable(rows) {
  els.resultTableBody.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.displayLane || row.lane)}</td>
          <td>${formatNumber(row.samplePercent)}</td>
          <td>${formatNumber(row.controlPercent)}</td>
          <td><strong>${formatNumber(row.foldChange)}</strong></td>
        </tr>
      `,
    )
    .join("");
}

function renderLabelEditor(rows) {
  els.labelEditor.innerHTML = rows
    .map(
      (row, index) => `
        <label>
          ${escapeHtml(row.lane)}
          <input type="text" value="${escapeHtml(row.displayLane || row.lane)}" data-label-index="${index}" />
        </label>
      `,
    )
    .join("");
}

function updateSharedLaneLabel(event) {
  const input = event.target.closest("[data-label-index]");
  const analysis = activeAnalysis();
  if (!input || !analysis) return;
  const index = Number(input.dataset.labelIndex);
  // Keep every view synchronized with the edited display label.
  analysis.results[index].displayLane = input.value || analysis.results[index].lane;
  drawBarChart(els.foldChart, analysis.results, analysis.title);
  renderResultTable(analysis.results);
  renderGroupedGraphs();
}

function renderGroupedGraphs() {
  const analysis = activeAnalysis();
  const shouldShow = els.enableGroupedGraphs.checked && analysis;
  els.groupPanel.hidden = !shouldShow;
  if (!shouldShow) return;
  const groups = buildGroupedRows(analysis);
  renderCustomGroupingPanel(analysis);
  renderGroupSummary(groups);
  renderMiniCharts(groups);
}

function buildGroupedRows(analysis) {
  const mode = els.groupMode.value;
  const size = clampInteger(Number(els.groupSize.value), 2, 24, 2);
  if (mode === "blocks") return buildBlockGroups(analysis.results, size);
  if (mode === "custom") return buildCustomGroups(analysis);
  return buildInterleavedGroups(analysis.results, size);
}

// Builds positional groups for both single-analysis and comparison row shapes.
function buildInterleavedGroups(rows, size, labelForRow = (row) => row.displayLane || row.lane) {
  const groups = Array.from({ length: size }, (_, offset) => ({ offset, rows: [] }));
  rows.forEach((row, index) => {
    groups[index % size].rows.push(row);
  });
  return groups
    .filter((group) => group.rows.length)
    .map((group) => ({
      name: `Group ${group.offset + 1}: ${group.rows.map(labelForRow).join(", ")}`,
      rows: group.rows,
    }));
}

function buildBlockGroups(rows, size) {
  const groups = [];
  for (let index = 0; index < rows.length; index += size) {
    const groupRows = rows.slice(index, index + size);
    groups.push({ name: `Group ${groups.length + 1}: lanes ${index + 1}-${index + groupRows.length}`, rows: groupRows });
  }
  return groups;
}

function analysisRowKey(row, index) {
  return String(row?.lane ?? index);
}

function comparisonRowKey(row, index) {
  return String(row?.label ?? index);
}

function rowKeyMap(rows, keyForRow) {
  const map = new Map();
  rows.forEach((row, index) => {
    const key = keyForRow(row, index);
    if (key && !map.has(key)) map.set(key, row);
  });
  return map;
}

function groupSelectionKeys(group, rows, keyForRow) {
  const availableKeys = new Set(rows.map((row, index) => keyForRow(row, index)));
  const keys = Array.isArray(group.keys)
    ? group.keys.map((key) => String(key))
    : Array.isArray(group.indices)
      ? group.indices.map((rowIndex) => {
        const row = rows[rowIndex];
        return row ? keyForRow(row, rowIndex) : "";
      })
      : [];
  return [...new Set(keys)].filter((key) => availableKeys.has(key));
}

function setGroupSelectionKeys(group, rows, keyForRow, selectedKeys) {
  const wanted = new Set(selectedKeys.map((key) => String(key)));
  const orderedKeys = [];
  const orderedIndices = [];
  rows.forEach((row, index) => {
    const key = keyForRow(row, index);
    if (wanted.has(key)) {
      orderedKeys.push(key);
      orderedIndices.push(index);
    }
  });
  group.keys = orderedKeys;
  group.indices = orderedIndices;
}

function updateGroupSelection(group, rows, keyForRow, rowKey, checked) {
  const keys = groupSelectionKeys(group, rows, keyForRow);
  const selected = new Set(keys);
  if (checked) selected.add(rowKey);
  else selected.delete(rowKey);
  setGroupSelectionKeys(group, rows, keyForRow, selected);
}

// Resolves saved lane selections against the current row collection.
function buildSelectedGroups(groupDefinitions, rows, keyForRow = analysisRowKey) {
  const rowsByKey = rowKeyMap(rows, keyForRow);
  return groupDefinitions
    .map((group) => ({
      name: String(group.name ?? "").trim() || "Custom group",
      rows: groupSelectionKeys(group, rows, keyForRow).map((key) => rowsByKey.get(key)).filter(Boolean),
    }))
    .filter((group) => group.rows.length);
}

function buildCustomGroups(analysis) {
  return buildSelectedGroups(analysis.customGroups, analysis.results, analysisRowKey);
}

function groupBaselineError(group) {
  return `Group "${group.name}" cannot be normalized because its first lane has a zero or missing fold change.`;
}

function normalizedGroupRows(group) {
  const baseline = group.rows[0]?.foldChange;
  if (!Number.isFinite(baseline) || baseline === 0) return null;
  if (group.rows.some((row) => !Number.isFinite(row.foldChange))) return null;
  return group.rows.map((row) => ({ ...row, foldChange: row.foldChange / baseline }));
}

function renderGroupSummary(groups) {
  els.groupSummary.innerHTML = groups
    .map((group) => {
      const normalizedRows = normalizedGroupRows(group);
      if (!normalizedRows) {
        return `<div class="group-box"><h3>${escapeHtml(group.name)}</h3><p class="error-text">${escapeHtml(groupBaselineError(group))}</p></div>`;
      }
      const items = normalizedRows
        .map((row) => `<li>${escapeHtml(row.displayLane || row.lane)}: ${formatNumber(row.foldChange)}x</li>`)
        .join("");
      return `<div class="group-box"><h3>${escapeHtml(group.name)}</h3><ul>${items}</ul></div>`;
    })
    .join("");
}

function renderMiniCharts(groups) {
  els.blockGraphs.innerHTML = groups
    .map(
      (group, index) => `
        <div class="mini-chart">
          <h3>${escapeHtml(group.name)}</h3>
          <button class="ghost-button" type="button" data-download-canvas="groupChart${index}" data-download-name="${escapeHtml(filenameSafe(group.name))}.jpg">Export JPG</button>
          <canvas id="groupChart${index}" width="640" height="360" aria-label="Grouped graph ${index + 1}"></canvas>
        </div>
      `,
    )
    .join("");
  groups.forEach((group, index) => {
    const canvas = document.querySelector(`#groupChart${index}`);
    const normalizedRows = normalizedGroupRows(group);
    if (!normalizedRows) {
      drawChartMessage(canvas, groupBaselineError(group));
      return;
    }
    drawBarChart(
      canvas,
      normalizedRows,
      group.name,
    );
  });
}

function renderCustomGroupingPanel(analysis) {
  const isCustom = els.groupMode.value === "custom" && els.enableGroupedGraphs.checked;
  els.customGroupingPanel.hidden = !isCustom;
  if (!isCustom) return;
  if (!analysis.customGroups.length) analysis.customGroups = createDefaultCustomGroups(analysis.results, analysisRowKey);
  els.customGroups.innerHTML = analysis.customGroups
    .map((group, groupIndex) => {
      const selectedKeys = new Set(groupSelectionKeys(group, analysis.results, analysisRowKey));
      return `
        <div class="custom-group-card">
          <input type="text" value="${escapeHtml(group.name)}" data-custom-name="${groupIndex}" />
          <div class="lane-picks">
            ${analysis.results
              .map((row, laneIndex) => {
                const rowKey = analysisRowKey(row, laneIndex);
                return `
                  <label>
                    <input type="checkbox" data-custom-group="${groupIndex}" data-custom-lane-key="${escapeHtml(rowKey)}" ${selectedKeys.has(rowKey) ? "checked" : ""} />
                    ${escapeHtml(row.displayLane || row.lane)}
                  </label>
                `;
              })
              .join("")}
          </div>
        </div>
      `;
    })
    .join("");
}

function updateCustomGroups(event) {
  const analysis = activeAnalysis();
  if (!analysis) return;
  const nameInput = event.target.closest("[data-custom-name]");
  if (nameInput) {
    const groupIndex = Number(nameInput.dataset.customName);
    if (analysis.customGroups[groupIndex]) analysis.customGroups[groupIndex].name = nameInput.value;
    renderGroupSummary(buildGroupedRows(analysis));
    renderMiniCharts(buildGroupedRows(analysis));
    return;
  }

  const checkbox = event.target.closest("[data-custom-group]");
  if (!checkbox) return;
  const group = analysis.customGroups[Number(checkbox.dataset.customGroup)];
  if (!group || !checkbox.dataset.customLaneKey) return;
  updateGroupSelection(group, analysis.results, analysisRowKey, checkbox.dataset.customLaneKey, checkbox.checked);
  renderGroupedGraphs();
}

function addCustomGroup() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  analysis.customGroups.push({ name: `Custom group ${analysis.customGroups.length + 1}`, indices: [], keys: [] });
  renderGroupedGraphs();
}

function renderComparisonLabelEditor() {
  els.comparisonLabelEditor.innerHTML = state.pairedAnalyses
    .map(
      (analysis, pairIndex) => `
        <div class="comparison-label-card">
          <h3>${escapeHtml(analysis.name)}</h3>
          <div class="comparison-label-grid">
            ${analysis.results
              .map(
                (row, laneIndex) => `
                  <label>
                    ${escapeHtml(row.lane)}
                    <input type="text" value="${escapeHtml(row.displayLane || row.lane)}" data-pair-label="${pairIndex}" data-pair-lane="${laneIndex}" />
                  </label>
                `,
              )
              .join("")}
          </div>
        </div>
      `,
    )
    .join("");
}

function updateComparisonLaneLabel(event) {
  const input = event.target.closest("[data-pair-label]");
  if (!input) return;
  const analysis = state.pairedAnalyses[Number(input.dataset.pairLabel)];
  const row = analysis?.results[Number(input.dataset.pairLane)];
  if (!row) return;
  row.displayLane = input.value.trim() || row.lane;
  renderComparisonChart();
}

function renderComparisonChart() {
  if (!state.pairedAnalyses.length) return false;
  try {
    const rows = buildComparisonRows();
    els.comparisonChartTitle.textContent = rows.length
      ? "Common lane comparison"
      : "No shared lane labels found";

    if (els.comparisonChartType.value === "points") {
      drawAveragePointChart(els.comparisonChart, rows, state.pairedAnalyses.map((analysis) => analysis.name));
    } else {
      drawGroupedComparisonChart(els.comparisonChart, rows, state.pairedAnalyses.map((analysis) => analysis.name));
    }

    els.downloadComparisonChartButton.disabled = false;
    els.downloadComparisonWorkbookButton.disabled = false;
    renderComparisonGroupedGraphs(rows);
    return true;
  } catch (error) {
    els.comparisonChartTitle.innerHTML = `<span class="error-text">${escapeHtml(error.message)}</span>`;
    drawEmptyChart(els.comparisonChart);
    els.comparisonGroupPanel.hidden = true;
    els.comparisonCustomGroupingPanel.hidden = true;
    els.downloadComparisonChartButton.disabled = true;
    els.downloadComparisonWorkbookButton.disabled = true;
    return false;
  }
}

function buildComparisonRows() {
  const labelMaps = state.pairedAnalyses.map(comparisonLabelMap);
  if (!labelMaps.length) return [];
  const commonLabels = [...labelMaps[0].keys()].filter((label) => labelMaps.every((map) => map.has(label)));
  return commonLabels.map((label) => ({
    label,
    values: labelMaps.map((map) => {
      const row = map.get(label);
      if (!row || !Number.isFinite(row.foldChange)) {
        throw new Error(`Comparison lane "${label}" has an invalid fold change.`);
      }
      return row.foldChange;
    }),
  }));
}

function comparisonLabelMap(analysis) {
  const rowsByLabel = new Map();
  const duplicates = new Set();
  analysis.results.forEach((row) => {
    const label = row.displayLane || row.lane;
    if (rowsByLabel.has(label)) duplicates.add(label);
    rowsByLabel.set(label, row);
  });
  if (duplicates.size) {
    throw new Error(`${analysis.name} has duplicate comparison labels: ${[...duplicates].join(", ")}.`);
  }
  return rowsByLabel;
}

function renderComparisonGroupedGraphs(rows) {
  const shouldShow = els.enableGroupedGraphs.checked && state.mode === "comparison" && rows.length > 0;
  els.comparisonGroupPanel.hidden = !shouldShow;
  if (!shouldShow) return;

  const groups = buildComparisonGroups(rows);
  renderComparisonCustomGroupingPanel(rows);
  renderComparisonGroupSummary(groups);
  renderComparisonMiniCharts(groups);
}

function buildComparisonGroups(rows) {
  const mode = els.groupMode.value;
  const size = clampInteger(Number(els.groupSize.value), 2, 24, 2);
  if (mode === "blocks") return buildBlockGroups(rows, size);
  if (mode === "custom") return buildComparisonCustomGroups(rows);
  return buildInterleavedGroups(rows, size, (row) => row.label);
}

function buildComparisonCustomGroups(rows) {
  return buildSelectedGroups(state.comparisonCustomGroups, rows, comparisonRowKey);
}

function renderComparisonGroupSummary(groups) {
  els.comparisonGroupSummary.innerHTML = groups
    .map((group) => {
      const items = group.rows.map((row) => `<li>${escapeHtml(row.label)}</li>`).join("");
      return `<div class="group-box"><h3>${escapeHtml(group.name)}</h3><ul>${items}</ul></div>`;
    })
    .join("");
}

function renderComparisonMiniCharts(groups) {
  const seriesNames = state.pairedAnalyses.map((analysis) => analysis.name);
  els.comparisonGroupedCharts.innerHTML = groups
    .map(
      (group, index) => `
        <div class="mini-chart">
          <h3>${escapeHtml(group.name)}</h3>
          <button class="ghost-button" type="button" data-download-canvas="comparisonGroupChart${index}" data-download-name="${escapeHtml(filenameSafe(group.name))}.jpg">Export JPG</button>
          <canvas id="comparisonGroupChart${index}" width="720" height="420" aria-label="Grouped comparison graph ${index + 1}"></canvas>
        </div>
      `,
    )
    .join("");

  groups.forEach((group, index) => {
    const canvas = document.querySelector(`#comparisonGroupChart${index}`);
    if (els.comparisonChartType.value === "points") {
      drawAveragePointChart(canvas, group.rows, seriesNames, group.name);
    } else {
      drawGroupedComparisonChart(canvas, group.rows, seriesNames, group.name);
    }
  });
}

function renderComparisonCustomGroupingPanel(rows) {
  const isCustom = els.groupMode.value === "custom" && els.enableGroupedGraphs.checked && state.mode === "comparison";
  els.comparisonCustomGroupingPanel.hidden = !isCustom;
  if (!isCustom) return;
  if (!state.comparisonCustomGroups.length) state.comparisonCustomGroups = createDefaultCustomGroups(rows, comparisonRowKey);

  els.comparisonCustomGroups.innerHTML = state.comparisonCustomGroups
    .map((group, groupIndex) => {
      const selectedKeys = new Set(groupSelectionKeys(group, rows, comparisonRowKey));
      return `
        <div class="custom-group-card">
          <input type="text" value="${escapeHtml(group.name)}" data-comparison-custom-name="${groupIndex}" />
          <div class="lane-picks">
            ${rows
              .map((row, laneIndex) => {
                const rowKey = comparisonRowKey(row, laneIndex);
                return `
                  <label>
                    <input type="checkbox" data-comparison-custom-group="${groupIndex}" data-comparison-custom-lane-key="${escapeHtml(rowKey)}" ${selectedKeys.has(rowKey) ? "checked" : ""} />
                    ${escapeHtml(row.label)}
                  </label>
                `;
              })
              .join("")}
          </div>
        </div>
      `;
    })
    .join("");
}

function updateComparisonCustomGroups(event) {
  const nameInput = event.target.closest("[data-comparison-custom-name]");
  if (nameInput) {
    const groupIndex = Number(nameInput.dataset.comparisonCustomName);
    if (state.comparisonCustomGroups[groupIndex]) state.comparisonCustomGroups[groupIndex].name = nameInput.value;
    renderComparisonChart();
    return;
  }

  const checkbox = event.target.closest("[data-comparison-custom-group]");
  if (!checkbox) return;
  const group = state.comparisonCustomGroups[Number(checkbox.dataset.comparisonCustomGroup)];
  if (!group || !checkbox.dataset.comparisonCustomLaneKey) return;
  updateGroupSelection(group, buildComparisonRows(), comparisonRowKey, checkbox.dataset.comparisonCustomLaneKey, checkbox.checked);
  renderComparisonChart();
}

function addComparisonCustomGroup() {
  state.comparisonCustomGroups.push({ name: `Custom group ${state.comparisonCustomGroups.length + 1}`, indices: [], keys: [] });
  renderComparisonChart();
}

function drawGroupedComparisonChart(canvas, rows, seriesNames, title = "Grouped bar comparison") {
  const ctx = canvas.getContext("2d");
  const padding = { top: 48, right: 32, bottom: 110, left: 72 };
  const plotWidth = canvas.width - padding.left - padding.right;
  const plotHeight = canvas.height - padding.top - padding.bottom;
  const allValues = rows.flatMap((row) => row.values);
  const maxValue = Math.max(1.2, ...allValues) * 1.16;
  const colors = chartSeriesColors();

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--surface");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGrid(ctx, canvas, padding, plotHeight, maxValue);

  const band = plotWidth / Math.max(rows.length, 1);
  const groupWidth = band * 0.7;
  const barWidth = groupWidth / Math.max(seriesNames.length, 1);
  rows.forEach((row, rowIndex) => {
    const averageValue = average(row.values);
    const highestValue = Math.max(...row.values);
    row.values.forEach((value, seriesIndex) => {
      const x = padding.left + band * rowIndex + (band - groupWidth) / 2 + barWidth * seriesIndex;
      const h = (value / maxValue) * plotHeight;
      const y = padding.top + plotHeight - h;
      ctx.fillStyle = colors[seriesIndex % colors.length];
      ctx.fillRect(x, y, Math.max(8, barWidth - 3), h);
    });
    const labelX = padding.left + band * rowIndex + band / 2;
    const labelY = padding.top + plotHeight - (highestValue / maxValue) * plotHeight - 14;
    drawAverageLabel(ctx, averageValue, labelX, labelY);
    drawAngledLabel(ctx, row.label, padding.left + band * rowIndex + band / 2, padding.top + plotHeight + 18);
  });

  drawLegend(ctx, seriesNames, colors, padding.left, 18);
  drawAxes(ctx, canvas, padding, plotHeight);
  drawTitle(ctx, canvas, title);
}

function drawAverageLabel(ctx, value, x, y) {
  ctx.fillStyle = themeToken("--text");
  ctx.font = themeFont(600, 12, "--font-mono");
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.fillText(`Avg ${value.toFixed(2)}`, x, y);
}

function drawAveragePointChart(canvas, rows, seriesNames, title = "Average with individual points") {
  const ctx = canvas.getContext("2d");
  const padding = { top: 48, right: 32, bottom: 110, left: 72 };
  const plotWidth = canvas.width - padding.left - padding.right;
  const plotHeight = canvas.height - padding.top - padding.bottom;
  const averages = rows.map((row) => average(row.values));
  const maxValue = Math.max(1.2, ...rows.flatMap((row) => row.values), ...averages) * 1.16;
  const colors = chartSeriesColors();

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--surface");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGrid(ctx, canvas, padding, plotHeight, maxValue);

  const band = plotWidth / Math.max(rows.length, 1);
  const barWidth = Math.min(62, band * 0.44);
  rows.forEach((row, index) => {
    const x = padding.left + band * index + (band - barWidth) / 2;
    const avg = averages[index];
    const h = (avg / maxValue) * plotHeight;
    const y = padding.top + plotHeight - h;
    ctx.fillStyle = themeToken("--chart-average");
    ctx.fillRect(x, y, barWidth, h);
    row.values.forEach((value, valueIndex) => {
      const px = x + (barWidth / Math.max(row.values.length - 1, 1)) * valueIndex;
      const py = padding.top + plotHeight - (value / maxValue) * plotHeight;
      ctx.fillStyle = colors[valueIndex % colors.length];
      ctx.beginPath();
      ctx.arc(px, py, 6, 0, Math.PI * 2);
      ctx.fill();
    });
    drawValueLabel(ctx, avg, x + barWidth / 2, y - 12);
    drawAngledLabel(ctx, row.label, padding.left + band * index + band / 2, padding.top + plotHeight + 18);
  });

  drawLegend(ctx, seriesNames, colors, padding.left, 18);
  drawAxes(ctx, canvas, padding, plotHeight);
  drawTitle(ctx, canvas, title);
}

function drawLegend(ctx, names, colors, x, y) {
  names.forEach((name, index) => {
    const offset = index * 150;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x + offset, y, 14, 14);
    ctx.fillStyle = themeToken("--text");
    ctx.font = themeFont(600, 12);
    ctx.textAlign = "left";
    ctx.fillText(name, x + offset + 20, y + 12);
  });
}

function average(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function downloadSharedCsv() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  const lines = [
    "Lane,Sample %,Control %,Fold change",
    ...analysis.results.map((row) =>
      [csvCell(row.displayLane || row.lane), formatNumber(row.samplePercent), formatNumber(row.controlPercent), formatNumber(row.foldChange)].join(","),
    ),
  ];
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${filenameSafe(analysis.name)}-fold-change.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

function downloadSharedWorkbook() {
  if (!state.sharedAnalyses.length || !ensureWorkbookExporter()) return;
  if (!ensureWorkbookRowBudget(estimateWorkbookRows(state.sharedAnalyses))) return;
  try {
    const workbook = XLSX.utils.book_new();
    const usedSheetNames = new Set();
    appendWorkbookSheet(workbook, usedSheetNames, "Summary", sharedWorkbookSummaryRows());
    appendWorkbookSheet(workbook, usedSheetNames, "Fold changes", sharedWorkbookResultRows());
    appendWorkbookSheet(workbook, usedSheetNames, "Input signals", sharedWorkbookInputRows());

    const groupRows = sharedWorkbookGroupRows();
    if (groupRows.length) appendWorkbookSheet(workbook, usedSheetNames, "Grouped values", groupRows);

    appendScanSheets(workbook, usedSheetNames, scanRefsForAnalyses(state.sharedAnalyses));
    downloadWorkbookFile(workbook, "western-blot-shared-analysis.xlsx");
  } catch (error) {
    alert(`Workbook export failed: ${error.message}`);
  }
}

function downloadComparisonWorkbook() {
  if (!state.pairedAnalyses.length || !ensureWorkbookExporter()) return;
  if (!ensureWorkbookRowBudget(estimateWorkbookRows(state.pairedAnalyses))) return;
  try {
    const workbook = XLSX.utils.book_new();
    const usedSheetNames = new Set();
    appendWorkbookSheet(workbook, usedSheetNames, "Summary", comparisonWorkbookSummaryRows());
    appendWorkbookSheet(workbook, usedSheetNames, "Common lanes", comparisonWorkbookCommonRows());
    appendWorkbookSheet(workbook, usedSheetNames, "Pair fold changes", comparisonWorkbookPairRows());
    appendWorkbookSheet(workbook, usedSheetNames, "Input signals", comparisonWorkbookInputRows());

    const groupRows = comparisonWorkbookGroupRows();
    if (groupRows.length) appendWorkbookSheet(workbook, usedSheetNames, "Grouped values", groupRows);

    appendScanSheets(workbook, usedSheetNames, scanRefsForAnalyses(state.pairedAnalyses));
    downloadWorkbookFile(workbook, "western-blot-comparison-analysis.xlsx");
  } catch (error) {
    alert(`Workbook export failed: ${error.message}`);
  }
}

function downloadScanAuditWorkbook(blotId, scanIndex) {
  if (!ensureWorkbookExporter()) return;
  const blot = blotById(blotId);
  const scan = scansForBlot(blotId)[scanIndex];
  if (!blot || !scan) return;

  try {
    const workbook = XLSX.utils.book_new();
    const usedSheetNames = new Set();
    appendWorkbookSheet(workbook, usedSheetNames, "Scan metadata", scanMetadataRows({ blotId, scanId: scan.id }));
    appendWorkbookSheet(workbook, usedSheetNames, "Extraction audit", scanAuditRows({ blotId, scanId: scan.id }));
    downloadWorkbookFile(workbook, `${filenameSafe(blot.name)}-${filenameSafe(scan.proteinName)}-audit.xlsx`);
  } catch (error) {
    alert(`Audit export failed: ${error.message}`);
  }
}

function ensureWorkbookExporter() {
  if (window.XLSX?.utils?.book_new && window.XLSX?.writeFile) return true;
  alert("The Excel exporter has not loaded yet. Check the network connection and retry.");
  return false;
}

function ensureWorkbookRowBudget(rowCount) {
  if (rowCount <= MAX_WORKBOOK_EXPORT_ROWS) return true;
  alert(`Workbook export is too large (${rowCount.toLocaleString()} rows). Reduce inputs or export smaller pieces before trying again.`);
  return false;
}

function estimateWorkbookRows(analyses) {
  const resultRows = analyses.reduce((total, analysis) => total + analysis.results.length, 0);
  const inputRows = analyses.reduce(
    (total, analysis) => total + (analysis.sources || []).reduce((sourceTotal, source) => sourceTotal + source.rows.length, 0),
    0,
  );
  const scanAuditRowsCount = scanAuditRows({ refs: scanRefsForAnalyses(analyses) }).length;
  return resultRows + inputRows + scanAuditRowsCount + 32;
}

function appendWorkbookSheet(workbook, usedSheetNames, name, rows) {
  const safeRows = sanitizeWorkbookRows(rows.length ? rows : [{ Note: "No data" }]);
  const worksheet = XLSX.utils.json_to_sheet(safeRows);
  XLSX.utils.book_append_sheet(workbook, worksheet, uniqueSheetName(name, usedSheetNames));
}

function uniqueSheetName(name, usedSheetNames) {
  const base = String(name || "Sheet")
    .replace(/[\[\]*?/\\:]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 31) || "Sheet";
  let candidate = base;
  let index = 2;
  while (usedSheetNames.has(candidate)) {
    const suffix = ` ${index}`;
    candidate = `${base.slice(0, 31 - suffix.length)}${suffix}`;
    index += 1;
  }
  usedSheetNames.add(candidate);
  return candidate;
}

function sanitizeWorkbookRows(rows) {
  return rows.map((row) =>
    Object.fromEntries(
      Object.entries(row).map(([key, value]) => [key, workbookCell(value)]),
    ),
  );
}

function workbookCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" && /^\s*[=+\-@\t\r]/.test(value)) return `'${value}`;
  return value;
}

function downloadWorkbookFile(workbook, filename) {
  const safeName = filenameSafe(filename).replace(/\.xlsx$/i, "");
  XLSX.writeFile(workbook, `${safeName}.xlsx`);
}

function sharedWorkbookSummaryRows() {
  const normalizationLanes = [...new Set(state.sharedAnalyses.map((analysis) => analysis.normalizationLane).filter(Boolean))];
  const loadingControlModes = [...new Set(state.sharedAnalyses.map((analysis) =>
    analysis.loadingControlAdjusted ? "Applied" : "Skipped",
  ))];
  return [
    { Setting: "Generated at", Value: new Date().toISOString() },
    { Setting: "Mode", Value: "Multiple samples, optional control" },
    { Setting: "Normalization lane", Value: normalizationLanes.join(", ") },
    { Setting: "Loading control adjustment", Value: loadingControlModes.join(", ") },
    { Setting: "Analyses", Value: state.sharedAnalyses.length },
    { Setting: "Active analysis", Value: activeAnalysis()?.name || "" },
    { Setting: "Grouped graphs", Value: els.enableGroupedGraphs.checked ? "Enabled" : "Disabled" },
    { Setting: "Grouping mode", Value: els.groupMode.value },
    { Setting: "Group size", Value: Number(els.groupSize.value) },
  ];
}

function comparisonWorkbookSummaryRows() {
  const normalizationLanes = [...new Set(state.pairedAnalyses.map((analysis) => analysis.normalizationLane).filter(Boolean))];
  return [
    { Setting: "Generated at", Value: new Date().toISOString() },
    { Setting: "Mode", Value: "Compare sample/control pairs" },
    { Setting: "Normalization lane", Value: normalizationLanes.join(", ") },
    { Setting: "Pairs", Value: state.pairedAnalyses.length },
    { Setting: "Comparison graph", Value: els.comparisonChartType.value },
    { Setting: "Grouped graphs", Value: els.enableGroupedGraphs.checked ? "Enabled" : "Disabled" },
    { Setting: "Grouping mode", Value: els.groupMode.value },
    { Setting: "Group size", Value: Number(els.groupSize.value) },
  ];
}

function sharedWorkbookResultRows() {
  return state.sharedAnalyses.flatMap((analysis) =>
    analysis.results.map((row, index) => ({
      Analysis: analysis.name,
      Title: analysis.title,
      "Normalization lane": analysis.normalizationLane,
      "Lane #": index + 1,
      Lane: row.lane,
      "Graph label": row.displayLane || row.lane,
      "Sample signal": row.sampleSignal,
      "Control signal": row.controlSignal,
      "Sample %": row.samplePercent,
      "Control %": row.controlPercent,
      "Fold change": row.foldChange,
    })),
  );
}

function comparisonWorkbookPairRows() {
  return state.pairedAnalyses.flatMap((analysis, pairIndex) =>
    analysis.results.map((row, laneIndex) => ({
      Pair: analysis.name,
      "Pair #": pairIndex + 1,
      "Normalization lane": analysis.normalizationLane,
      "Lane #": laneIndex + 1,
      Lane: row.lane,
      "Graph label": row.displayLane || row.lane,
      "Sample signal": row.sampleSignal,
      "Control signal": row.controlSignal,
      "Sample %": row.samplePercent,
      "Control %": row.controlPercent,
      "Fold change": row.foldChange,
    })),
  );
}

function comparisonWorkbookCommonRows() {
  const seriesNames = state.pairedAnalyses.map((analysis) => analysis.name);
  return buildComparisonRows().map((row) => {
    const output = {
      Lane: row.label,
      Average: average(row.values),
    };
    row.values.forEach((value, index) => {
      output[seriesNames[index] || `Pair ${index + 1}`] = value;
    });
    return output;
  });
}

function sharedWorkbookInputRows() {
  return workbookInputRowsFromAnalyses(state.sharedAnalyses);
}

function comparisonWorkbookInputRows() {
  return workbookInputRowsFromAnalyses(state.pairedAnalyses);
}

function workbookInputRowsFromAnalyses(analyses) {
  return analyses.flatMap((analysis) =>
    (analysis.sources || []).flatMap((source) =>
      source.rows.map((row) => ({
        Analysis: analysis.name,
        Mode: source.mode,
        Role: source.role,
        Index: source.index,
        Source: source.label,
        "Source type": source.sourceType,
        "Source file": source.fileLabel,
        Sheet: source.sheetName,
        "Blot ID": source.blotId,
        "Scan ID": source.scanId,
        "Row #": row.rowIndex,
        Lane: row.lane,
        "Display lane": row.displayLane,
        Signal: row.signal,
      })),
    ),
  );
}

function sharedWorkbookGroupRows() {
  if (!els.enableGroupedGraphs.checked) return [];
  return state.sharedAnalyses.flatMap((analysis) =>
    buildGroupedRows(analysis).flatMap((group) => {
      const normalizedRows = normalizedGroupRows(group);
      if (!normalizedRows) {
        return [{
          Analysis: analysis.name,
          Group: group.name,
          Error: groupBaselineError(group),
        }];
      }
      return normalizedRows.map((row, index) => ({
        Analysis: analysis.name,
        Group: group.name,
        "Group row #": index + 1,
        Lane: row.lane,
        "Graph label": row.displayLane || row.lane,
        "Normalized fold change": row.foldChange,
      }));
    }),
  );
}

function comparisonWorkbookGroupRows() {
  if (!els.enableGroupedGraphs.checked) return [];
  const seriesNames = state.pairedAnalyses.map((analysis) => analysis.name);
  return buildComparisonGroups(buildComparisonRows()).flatMap((group) =>
    group.rows.map((row, rowIndex) => {
      const output = {
        Group: group.name,
        "Group row #": rowIndex + 1,
        Lane: row.label,
        Average: average(row.values),
      };
      row.values.forEach((value, index) => {
        output[seriesNames[index] || `Pair ${index + 1}`] = value;
      });
      return output;
    }),
  );
}

function appendScanSheets(workbook, usedSheetNames, refs = []) {
  const metadataRows = scanMetadataRows({ refs });
  if (metadataRows.length) appendWorkbookSheet(workbook, usedSheetNames, "Scan metadata", metadataRows);
  const auditRows = scanAuditRows({ refs });
  if (auditRows.length) appendWorkbookSheet(workbook, usedSheetNames, "Scan audit", auditRows);
}

function scanRefsForAnalyses(analyses) {
  const refsByKey = new Map();
  analyses.forEach((analysis) => {
    (analysis.sources || []).forEach((source) => {
      if (source.sourceType !== "blot" || !source.blotId || !source.scanId) return;
      refsByKey.set(`${source.blotId}:${source.scanId}`, {
        blotId: source.blotId,
        scanId: source.scanId,
      });
    });
  });
  return [...refsByKey.values()];
}

function scanRecords(filters = {}) {
  const records = [];
  const hasRefsFilter = Array.isArray(filters.refs);
  const refKeys = new Set(
    hasRefsFilter
      ? filters.refs.map((ref) => `${ref.blotId}:${ref.scanId}`)
      : [],
  );
  if (hasRefsFilter && !refKeys.size) return records;
  blotState.blots.forEach((blot) => {
    if (filters.blotId && blot.id !== filters.blotId) return;
    scansForBlot(blot.id).forEach((scan, scanIndex) => {
      if (filters.scanId && scan.id !== filters.scanId) return;
      if (hasRefsFilter && !refKeys.has(`${blot.id}:${scan.id}`)) return;
      records.push({ blot, scan, scanIndex });
    });
  });
  return records;
}

function scanMetadataRows(filters = {}) {
  return scanRecords(filters).map(({ blot, scan, scanIndex }) => ({
    Blot: blot.name,
    "Blot ID": blot.id,
    "Scan #": scanIndex + 1,
    Scan: scan.proteinName,
    "Scan ID": scan.id,
    "Saved at": scan.createdAt || "",
    Channel: `${scan.channel}nm`,
    Background: scan.backgroundAxis,
    Lanes: scan.lanes.length,
    "Color mode": scan.settings?.colorMode || "",
    "Brightness 700": scan.settings?.brightness700 ?? "",
    "Contrast 700": scan.settings?.contrast700 ?? "",
    "Brightness 800": scan.settings?.brightness800 ?? "",
    "Contrast 800": scan.settings?.contrast800 ?? "",
  }));
}

function scanAuditRows(filters = {}) {
  return scanRecords(filters).flatMap(({ blot, scan, scanIndex }) =>
    scan.lanes.map((lane, laneIndex) => ({
      Blot: blot.name,
      "Blot ID": blot.id,
      "Scan #": scanIndex + 1,
      Scan: scan.proteinName,
      "Scan ID": scan.id,
      "Saved at": scan.createdAt || "",
      Channel: `${scan.channel}nm`,
      Background: scan.backgroundAxis,
      "Lane #": laneIndex + 1,
      Lane: lane.name,
      "Adjusted signal": lane.adjustedSignal ?? lane.signal,
      "Raw signal": lane.rawSignal ?? "",
      "Background signal": lane.backgroundSignal ?? "",
      X: lane.x ?? "",
      Y: lane.y ?? "",
      W: lane.w ?? "",
      H: lane.h ?? "",
      "Area px2": lane.area ?? "",
      "Color mode": scan.settings?.colorMode || "",
      "Brightness 700": scan.settings?.brightness700 ?? "",
      "Contrast 700": scan.settings?.contrast700 ?? "",
      "Brightness 800": scan.settings?.brightness800 ?? "",
      "Contrast 800": scan.settings?.contrast800 ?? "",
    })),
  );
}

function createDefaultCustomGroups(rowsOrCount, keyForRow = analysisRowKey) {
  const rows = Array.isArray(rowsOrCount)
    ? rowsOrCount
    : Array.from({ length: rowsOrCount }, (_, index) => ({ lane: `Lane ${index + 1}` }));
  const buildGroup = (name, parity) => {
    const indices = [];
    const keys = [];
    rows.forEach((row, index) => {
      if (index % 2 !== parity) return;
      indices.push(index);
      keys.push(keyForRow(row, index));
    });
    return { name, indices, keys };
  };
  return [
    buildGroup("Odd lanes", 0),
    buildGroup("Even lanes", 1),
  ].filter((group) => group.indices.length);
}

function showSharedError(message) {
  state.sharedAnalyses = [];
  els.chartTitle.innerHTML = `<span class="error-text">${escapeHtml(message)}</span>`;
  drawEmptyChart(els.foldChart);
  els.downloadChartButton.disabled = true;
  els.downloadCsvButton.disabled = true;
  els.downloadWorkbookButton.disabled = true;
  els.labelPanel.hidden = true;
  els.groupPanel.hidden = true;
  document.getElementById("pptxPanel").hidden = true;
}

function downloadGeneratedChart(event) {
  const button = event.target.closest("[data-download-canvas]");
  if (!button) return;
  const canvas = document.querySelector(`#${CSS.escape(button.dataset.downloadCanvas)}`);
  if (!canvas) return;
  downloadCanvasJpeg(canvas, button.dataset.downloadName || "western-blot-chart.jpg");
}

function downloadCanvasJpeg(canvas, filename) {
  const safeName = filenameSafe(filename);
  const link = document.createElement("a");
  link.href = canvas.toDataURL(JPEG_MIME_TYPE, CHART_JPEG_QUALITY);
  link.download = safeName.endsWith(".jpg") || safeName.endsWith(".jpeg") ? safeName : `${safeName}.jpg`;
  link.click();
}

function currentSharedChartFilename() {
  const analysis = activeAnalysis();
  return `${filenameSafe(analysis?.name || "western-blot")}-fold-change.jpg`;
}

function normalizeLaneName(value, index) {
  const text = String(value ?? "").trim();
  return text || `Lane ${index + 1}`;
}

function parseSignal(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : NaN;
  const cleaned = String(value ?? "").replace(/,/g, "").trim();
  if (cleaned === "") return NaN;
  if (!SIGNAL_NUMBER_PATTERN.test(String(value ?? "").trim())) return NaN;
  const number = Number(cleaned);
  return Number.isFinite(number) ? number : NaN;
}

function finiteNumberOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatRowList(rows) {
  const visibleRows = rows.slice(0, 8).join(", ");
  return rows.length > 8 ? `${visibleRows}, and ${rows.length - 8} more` : visibleRows;
}

function clampInteger(value, min, max, fallback) {
  if (!Number.isInteger(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

function clampNumber(value, min, max, fallback) {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatNumber(value) {
  if (!Number.isFinite(value)) return "N/A";
  return value.toLocaleString(undefined, {
    maximumFractionDigits: 3,
    minimumFractionDigits: 0,
  });
}

function formatAuditNumber(value) {
  const number = finiteNumberOrNull(value);
  return number === null ? "N/A" : formatNumber(number);
}

function formatTimestamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toLocaleString();
}

function csvCell(value) {
  let text = String(value ?? "");
  if (/^\s*[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function filenameSafe(value) {
  const cleaned = String(value ?? "western-blot-chart")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || "western-blot-chart";
}
// ─── Main tab switching ────────────────────────────────────────────────────

document.querySelectorAll(".main-tab-button").forEach(button => {
  button.addEventListener("click", () => {
    // Update active tab button
    document.querySelectorAll(".main-tab-button").forEach(b => b.classList.remove("active"));
    button.classList.add("active");

    // Show correct content panel
    const tab = button.dataset.mainTab;
    document.getElementById("tabQuantification").hidden = tab !== "quantification";
    document.getElementById("tabBlotBrowser").hidden = tab !== "blot-browser";
  });
});

// ─── Blot browser ─────────────────────────────────────────────────────────

function createCanvasState() {
  return {
    image: null,
    imageObjectUrl: null,
    boxes: [],
    zoom: 1,
    panX: 0,
    panY: 0,
    mode: "pan",
    isPanning: false,
    isDrawing: false,
    startX: 0,
    startY: 0,
    lastPanX: 0,
    lastPanY: 0,
    selectedBoxIndex: null,
    currentBlotId: null,
    imageWidth: 0,
    imageHeight: 0,
  };
}

let canvasState = createCanvasState();
let canvasReloadTimer = null;
let canvasImageController = null;
let canvasImageRequestId = 0;
const signalExtractionControllers = new Set();
const blotListElement = document.querySelector("#blotList");
const blotScrollUpButton = document.querySelector("#blotScrollUp");
const blotScrollDownButton = document.querySelector("#blotScrollDown");

document.querySelector("#zipFileInput")?.addEventListener("change", async (event) => {
  const files = Array.from(event.target.files);
  for (const file of files) {
    await uploadZip(file);
  }
  event.target.value = "";
});

document.querySelector("#blotList")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-blot-index]");
  if (!button) return;
  selectBlot(Number(button.dataset.blotIndex));
});

document.querySelector("#blotList")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-blot-delete]");
  if (!button) return;
  deleteBlot(Number(button.dataset.blotDelete), button);
});

blotScrollUpButton?.addEventListener("click", () => scrollBlotList(-1));
blotScrollDownButton?.addEventListener("click", () => scrollBlotList(1));
blotListElement?.addEventListener("scroll", updateBlotScrollButtons);
if (window.ResizeObserver && blotListElement) {
  new ResizeObserver(updateBlotScrollButtons).observe(blotListElement);
}
document.querySelectorAll("[data-blot-analysis-tab]").forEach(button => {
  button.addEventListener("click", () => setBlotAnalysisTab(button.dataset.blotAnalysisTab));
});

function clientId(prefix) {
  return `${prefix}-${window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function normalizeBlot(blot) {
  if (!blot || typeof blot !== "object") {
    throw new Error("ZIP processing returned invalid blot data.");
  }
  const id = typeof blot.id === "string" ? blot.id.trim() : "";
  if (!id) throw new Error("ZIP processing returned a blot without an id.");
  const name = String(blot.name ?? id).trim() || "Untitled blot";
  const files = blot.files && typeof blot.files === "object" && !Array.isArray(blot.files)
    ? blot.files
    : {};
  const createdAt = typeof blot.createdAt === "string" ? blot.createdAt : "";
  return { ...blot, id, name, files, createdAt };
}

function mergeBlots(blots) {
  if (!Array.isArray(blots)) throw new Error("ZIP processing returned invalid blot data.");
  const activeBlotId = blotState.blots[blotState.activeBlotIndex]?.id;
  blots.map(normalizeBlot).forEach((blot) => {
    const existingIndex = blotState.blots.findIndex((candidate) => candidate.id === blot.id);
    if (existingIndex >= 0) {
      blotState.blots[existingIndex] = { ...blotState.blots[existingIndex], ...blot };
    } else {
      blotState.blots.push(blot);
    }
    if (!blotState.scans[blot.id]) blotState.scans[blot.id] = [];
  });
  sortBlotsByCreatedAt();
  const activeIndex = activeBlotId
    ? blotState.blots.findIndex((blot) => blot.id === activeBlotId)
    : -1;
  blotState.activeBlotIndex = activeIndex >= 0 ? activeIndex : null;
}

function sortBlotsByCreatedAt() {
  blotState.blots.sort((left, right) => {
    const leftTime = Date.parse(left.createdAt || "");
    const rightTime = Date.parse(right.createdAt || "");
    const safeLeftTime = Number.isFinite(leftTime) ? leftTime : Number.POSITIVE_INFINITY;
    const safeRightTime = Number.isFinite(rightTime) ? rightTime : Number.POSITIVE_INFINITY;
    return safeLeftTime - safeRightTime
      || String(left.name || "").localeCompare(String(right.name || ""))
      || String(left.id || "").localeCompare(String(right.id || ""));
  });
}

function renderBlotList() {
  const container = blotListElement;
  if (!container) return;

  if (!blotState.blots.length) {
    container.innerHTML = `<p class="blot-empty-state">No blots loaded yet.</p>`;
    requestAnimationFrame(updateBlotScrollButtons);
    return;
  }

  container.innerHTML = blotState.blots
    .map((blot, index) => `
      <div class="blot-list-row">
        <button class="blot-list-item ${index === blotState.activeBlotIndex ? "active" : ""}"
          type="button"
          data-blot-index="${index}">
          ${escapeHtml(blot.name)}
        </button>
        <button class="blot-delete-button"
          type="button"
          data-blot-delete="${index}"
          aria-label="Delete ${escapeHtml(blot.name)}"
          title="Delete blot">
          Delete
        </button>
      </div>
    `)
    .join("");
  requestAnimationFrame(() => {
    ensureActiveBlotVisible();
    updateBlotScrollButtons();
  });
}

function blotListScrollAmount() {
  if (!blotListElement) return 320;
  return Math.max(280, Math.round(blotListElement.clientWidth * 0.72));
}

function scrollBlotList(direction) {
  if (!blotListElement) return;
  blotListElement.scrollTo({
    left: blotListElement.scrollLeft + direction * blotListScrollAmount(),
    behavior: "smooth",
  });
}

function updateBlotScrollButtons() {
  if (!blotListElement || !blotScrollUpButton || !blotScrollDownButton) return;
  const maxScrollLeft = Math.max(0, blotListElement.scrollWidth - blotListElement.clientWidth);
  blotScrollUpButton.disabled = blotListElement.scrollLeft <= 1;
  blotScrollDownButton.disabled = blotListElement.scrollLeft >= maxScrollLeft - 1;
}

function ensureActiveBlotVisible() {
  if (!blotListElement || blotState.activeBlotIndex === null) return;
  const activeButton = blotListElement.querySelector(`[data-blot-index="${blotState.activeBlotIndex}"]`);
  const activeRow = activeButton?.closest(".blot-list-row");
  if (!activeRow) return;

  const rowLeft = activeRow.offsetLeft;
  const rowRight = rowLeft + activeRow.offsetWidth;
  const visibleLeft = blotListElement.scrollLeft;
  const visibleRight = visibleLeft + blotListElement.clientWidth;
  if (rowLeft < visibleLeft) {
    blotListElement.scrollTo({ left: rowLeft, behavior: "smooth" });
  } else if (rowRight > visibleRight) {
    blotListElement.scrollTo({ left: rowRight - blotListElement.clientWidth, behavior: "smooth" });
  }
}

async function deleteBlot(index, button) {
  const blot = blotState.blots[index];
  if (!blot || !window.confirm(`Remove “${blot.name}” and its session scans? This cannot be undone.`)) return;

  button.disabled = true;
  button.textContent = "Deleting…";
  void cleanupBlots([blot]);

  const deletionIndex = blotState.blots.findIndex((candidate) => candidate.id === blot.id);
  if (deletionIndex < 0) return;
  const wasActive = blotState.blots[blotState.activeBlotIndex]?.id === blot.id;
  const activeBlotId = wasActive ? null : blotState.blots[blotState.activeBlotIndex]?.id;

  blotState.blots.splice(deletionIndex, 1);
  delete blotState.scans[blot.id];
  delete blotState.scanById[blot.id];
  const activeIndex = activeBlotId
    ? blotState.blots.findIndex((candidate) => candidate.id === activeBlotId)
    : -1;
  blotState.activeBlotIndex = activeIndex >= 0 ? activeIndex : null;

  if (wasActive) {
    disposeCanvasResources();
    const preview = document.querySelector("#blotPreview");
    if (preview) preview.innerHTML = '<p class="blot-empty-state">Select a blot to preview</p>';
    renderBlotAnalysisEmpty("Select a blot to adjust channels and draw boxes.");
  }

  renderBlotList();
  refreshBlotDependentControls();
  setZipUploadStatus(`${blot.name} deleted.`, true);

  if (wasActive && blotState.blots.length) {
    await selectBlot(Math.min(deletionIndex, blotState.blots.length - 1));
  }
}

function switchSource(button, role, index, mode) {
  invalidateAnalyses();
  const group = button.dataset.sourceGroup;
  document.querySelectorAll(`[data-source-group="${group}"]`).forEach((sourceButton) => {
    sourceButton.classList.remove("active");
  });
  button.classList.add("active");

  document.getElementById(sourcePanelId(role, "file", index)).hidden = mode === "blot";
  document.getElementById(sourcePanelId(role, "blot", index)).hidden = mode === "file";

  if (mode === "blot") {
    refreshBlotSourceDropdowns();
  } else {
    refreshNormalizationLanes();
  }
  renderWorkflowState();
}

function handleDocumentClick(event) {
  const sourceButton = event.target.closest("[data-source-role]");
  if (sourceButton) {
    switchSource(
      sourceButton,
      sourceButton.dataset.sourceRole,
      Number(sourceButton.dataset.sourceIndex),
      sourceButton.dataset.sourceMode,
    );
    return;
  }

  const boxButton = event.target.closest("[data-box-action]");
  if (boxButton) {
    const index = Number(boxButton.dataset.boxIndex);
    if (boxButton.dataset.boxAction === "move") {
      moveBox(index, Number(boxButton.dataset.boxDirection), canvasState.currentBlotId);
    } else {
      deleteBox(index, canvasState.currentBlotId);
    }
    return;
  }

  const boxSelect = event.target.closest("[data-box-select]");
  if (boxSelect) {
    selectBox(Number(boxSelect.dataset.boxSelect), canvasState.currentBlotId);
    return;
  }

  const scanAuditExport = event.target.closest("[data-scan-audit-export]");
  if (scanAuditExport) {
    downloadScanAuditWorkbook(canvasState.currentBlotId, Number(scanAuditExport.dataset.scanAuditExport));
    return;
  }

  const scanDelete = event.target.closest("[data-scan-delete]");
  if (scanDelete) {
    deleteScan(canvasState.currentBlotId, Number(scanDelete.dataset.scanDelete));
  }
}

function handleDocumentChange(event) {
  const blotSelect = event.target.closest("[data-refresh-scan-role]");
  if (blotSelect) {
    invalidateAnalyses();
    refreshScanDropdown(blotSelect.dataset.refreshScanRole, Number(blotSelect.dataset.refreshScanIndex));
    renderWorkflowState();
    return;
  }

  if (event.target.closest("[data-blot-source-scan]")) {
    invalidateAnalyses();
    refreshNormalizationLanes();
    renderWorkflowState();
  }
}

function refreshBlotSourceDropdowns() {
  const blotsWithScans = blotState.blots.filter((blot) => scansForBlot(blot.id).length > 0);
  document.querySelectorAll("[data-blot-source-blot]").forEach(select => {
    const currentValue = select.value;
    select.innerHTML = `<option value="">-- Select blot --</option>` +
      blotsWithScans
        .map(blot => `<option value="${escapeHtml(blot.id)}" ${blot.id === currentValue ? "selected" : ""}>${escapeHtml(blot.name)}</option>`)
        .join("");
  });
}

function normalizeScan(scan) {
  if (!scan || typeof scan !== "object") return null;
  const lanes = Array.isArray(scan.lanes)
    ? scan.lanes.map((lane, index) => {
      if (!lane || typeof lane !== "object") return null;
      const signal = parseSignal(lane.signal ?? lane.adjustedSignal);
      if (!Number.isFinite(signal)) return null;
      const x = finiteNumberOrNull(lane.x);
      const y = finiteNumberOrNull(lane.y);
      const w = finiteNumberOrNull(lane.w);
      const h = finiteNumberOrNull(lane.h);
      const area = finiteNumberOrNull(lane.area) ?? (Number.isFinite(w) && Number.isFinite(h) ? Math.round(w * h) : null);
      return {
        name: normalizeLaneName(lane.name, index),
        signal,
        adjustedSignal: signal,
        rawSignal: finiteNumberOrNull(lane.rawSignal),
        backgroundSignal: finiteNumberOrNull(lane.backgroundSignal),
        x,
        y,
        w,
        h,
        area,
      };
    }).filter(Boolean)
    : [];
  if (!lanes.length) return null;
  const id = typeof scan.id === "string" && scan.id.trim() ? scan.id.trim() : clientId("scan");
  const channel = ["700", "800"].includes(String(scan.channel)) ? String(scan.channel) : "700";
  const backgroundAxis = ["leftright", "topbottom"].includes(String(scan.backgroundAxis)) ? String(scan.backgroundAxis) : "leftright";
  const createdAt = typeof scan.createdAt === "string" ? scan.createdAt : "";
  const settings = scan.settings && typeof scan.settings === "object" && !Array.isArray(scan.settings)
    ? scan.settings
    : {};
  return {
    ...scan,
    id,
    proteinName: String(scan.proteinName ?? "Untitled scan").trim() || "Untitled scan",
    channel,
    backgroundAxis,
    createdAt,
    settings,
    lanes,
  };
}

function scansForBlot(blotId) {
  const existing = blotState.scans[blotId];
  if (Array.isArray(existing) && blotState.scanById[blotId]) return existing;

  const scans = Array.isArray(existing) ? existing : [];
  const normalized = scans.map(normalizeScan).filter(Boolean);
  if (!Array.isArray(existing) || normalized.length !== scans.length || normalized.some((scan, index) => scan !== scans[index]) || !blotState.scanById[blotId]) {
    blotState.scans[blotId] = normalized;
    indexScansForBlot(blotId);
  }
  return blotState.scans[blotId];
}

function setScansForBlot(blotId, scans) {
  if (!blotState.blots.some((blot) => blot.id === blotId) || !Array.isArray(scans)) return;
  blotState.scans[blotId] = scans.map(normalizeScan).filter(Boolean);
  indexScansForBlot(blotId);
}

function indexScansForBlot(blotId) {
  const scans = Array.isArray(blotState.scans[blotId]) ? blotState.scans[blotId] : [];
  blotState.scanById[blotId] = new Map(scans.map((scan) => [scan.id, scan]));
}

function findScanByRef(blotId, scanRef) {
  const ref = typeof scanRef === "string" ? scanRef.trim() : "";
  if (!ref) return null;
  if (!blotState.scanById[blotId]) scansForBlot(blotId);
  return blotState.scanById[blotId]?.get(ref) || null;
}

function refreshAllScanDropdowns() {
  document.querySelectorAll("[data-refresh-scan-role]").forEach(select => {
    refreshScanDropdown(select.dataset.refreshScanRole, Number(select.dataset.refreshScanIndex));
  });
}

function refreshBlotDependentControls() {
  refreshBlotSourceDropdowns();
  refreshAllScanDropdowns();
  renderWorkflowState();
}

function refreshScanDropdown(role, index) {
  const blotSelect = document.querySelector(`[data-blot-source-blot="${role}-${index}"]`);
  const scanSelect = document.querySelector(`[data-blot-source-scan="${role}-${index}"]`);
  if (!blotSelect || !scanSelect) return;

  const blotId = blotSelect.value;
  if (!blotId) {
    scanSelect.innerHTML = `<option value="">-- Select scan --</option>`;
    refreshNormalizationLanes();
    return;
  }
  const scans = scansForBlot(blotId);
  const currentValue = scanSelect.value;

  scanSelect.innerHTML = `<option value="">-- Select scan --</option>` +
    scans.map((scan) => `
      <option value="${escapeHtml(scan.id)}">${escapeHtml(scan.proteinName)} (${scan.lanes.length} lanes)</option>
    `).join("");

  if (currentValue && scans.some((scan) => scan.id === currentValue)) {
    scanSelect.value = currentValue;
  }
  refreshNormalizationLanes();
}

async function uploadZip(file) {
  const maxBytes = Number(CONFIG.MAX_ZIP_UPLOAD_BYTES || DEFAULT_ZIP_UPLOAD_BYTES);
  const mimeType = String(file.type || "").toLowerCase();
  if (!file.name.toLowerCase().endsWith(".zip") || !ALLOWED_ZIP_MIME_TYPES.has(mimeType)) {
    alert("Please select a ZIP file.");
    return;
  }
  if (file.size > maxBytes) {
    alert(`ZIP is too large. Maximum size is ${Math.floor(maxBytes / 1024 / 1024)} MB.`);
    return;
  }

  try {
    setZipUploadStatus(`Processing ${file.name}...`);
    const data = await processZipFile(file);

    mergeBlots(data.blots || []);
    if (data.scans) {
      Object.entries(data.scans).forEach(([blotId, scans]) => {
        setScansForBlot(blotId, scans);
      });
    }
    renderBlotList();
    refreshBlotDependentControls();
    setZipUploadStatus(`${file.name} imported.`, true);
  } catch (error) {
    setZipUploadStatus("");
    alert(`Failed to load ZIP: ${error.message}`);
  }
}

async function processZipFile(file) {
  if (CONFIG.USE_VERCEL_BLOB_UPLOADS) {
    setZipUploadStatus(`Uploading ${file.name}...`);
    const blob = await uploadZipToVercelBlob(file);
    setZipUploadStatus(`Processing ${file.name}...`);
    return apiJson(apiUrl("/process-upload"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: activeSessionId, upload: blob }),
      timeoutMs: ZIP_PROCESSING_TIMEOUT_MS,
    }, "ZIP processing failed.");
  }

  const form = new FormData();
  form.append("sessionId", activeSessionId);
  form.append("file", file);
  return apiJson(apiUrl("/upload-zip"), {
    method: "POST",
    body: form,
    timeoutMs: ZIP_PROCESSING_TIMEOUT_MS,
  }, "ZIP processing failed.");
}

async function uploadZipToVercelBlob(file) {
  const [config, uploadStatus] = await Promise.all([
    runtimeConfig(),
    blobUploadStatus(),
  ]);
  const uploadId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const configuredAccess = uploadStatus.blobAccess || config.blobAccess || CONFIG.BLOB_ACCESS;
  const blobAccess = configuredAccess === "public" ? "public" : "private";
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), BLOB_UPLOAD_TIMEOUT_MS);

  try {
    return await manualPresignedUpload(`uploads/${activeSessionId}/${uploadId}.zip`, file, {
      access: blobAccess,
      signal: controller.signal,
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error("Blob upload timed out before Vercel returned a stored file reference.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function manualPresignedUpload(pathname, file, options) {
  const contentType = file.type || "application/octet-stream";
  const presign = await apiJson(apiUrl("/blob-upload"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: "blob.generate-presigned-url",
      payload: {
        pathname,
        clientPayload: JSON.stringify({ sessionId: activeSessionId }),
        multipart: false,
      },
    }),
    timeoutMs: SETUP_API_TIMEOUT_MS,
  }, "Could not prepare Blob upload.");

  const payload = presign.presignedUrlPayload;
  if (!payload?.delegationToken || !payload?.signature || !payload?.params) {
    throw new Error("Blob upload endpoint returned an invalid presigned URL payload.");
  }

  const uploadUrl = buildPresignedBlobUrl(pathname, payload);
  const storeId = parseBlobStoreIdFromDelegationToken(payload.delegationToken);
  const response = await uploadFileWithProgress(uploadUrl, file, {
    access: options.access,
    contentType,
    signal: options.signal,
    storeId,
  });

  return {
    url: response.url,
    downloadUrl: response.downloadUrl,
    pathname: response.pathname || pathname,
    contentType: response.contentType || contentType,
    contentDisposition: response.contentDisposition || "",
  };
}

function buildPresignedBlobUrl(pathname, payload) {
  const url = new URL("https://vercel.com/api/blob/");
  url.searchParams.set("pathname", pathname);
  Object.entries(payload.params || {}).forEach(([key, value]) => {
    url.searchParams.set(key, value);
  });
  url.searchParams.set("vercel-blob-delegation", payload.delegationToken);
  url.searchParams.set("vercel-blob-signature", payload.signature);
  return url.href;
}

function parseBlobStoreIdFromDelegationToken(delegationToken) {
  const parts = String(delegationToken || "").split(".");
  if (parts.length < 2) {
    throw new Error("Blob upload delegation token is invalid.");
  }

  let decoded;
  try {
    decoded = JSON.parse(base64UrlDecode(parts[0]));
  } catch (_error) {
    throw new Error("Blob upload delegation token could not be decoded.");
  }

  const storeId = String(decoded.storeId || "");
  if (!storeId) {
    throw new Error("Blob upload delegation token is missing the Blob store id.");
  }
  return storeId.startsWith("store_") ? storeId.slice("store_".length) : storeId;
}

function base64UrlDecode(value) {
  const normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(normalized.length + ((4 - normalized.length % 4) % 4), "=");
  const binary = window.atob(padded);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function uploadFileWithProgress(url, file, options) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;

    function finish(error, value) {
      if (settled) return;
      settled = true;
      options.signal?.removeEventListener("abort", abortUpload);
      if (error) reject(error);
      else resolve(value);
    }

    function abortUpload() {
      xhr.abort();
      finish(new Error("Blob upload was cancelled."));
    }

    xhr.upload.onprogress = event => {
      if (!event.lengthComputable) return;
      const percent = Math.max(0, Math.min(100, Math.floor((event.loaded / event.total) * 100)));
      setZipUploadStatus(`Uploading ${file.name}... ${percent}%`);
    };
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        finish(new Error(`Blob upload failed with status ${xhr.status}.`));
        return;
      }
      try {
        finish(null, JSON.parse(xhr.responseText || "{}"));
      } catch (_error) {
        finish(new Error("Blob upload returned an invalid response."));
      }
    };
    xhr.onerror = () => finish(new Error("Blob upload network request failed."));
    xhr.onabort = () => finish(new Error("Blob upload was cancelled."));

    if (options.signal?.aborted) {
      abortUpload();
      return;
    }
    options.signal?.addEventListener("abort", abortUpload, { once: true });

    xhr.open("PUT", url);
    xhr.setRequestHeader("x-api-version", "12");
    xhr.setRequestHeader("x-content-length", String(file.size));
    xhr.setRequestHeader("x-content-type", options.contentType);
    xhr.setRequestHeader("x-vercel-blob-access", options.access);
    xhr.setRequestHeader("x-vercel-blob-store-id", options.storeId);
    xhr.setRequestHeader("x-api-blob-request-id", `${options.storeId}:${Date.now()}:${Math.random().toString(16).slice(2)}`);
    xhr.setRequestHeader("x-api-blob-request-attempt", "0");
    xhr.send(file);
  });
}

async function blobUploadStatus() {
  const status = await apiJson(apiUrl("/blob-upload"), { timeoutMs: SETUP_API_TIMEOUT_MS, retry: 1 }, "Blob upload endpoint is not reachable.");
  if (!status.hasBlobReadWriteToken) {
    throw new Error("BLOB_READ_WRITE_TOKEN is not configured for this deployment.");
  }
  return status;
}

function setZipUploadStatus(message, success = false) {
  const status = document.querySelector("#zipUploadStatus");
  if (!status) return;
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle("success", success);
}

function cleanupBlots(blots, keepalive = false) {
  const removable = (blots || []).filter((blot) => blot?.files);
  if (!removable.length) return Promise.resolve();
  return apiFetch(apiUrl("/cleanup"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId: activeSessionId, blots: removable }),
    keepalive,
  }).catch((error) => {
    console.warn("Temporary blot cleanup failed.", error);
  });
}

window.addEventListener("pagehide", () => {
  disposeCanvasResources();
  cleanupBlots(blotState.blots, true);
});

async function selectBlot(index) {
  disposeCanvasResources();
  blotState.activeBlotIndex = index;
  renderBlotList();

  const blot = blotState.blots[index];
  const preview = document.querySelector("#blotPreview");
  if (!preview) return;

  preview.innerHTML = `<p class="blot-empty-state">Loading...</p>`;
  renderBlotAnalysisEmpty("Loading blot analysis...");

  try {
    preview.innerHTML = blotViewerHtml();
    renderBlotAnalysis(blot.id);
    initCanvas(blot.id);
    bindBlotViewerControls(blot.id);
  } catch (error) {
    preview.innerHTML = `<p class="blot-empty-state">Failed to load blot: ${escapeHtml(error.message)}</p>`;
    renderBlotAnalysisEmpty(`Failed to load blot: ${error.message}`);
  }
}

function blotViewerHtml() {
  return `
    <div class="blot-viewer">
      <div class="blot-canvas-wrap">
        <canvas id="blotCanvas"></canvas>
      </div>
    </div>
  `;
}

function renderBlotAnalysis(blotId) {
  const toolsPanel = document.getElementById("blotAnalysisTools");
  const boxesPanel = document.getElementById("blotAnalysisBoxes");
  if (toolsPanel) {
    toolsPanel.innerHTML = `
      <div class="blot-controls-bar">
        ${blotModeControlsHtml()}
        ${channelAdjustmentControlsHtml("red", "700")}
        ${channelAdjustmentControlsHtml("green", "800")}
        ${boxToolControlsHtml()}
        ${boxLayoutControlsHtml()}
      </div>
    `;
  }
  if (boxesPanel) {
    boxesPanel.innerHTML = `
      <div class="blot-box-list-wrap">
        <p class="eyebrow">Drawn boxes</p>
        <div id="blotBoxList" class="blot-box-list">
          <p class="blot-empty-state">No boxes drawn yet.</p>
        </div>
      </div>
    `;
  }
  setBlotAnalysisTab(currentBlotAnalysisTab());
  renderBoxList(blotId);
}

function renderBlotAnalysisEmpty(message) {
  const toolsPanel = document.getElementById("blotAnalysisTools");
  const boxesPanel = document.getElementById("blotAnalysisBoxes");
  if (toolsPanel) toolsPanel.innerHTML = `<p class="blot-empty-state">${escapeHtml(message)}</p>`;
  if (boxesPanel) boxesPanel.innerHTML = `<p class="blot-empty-state">Select a blot to view drawn boxes.</p>`;
  setBlotAnalysisTab("analysis");
}

function currentBlotAnalysisTab() {
  return document.querySelector("[data-blot-analysis-tab].active")?.dataset.blotAnalysisTab || "analysis";
}

function setBlotAnalysisTab(tabName) {
  const nextTab = tabName === "boxes" ? "boxes" : "analysis";
  document.querySelectorAll("[data-blot-analysis-tab]").forEach(button => {
    const isActive = button.dataset.blotAnalysisTab === nextTab;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", String(isActive));
  });
  document.querySelectorAll("[data-blot-analysis-panel]").forEach(panel => {
    panel.hidden = panel.dataset.blotAnalysisPanel !== nextTab;
  });
}

function blotModeControlsHtml() {
  return `
    <div class="blot-control-group">
      <label>Color mode
        <select id="colorMode">
          <option value="color">Red / Green</option>
          <option value="grayscale">Black & White</option>
        </select>
      </label>
      <label>Quantify channel
        <select id="quantChannel">
          <option value="700">700nm</option>
          <option value="800">800nm</option>
        </select>
      </label>
      <label>Background
        <select id="backgroundAxis">
          <option value="leftright">Left & Right</option>
          <option value="topbottom">Top & Bottom</option>
        </select>
      </label>
    </div>
  `;
}

function channelAdjustmentControlsHtml(colorClass, channel) {
  return `
    <div class="blot-control-group">
      <span class="channel-label ${colorClass}">${channel}nm</span>
      <label>Brightness
        <input type="range" min="0.1" max="3" step="0.05" value="1" id="brightness${channel}" />
      </label>
      <label>Contrast
        <input type="range" min="0.1" max="5" step="0.1" value="1" id="contrast${channel}" />
      </label>
    </div>
  `;
}

function boxToolControlsHtml() {
  return `
    <div class="blot-control-group">
      <label>Mode
        <div class="mode-toggle">
          <button class="mode-button active" id="modePan" type="button">Pan</button>
          <button class="mode-button" id="modeDraw" type="button">Draw</button>
        </div>
      </label>
      <button class="ghost-button" id="extractSignalsButton" type="button">Refresh signals</button>
      <button class="ghost-button" id="clearBoxesButton" type="button">Clear all</button>
    </div>
  `;
}

function boxLayoutControlsHtml() {
  return `
    <div class="blot-control-group box-layout-tools">
      <span class="channel-label">Box layout</span>
      <p class="box-selected-status" id="selectedBoxStatus">Select a box below.</p>
      <div class="box-tool-row">
        <label>Copies
          <input id="duplicateBoxCount" type="number" min="1" max="48" step="1" value="1" />
        </label>
        <label>Gap px
          <input id="duplicateBoxGap" type="number" min="-2000" max="2000" step="1" value="8" />
        </label>
        <label>Direction
          <select id="duplicateBoxDirection">
            <option value="right">Right</option>
            <option value="left">Left</option>
            <option value="down">Down</option>
            <option value="up">Up</option>
          </select>
        </label>
        <button class="ghost-button box-button" id="duplicateBoxButton" type="button">Duplicate</button>
      </div>
      <div class="box-button-grid" aria-label="Align boxes to selected box">
        <button class="ghost-button box-button" type="button" data-box-align="left">Left</button>
        <button class="ghost-button box-button" type="button" data-box-align="centerX">Center X</button>
        <button class="ghost-button box-button" type="button" data-box-align="right">Right</button>
        <button class="ghost-button box-button" type="button" data-box-align="top">Top</button>
        <button class="ghost-button box-button" type="button" data-box-align="centerY">Center Y</button>
        <button class="ghost-button box-button" type="button" data-box-align="bottom">Bottom</button>
      </div>
      <div class="box-tool-row">
        <button class="ghost-button box-button" id="matchBoxSizeButton" type="button">Match size</button>
        <label>Step px
          <input id="boxNudgeStep" type="number" min="1" max="100" step="1" value="1" />
        </label>
        <div class="box-nudge-grid" aria-label="Nudge selected box">
          <button class="ghost-button box-button" type="button" data-box-nudge="0,-1" aria-label="Nudge box up">&uarr;</button>
          <button class="ghost-button box-button" type="button" data-box-nudge="-1,0" aria-label="Nudge box left">&larr;</button>
          <button class="ghost-button box-button" type="button" data-box-nudge="1,0" aria-label="Nudge box right">&rarr;</button>
          <button class="ghost-button box-button" type="button" data-box-nudge="0,1" aria-label="Nudge box down">&darr;</button>
        </div>
      </div>
    </div>
  `;
}

function bindBlotViewerControls(blotId) {
  ["brightness700", "contrast700", "brightness800", "contrast800", "colorMode"].forEach(id => {
    document.getElementById(id)?.addEventListener("input", () => {
      scheduleCanvasImageReload(blotId);
    });
    document.getElementById(id)?.addEventListener("change", () => {
      scheduleCanvasImageReload(blotId, true);
    });
  });
  document.getElementById("duplicateBoxButton")?.addEventListener("click", () => duplicateSelectedBox(blotId));
  document.getElementById("matchBoxSizeButton")?.addEventListener("click", () => matchBoxesToSelectedSize(blotId));
  document.querySelectorAll("[data-box-align]").forEach(button => {
    button.addEventListener("click", () => alignBoxesToSelected(button.dataset.boxAlign, blotId));
  });
  document.querySelectorAll("[data-box-nudge]").forEach(button => {
    button.addEventListener("click", () => {
      const [dx, dy] = button.dataset.boxNudge.split(",").map(Number);
      nudgeSelectedBox(dx, dy, blotId);
    });
  });
  updateBoxToolState();
}

// ─── Blot canvas engine ───────────────────────────────────────────────────────

function initCanvas(blotId) {
  canvasState.boxes = [];
  canvasState.zoom = 1;
  canvasState.panX = 0;
  canvasState.panY = 0;
  canvasState.mode = "pan";
  canvasState.currentBlotId = blotId;

  const canvas = document.getElementById("blotCanvas");
  if (!canvas) return;

  // Set canvas size to fill its container
  const wrap = canvas.parentElement;
  canvas.width = wrap.clientWidth || 800;
  canvas.height = 520;

  // Load composite image onto canvas
  loadCanvasImage(blotId);

  // Wire up mode buttons
  document.getElementById("modePan")?.addEventListener("click", () => setCanvasMode("pan"));
  document.getElementById("modeDraw")?.addEventListener("click", () => setCanvasMode("draw"));
  document.getElementById("clearBoxesButton")?.addEventListener("click", () => {
    canvasState.boxes = [];
    canvasState.selectedBoxIndex = null;
    renderCanvas();
    renderBoxList(blotId);
  });
  document.getElementById("extractSignalsButton")?.addEventListener("click", () => extractSignals(blotId));
  renderBoxList(blotId);

  // Mouse events
  canvas.addEventListener("mousedown", onMouseDown);
  canvas.addEventListener("mousemove", onMouseMove);
  canvas.addEventListener("mouseup", onMouseUp);
  canvas.addEventListener("mouseleave", onMouseUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
}

function setCanvasMode(mode) {
  canvasState.mode = mode;
  const canvas = document.getElementById("blotCanvas");
  if (canvas) canvas.style.cursor = mode === "draw" ? "crosshair" : "grab";
  document.getElementById("modePan")?.classList.toggle("active", mode === "pan");
  document.getElementById("modeDraw")?.classList.toggle("active", mode === "draw");
}

function cancelPendingCanvasImageLoad() {
  if (canvasReloadTimer !== null) {
    window.clearTimeout(canvasReloadTimer);
    canvasReloadTimer = null;
  }
  if (canvasImageController) {
    canvasImageController.abort();
    canvasImageController = null;
  }
  canvasImageRequestId += 1;
}

function abortSignalExtractions() {
  for (const controller of signalExtractionControllers) {
    controller.abort();
  }
  signalExtractionControllers.clear();
}

function disposeCanvasResources() {
  cancelPendingCanvasImageLoad();
  abortSignalExtractions();
  if (canvasState.imageObjectUrl) {
    URL.revokeObjectURL(canvasState.imageObjectUrl);
  }
  if (canvasState.image) {
    canvasState.image.onload = null;
    canvasState.image.onerror = null;
    canvasState.image.src = "";
  }
  canvasState = createCanvasState();
}

function scheduleCanvasImageReload(blotId, immediate = false) {
  if (canvasReloadTimer !== null) window.clearTimeout(canvasReloadTimer);
  canvasReloadTimer = null;

  if (immediate) {
    loadCanvasImage(blotId, { preserveView: true });
    return;
  }

  canvasReloadTimer = window.setTimeout(() => {
    canvasReloadTimer = null;
    loadCanvasImage(blotId, { preserveView: true });
  }, 200);
}

async function loadCanvasImage(blotId, options = {}) {
  if (canvasImageController) canvasImageController.abort();
  const controller = new AbortController();
  canvasImageController = controller;
  const requestId = ++canvasImageRequestId;
  const previousView = {
    zoom: canvasState.zoom,
    panX: canvasState.panX,
    panY: canvasState.panY,
    imageWidth: canvasState.imageWidth,
    imageHeight: canvasState.imageHeight,
  };
  const img = new Image();
  img.onload = () => {
    if (requestId !== canvasImageRequestId || blotId !== canvasState.currentBlotId) {
      URL.revokeObjectURL(img.src);
      return;
    }
    if (canvasState.imageObjectUrl) URL.revokeObjectURL(canvasState.imageObjectUrl);
    canvasState.imageObjectUrl = img.src;
    canvasState.image = img;
    canvasState.imageWidth = img.naturalWidth;
    canvasState.imageHeight = img.naturalHeight;

    const canvas = document.getElementById("blotCanvas");
    if (!canvas) return;

    const canPreserveView = options.preserveView
      && previousView.imageWidth === img.naturalWidth
      && previousView.imageHeight === img.naturalHeight
      && previousView.zoom > 0;

    if (canPreserveView) {
      canvasState.zoom = previousView.zoom;
      canvasState.panX = previousView.panX;
      canvasState.panY = previousView.panY;
    } else {
      const scale = Math.min(canvas.width / img.naturalWidth, canvas.height / img.naturalHeight) * 0.9;
      canvasState.zoom = scale;
      canvasState.panX = (canvas.width  - img.naturalWidth  * scale) / 2;
      canvasState.panY = (canvas.height - img.naturalHeight * scale) / 2;
    }
    renderCanvas();
  };
  img.onerror = () => {
    URL.revokeObjectURL(img.src);
    if (requestId !== canvasImageRequestId || blotId !== canvasState.currentBlotId) return;
    canvasState.image = null;
    canvasState.imageObjectUrl = null;
    showBlotCanvasError("Could not decode blot image. Try reloading the image.");
  };
  try {
    const blob = await renderCompositeBlob(blotId, "color", controller.signal);
    if (requestId !== canvasImageRequestId || blotId !== canvasState.currentBlotId) return;
    img.src = URL.createObjectURL(blob);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId === canvasImageRequestId && blotId === canvasState.currentBlotId) {
      canvasState.image = null;
      showBlotCanvasError(error.message || "Could not load blot image.");
    }
  } finally {
    if (requestId === canvasImageRequestId) canvasImageController = null;
  }
}

function blotById(blotId) {
  return blotState.blots.find((blot) => blot.id === blotId) || null;
}

// Builds one canonical composite payload for the viewer and exported presentations.
function buildCompositePayload(blotId, defaultColorMode = "color") {
  const brightness700 = document.getElementById("brightness700")?.value ?? 1;
  const contrast700   = document.getElementById("contrast700")?.value   ?? 1;
  const brightness800 = document.getElementById("brightness800")?.value ?? 1;
  const contrast800   = document.getElementById("contrast800")?.value   ?? 1;
  const colorMode     = document.getElementById("colorMode")?.value     ?? defaultColorMode;
  const blot = blotById(blotId);
  if (!blot) throw new Error("Blot is no longer loaded.");
  return {
    sessionId: activeSessionId,
    blot,
    brightness700,
    contrast700,
    brightness800,
    contrast800,
    colorMode,
  };
}

async function renderCompositeBlob(blotId, defaultColorMode = "color", signal) {
  const response = await apiFetch(apiUrl("/render-composite"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildCompositePayload(blotId, defaultColorMode)),
    signal,
    timeoutMs: IMAGE_RENDER_TIMEOUT_MS,
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Could not load blot image."));
  }
  return response.blob();
}

function renderCanvas() {
  const canvas = document.getElementById("blotCanvas");
  if (!canvas) return;
  normalizeSelectedBoxIndex();
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Dark background
  ctx.fillStyle = themeToken("--image-canvas");
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  if (canvasState.image) {
    ctx.save();
    ctx.translate(canvasState.panX, canvasState.panY);
    ctx.scale(canvasState.zoom, canvasState.zoom);
    ctx.drawImage(canvasState.image, 0, 0);

    // Draw boxes in image space
    canvasState.boxes.forEach((box, index) => {
      const isSelected = index === canvasState.selectedBoxIndex;
      ctx.strokeStyle = isSelected
        ? (themeToken("--annotation-selected") || themeToken("--primary"))
        : themeToken("--annotation");
      ctx.lineWidth = (isSelected ? 3 : 2) / canvasState.zoom;
      ctx.strokeRect(box.x, box.y, box.w, box.h);

      // Number label
      ctx.fillStyle = isSelected
        ? (themeToken("--annotation-selected") || themeToken("--primary"))
        : themeToken("--annotation");
      ctx.font = themeFont(600, 14 / canvasState.zoom, "--font-mono");
      ctx.fillText(index + 1, box.x + 4 / canvasState.zoom, box.y + 16 / canvasState.zoom);
    });

    ctx.restore();
  }
}

function showBlotCanvasError(message) {
  const canvas = document.getElementById("blotCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--image-canvas");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = themeToken("--danger");
  ctx.font = themeFont(600, 16);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(message, canvas.width / 2, canvas.height / 2);
}

// ─── Mouse event handlers ─────────────────────────────────────────────────────

function getImageCoords(canvas, clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const canvasX = (clientX - rect.left) * scaleX;
  const canvasY = (clientY - rect.top)  * scaleY;
  return {
    x: (canvasX - canvasState.panX) / canvasState.zoom,
    y: (canvasY - canvasState.panY) / canvasState.zoom,
  };
}

function onMouseDown(event) {
  const canvas = event.target;
  if (canvasState.mode === "pan") {
    canvasState.isPanning = true;
    canvasState.lastPanX = event.clientX;
    canvasState.lastPanY = event.clientY;
    canvas.style.cursor = "grabbing";
  } else if (canvasState.mode === "draw") {
    canvasState.isDrawing = true;
    const coords = getImageCoords(canvas, event.clientX, event.clientY);
    canvasState.startX = coords.x;
    canvasState.startY = coords.y;
  }
}

function onMouseMove(event) {
  const canvas = event.target;
  if (canvasState.isPanning) {
    canvasState.panX += event.clientX - canvasState.lastPanX;
    canvasState.panY += event.clientY - canvasState.lastPanY;
    canvasState.lastPanX = event.clientX;
    canvasState.lastPanY = event.clientY;
    renderCanvas();
  } else if (canvasState.isDrawing) {
    const coords = getImageCoords(canvas, event.clientX, event.clientY);
    // Draw preview box
    renderCanvas();
    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.translate(canvasState.panX, canvasState.panY);
    ctx.scale(canvasState.zoom, canvasState.zoom);
    ctx.strokeStyle = themeToken("--annotation");
    ctx.lineWidth = 2 / canvasState.zoom;
    ctx.setLineDash([4 / canvasState.zoom, 4 / canvasState.zoom]);
    ctx.strokeRect(
      canvasState.startX,
      canvasState.startY,
      coords.x - canvasState.startX,
      coords.y - canvasState.startY
    );
    ctx.restore();
  }
}

function onMouseUp(event) {
  const canvas = document.getElementById("blotCanvas");
  if (canvasState.isPanning) {
    canvasState.isPanning = false;
    if (canvas) canvas.style.cursor = "grab";
  } else if (canvasState.isDrawing) {
    canvasState.isDrawing = false;
    const coords = getImageCoords(canvas, event.clientX, event.clientY);
    const w = coords.x - canvasState.startX;
    const h = coords.y - canvasState.startY;

    // Only reject clicks with no drag at all
    if (Math.abs(w) > 0.5 && Math.abs(h) > 0.5) {
      if (remainingBoxSlots() <= 0) {
        alert(`Maximum ${MAX_CANVAS_BOXES} boxes per blot scan.`);
        return;
      }
      const box = createCanvasBox({
        x: w > 0 ? canvasState.startX : coords.x,
        y: h > 0 ? canvasState.startY : coords.y,
        w: Math.abs(w),
        h: Math.abs(h),
      });
      canvasState.boxes.push(box);
      canvasState.selectedBoxIndex = canvasState.boxes.length - 1;
      renderCanvas();
      void extractSignalsForBoxes(canvasState.currentBlotId, [box], { alertOnError: false });
    }
  }
}

function onWheel(event) {
  event.preventDefault();
  const canvas = document.getElementById("blotCanvas");
  if (!canvas) return;

  const rect = canvas.getBoundingClientRect();
  const mouseX = (event.clientX - rect.left) * (canvas.width / rect.width);
  const mouseY = (event.clientY - rect.top)  * (canvas.height / rect.height);

  const zoomFactor = event.deltaY < 0 ? 1.1 : 0.9;
  const newZoom = Math.min(20, Math.max(0.1, canvasState.zoom * zoomFactor));

  // Zoom toward mouse position
  canvasState.panX = mouseX - (mouseX - canvasState.panX) * (newZoom / canvasState.zoom);
  canvasState.panY = mouseY - (mouseY - canvasState.panY) * (newZoom / canvasState.zoom);
  canvasState.zoom = newZoom;
  renderCanvas();
}

// ─── Box list ─────────────────────────────────────────────────────────────────

function renderBoxList(blotId) {
  const container = document.getElementById("blotBoxList");
  if (!container) return;
  normalizeSelectedBoxIndex();

  if (!canvasState.boxes.length) {
    container.innerHTML = emptyBoxListHtml(blotId);
    updateBoxToolState();
    return;
  }

  container.innerHTML = activeBoxListHtml(blotId);
  bindBoxListInputs(container, blotId);
  updateBoxToolState();
}

function emptyBoxListHtml(blotId) {
  return `
    <p class="blot-empty-state">No boxes drawn yet.</p>
    <div id="savedScansWrap">
      ${renderSavedScans(blotId)}
    </div>
  `;
}

function activeBoxListHtml(blotId) {
  return `
    ${canvasState.boxes.map(boxListItemHtml).join("")}
    ${saveScanBarHtml()}
    <div id="savedScansWrap">
      ${renderSavedScans(blotId)}
    </div>
  `;
}

function boxListItemHtml(box, index) {
  const isSelected = index === canvasState.selectedBoxIndex;
  return `
    <div class="box-list-item ${isSelected ? "selected" : ""}">
      <button class="box-number" type="button" data-box-select="${index}" aria-label="Select box ${index + 1}" aria-pressed="${isSelected}">
        ${index + 1}
      </button>
      <input
        type="text"
        class="box-lane-name"
        value="${escapeHtml(box.laneName || `Lane ${index + 1}`)}"
        data-box-index="${index}"
        placeholder="Lane name"
      />
      <span class="box-area">Area <strong>${formatBoxArea(box).toLocaleString()} px²</strong></span>
      ${boxSignalHtml(box)}
      <div class="box-actions">
        <button class="ghost-button box-button" type="button"
          data-box-action="move" data-box-index="${index}" data-box-direction="-1">Up</button>
        <button class="ghost-button box-button" type="button"
          data-box-action="move" data-box-index="${index}" data-box-direction="1">Down</button>
        <button class="ghost-button box-button danger" type="button"
          data-box-action="delete" data-box-index="${index}">Remove</button>
      </div>
    </div>
  `;
}

function boxSignalHtml(box) {
  if (box.signalStatus === "loading") return `<span class="box-signal muted">Extracting...</span>`;
  if (box.signalStatus === "error") {
    return `<span class="box-signal error-text" title="${escapeHtml(box.signalError || "Signal extraction failed")}">Extraction failed</span>`;
  }
  if (box.signal !== null) {
    return `
      <span class="box-signal">
        <strong>${box.signal.adjustedSignal.toLocaleString()}</strong>
        <span class="muted-text">(raw: ${box.signal.rawSignal.toLocaleString()})</span>
      </span>
    `;
  }
  return `<span class="box-signal muted">No signal yet</span>`;
}

function saveScanBarHtml() {
  return `
    <div class="save-scan-bar">
      <input type="text" id="scanProteinName" placeholder="Protein name (e.g. pERK)" />
      <button class="primary-button" type="button" id="saveScanButton">Save scan</button>
    </div>
  `;
}

function bindBoxListInputs(container, blotId) {
  container.querySelectorAll(".box-lane-name").forEach(input => {
    input.addEventListener("input", (e) => {
      const index = Number(e.target.dataset.boxIndex);
      canvasState.boxes[index].laneName = e.target.value;
    });
  });
  document.getElementById("saveScanButton")?.addEventListener("click", () => saveScan(blotId));
}

function renderSavedScans(blotId) {
  const scans = scansForBlot(blotId);
  if (!scans.length) return `<p class="blot-empty-state" style="margin-top:12px;">No session scans yet.</p>`;

  return `
    <div class="saved-scans">
      <p class="eyebrow" style="margin: 12px 0 8px;">Session scans</p>
      ${scans.map((scan, index) => `
        <div class="saved-scan-item">
          <div class="saved-scan-main">
            <span class="scan-protein">${escapeHtml(scan.proteinName)}</span>
            <span class="scan-meta">${scan.lanes.length} lanes · ${scan.channel}nm · ${scan.backgroundAxis}${scan.createdAt ? ` · ${escapeHtml(formatTimestamp(scan.createdAt))}` : ""}</span>
            <button class="ghost-button box-button danger" type="button"
              data-scan-delete="${index}">Delete</button>
          </div>
          ${scanAuditDetailsHtml(scan, index)}
        </div>
      `).join("")}
    </div>
  `;
}

function scanAuditDetailsHtml(scan, index) {
  return `
    <details class="scan-audit">
      <summary>Extraction audit</summary>
      <div class="scan-audit-toolbar">
        <span class="muted-text">${escapeHtml(scanAuditSummary(scan))}</span>
        <button class="ghost-button box-button" type="button" data-scan-audit-export="${index}">Export XLSX</button>
      </div>
      <div class="scan-audit-table-wrap">
        <table class="scan-audit-table">
          <thead>
            <tr>
              <th>Lane</th>
              <th>Adjusted</th>
              <th>Raw</th>
              <th>Background</th>
              <th>X</th>
              <th>Y</th>
              <th>W</th>
              <th>H</th>
              <th>Area</th>
            </tr>
          </thead>
          <tbody>
            ${scan.lanes.map(scanAuditLaneRowHtml).join("")}
          </tbody>
        </table>
      </div>
    </details>
  `;
}

function scanAuditLaneRowHtml(lane) {
  return `
    <tr>
      <td>${escapeHtml(lane.name)}</td>
      <td>${formatAuditNumber(lane.adjustedSignal ?? lane.signal)}</td>
      <td>${formatAuditNumber(lane.rawSignal)}</td>
      <td>${formatAuditNumber(lane.backgroundSignal)}</td>
      <td>${formatAuditNumber(lane.x)}</td>
      <td>${formatAuditNumber(lane.y)}</td>
      <td>${formatAuditNumber(lane.w)}</td>
      <td>${formatAuditNumber(lane.h)}</td>
      <td>${formatAuditNumber(lane.area)}</td>
    </tr>
  `;
}

function scanAuditSummary(scan) {
  const settings = scan.settings || {};
  const colorMode = settings.colorMode ? `${settings.colorMode} view` : "viewer settings";
  return `${scan.channel}nm, ${scan.backgroundAxis}, ${colorMode}`;
}

function formatBoxArea(box) {
  const width = Math.max(1, Math.round(Math.abs(Number(box.w) || 0)));
  const height = Math.max(1, Math.round(Math.abs(Number(box.h) || 0)));
  return width * height;
}

function createCanvasBox({ x, y, w, h, laneName = "" }) {
  return constrainBoxToImage({
    x,
    y,
    w,
    h,
    laneName,
    signal: null,
    signalStatus: "loading",
    signalError: "",
    signalRequestId: 0,
  });
}

function normalizeSelectedBoxIndex() {
  if (!canvasState.boxes.length) {
    canvasState.selectedBoxIndex = null;
    return null;
  }
  if (!Number.isInteger(canvasState.selectedBoxIndex)) {
    canvasState.selectedBoxIndex = canvasState.boxes.length - 1;
  }
  canvasState.selectedBoxIndex = Math.min(
    canvasState.boxes.length - 1,
    Math.max(0, canvasState.selectedBoxIndex),
  );
  return canvasState.selectedBoxIndex;
}

function selectedBox() {
  const index = normalizeSelectedBoxIndex();
  return index === null ? null : canvasState.boxes[index];
}

function selectBox(index, blotId = canvasState.currentBlotId) {
  if (!Number.isInteger(index) || index < 0 || index >= canvasState.boxes.length) return;
  canvasState.selectedBoxIndex = index;
  renderCanvas();
  renderBoxList(blotId);
}

function updateBoxToolState() {
  const selectedIndex = normalizeSelectedBoxIndex();
  const hasSelection = selectedIndex !== null;
  const canAlign = hasSelection && canvasState.boxes.length > 1;
  const hasBoxCapacity = remainingBoxSlots() > 0;
  const status = document.getElementById("selectedBoxStatus");
  if (status) {
    status.textContent = hasSelection
      ? `Box ${selectedIndex + 1} selected.`
      : "Select a box below.";
  }

  document.getElementById("duplicateBoxButton")?.toggleAttribute("disabled", !hasSelection || !hasBoxCapacity);
  document.getElementById("matchBoxSizeButton")?.toggleAttribute("disabled", !canAlign);
  document.querySelectorAll("[data-box-align]").forEach(button => {
    button.toggleAttribute("disabled", !canAlign);
  });
  document.querySelectorAll("[data-box-nudge]").forEach(button => {
    button.toggleAttribute("disabled", !hasSelection);
  });
}

function remainingBoxSlots() {
  return Math.max(0, MAX_CANVAS_BOXES - canvasState.boxes.length);
}

function constrainBoxToImage(box) {
  const constrained = {
    ...box,
    x: Number(box.x) || 0,
    y: Number(box.y) || 0,
    w: Math.max(1, Math.abs(Number(box.w) || 1)),
    h: Math.max(1, Math.abs(Number(box.h) || 1)),
  };
  if (canvasState.imageWidth > 0) {
    constrained.w = Math.min(constrained.w, canvasState.imageWidth);
    constrained.x = Math.min(Math.max(0, constrained.x), Math.max(0, canvasState.imageWidth - constrained.w));
  }
  if (canvasState.imageHeight > 0) {
    constrained.h = Math.min(constrained.h, canvasState.imageHeight);
    constrained.y = Math.min(Math.max(0, constrained.y), Math.max(0, canvasState.imageHeight - constrained.h));
  }
  return constrained;
}

function boxOverlapsImage(box) {
  if (canvasState.imageWidth <= 0 || canvasState.imageHeight <= 0) return true;
  return box.x < canvasState.imageWidth
    && box.y < canvasState.imageHeight
    && box.x + box.w > 0
    && box.y + box.h > 0;
}

function markBoxGeometryChanged(box) {
  box.signal = null;
  box.signalStatus = "loading";
  box.signalError = "";
}

function duplicateSelectedBox(blotId) {
  const source = selectedBox();
  const sourceIndex = canvasState.selectedBoxIndex;
  if (!source) return;

  const remainingSlots = remainingBoxSlots();
  if (remainingSlots <= 0) {
    alert(`Maximum ${MAX_CANVAS_BOXES} boxes per blot scan.`);
    return;
  }
  const count = Math.min(
    remainingSlots,
    clampInteger(Number(document.getElementById("duplicateBoxCount")?.value), 1, 48, 1),
  );
  const gap = clampNumber(Number(document.getElementById("duplicateBoxGap")?.value), -2000, 2000, 8);
  const direction = document.getElementById("duplicateBoxDirection")?.value || "right";
  const offsets = {
    right: [source.w + gap, 0],
    left: [-(source.w + gap), 0],
    down: [0, source.h + gap],
    up: [0, -(source.h + gap)],
  };
  const [stepX, stepY] = offsets[direction] || offsets.right;
  const copies = [];

  for (let copyIndex = 1; copyIndex <= count; copyIndex += 1) {
    const candidate = {
      x: source.x + stepX * copyIndex,
      y: source.y + stepY * copyIndex,
      w: source.w,
      h: source.h,
    };
    if (!boxOverlapsImage(candidate)) break;
    copies.push(createCanvasBox(candidate));
  }

  if (!copies.length) return;
  canvasState.boxes.splice(sourceIndex + 1, 0, ...copies);
  canvasState.selectedBoxIndex = sourceIndex + copies.length;
  renderCanvas();
  void extractSignalsForBoxes(blotId, copies, { alertOnError: false });
}

function alignBoxesToSelected(alignment, blotId) {
  const anchor = selectedBox();
  if (!anchor || canvasState.boxes.length < 2) return;

  const changedBoxes = [];
  canvasState.boxes.forEach((box, index) => {
    if (index === canvasState.selectedBoxIndex) return;
    if (alignment === "left") box.x = anchor.x;
    if (alignment === "centerX") box.x = anchor.x + anchor.w / 2 - box.w / 2;
    if (alignment === "right") box.x = anchor.x + anchor.w - box.w;
    if (alignment === "top") box.y = anchor.y;
    if (alignment === "centerY") box.y = anchor.y + anchor.h / 2 - box.h / 2;
    if (alignment === "bottom") box.y = anchor.y + anchor.h - box.h;
    Object.assign(box, constrainBoxToImage(box));
    markBoxGeometryChanged(box);
    changedBoxes.push(box);
  });

  renderCanvas();
  void extractSignalsForBoxes(blotId, changedBoxes, { alertOnError: false });
}

function matchBoxesToSelectedSize(blotId) {
  const anchor = selectedBox();
  if (!anchor || canvasState.boxes.length < 2) return;

  const changedBoxes = [];
  canvasState.boxes.forEach((box, index) => {
    if (index === canvasState.selectedBoxIndex) return;
    const centerX = box.x + box.w / 2;
    const centerY = box.y + box.h / 2;
    box.w = anchor.w;
    box.h = anchor.h;
    box.x = centerX - box.w / 2;
    box.y = centerY - box.h / 2;
    Object.assign(box, constrainBoxToImage(box));
    markBoxGeometryChanged(box);
    changedBoxes.push(box);
  });

  renderCanvas();
  void extractSignalsForBoxes(blotId, changedBoxes, { alertOnError: false });
}

function nudgeSelectedBox(dx, dy, blotId) {
  const box = selectedBox();
  if (!box) return;
  const step = clampInteger(Number(document.getElementById("boxNudgeStep")?.value), 1, 100, 1);
  box.x += dx * step;
  box.y += dy * step;
  Object.assign(box, constrainBoxToImage(box));
  markBoxGeometryChanged(box);
  renderCanvas();
  void extractSignalsForBoxes(blotId, [box], { alertOnError: false });
}

async function saveScan(blotId) {
  const proteinName = document.getElementById("scanProteinName")?.value.trim();
  if (!proteinName) {
    alert("Please enter a protein name before saving.");
    return;
  }

  const hasSignals = canvasState.boxes.every(box => box.signal !== null);
  if (!hasSignals) {
    const isExtracting = canvasState.boxes.some(box => box.signalStatus === "loading");
    alert(isExtracting
      ? "Please wait for signal extraction to finish before saving."
      : "One or more signals could not be extracted. Use Refresh signals and try again."
    );
    return;
  }

  const channel = document.getElementById("quantChannel")?.value ?? "700";
  const backgroundAxis = document.getElementById("backgroundAxis")?.value ?? "leftright";

  const scan = {
    id: clientId("scan"),
    proteinName,
    createdAt: new Date().toISOString(),
    channel,
    backgroundAxis,
    settings: {
      colorMode: document.getElementById("colorMode")?.value ?? "color",
      brightness700: Number(document.getElementById("brightness700")?.value ?? 1),
      contrast700: Number(document.getElementById("contrast700")?.value ?? 1),
      brightness800: Number(document.getElementById("brightness800")?.value ?? 1),
      contrast800: Number(document.getElementById("contrast800")?.value ?? 1),
    },
    lanes: canvasState.boxes.map((box, index) => {
      const signal = box.signal || {};
      const w = finiteNumberOrNull(signal.w) ?? box.w;
      const h = finiteNumberOrNull(signal.h) ?? box.h;
      return {
        name: box.laneName || `Lane ${index + 1}`,
        signal: signal.adjustedSignal,
        adjustedSignal: signal.adjustedSignal,
        rawSignal: signal.rawSignal,
        backgroundSignal: signal.backgroundSignal,
        x: finiteNumberOrNull(signal.x) ?? box.x,
        y: finiteNumberOrNull(signal.y) ?? box.y,
        w,
        h,
        area: Math.round(Math.max(1, w) * Math.max(1, h)),
      };
    }),
  };

  if (!blotState.scans[blotId]) blotState.scans[blotId] = [];
  blotState.scans[blotId].push(scan);
  indexScansForBlot(blotId);

  // Clear boxes for next scan
  canvasState.boxes = [];
  canvasState.selectedBoxIndex = null;
  renderCanvas();
  renderBoxList(blotId);

  refreshBlotDependentControls();
}

async function deleteScan(blotId, scanIndex) {
  if (!blotState.scans[blotId]) return;
  blotState.scans[blotId].splice(scanIndex, 1);
  indexScansForBlot(blotId);
  renderBoxList(blotId);
  refreshBlotDependentControls();
}

function moveBox(index, direction, blotId) {
  const newIndex = index + direction;
  if (newIndex < 0 || newIndex >= canvasState.boxes.length) return;
  const boxes = canvasState.boxes;
  [boxes[index], boxes[newIndex]] = [boxes[newIndex], boxes[index]];
  if (canvasState.selectedBoxIndex === index) {
    canvasState.selectedBoxIndex = newIndex;
  } else if (canvasState.selectedBoxIndex === newIndex) {
    canvasState.selectedBoxIndex = index;
  }
  renderCanvas();
  renderBoxList(blotId);
}

function deleteBox(index, blotId) {
  canvasState.boxes.splice(index, 1);
  if (canvasState.selectedBoxIndex === index) {
    canvasState.selectedBoxIndex = canvasState.boxes.length ? Math.min(index, canvasState.boxes.length - 1) : null;
  } else if (canvasState.selectedBoxIndex > index) {
    canvasState.selectedBoxIndex -= 1;
  }
  renderCanvas();
  renderBoxList(blotId);
}

async function extractSignals(blotId) {
  if (!canvasState.boxes.length) {
    alert("Draw some boxes first.");
    return;
  }

  await extractSignalsForBoxes(blotId, [...canvasState.boxes]);
}

async function extractSignalsForBoxes(blotId, boxes, { alertOnError = true } = {}) {
  if (!blotId || !boxes.length) return;

  const controller = new AbortController();
  signalExtractionControllers.add(controller);
  const channel = document.getElementById("quantChannel")?.value ?? "700";
  const backgroundAxis = document.getElementById("backgroundAxis")?.value ?? "leftright";
  const requests = boxes.map((box) => {
    const requestId = (box.signalRequestId || 0) + 1;
    box.signalRequestId = requestId;
    box.signal = null;
    box.signalStatus = "loading";
    box.signalError = "";
    return { box, requestId };
  });
  renderBoxList(blotId);

  try {
    for (let start = 0; start < requests.length; start += MAX_CANVAS_BOXES) {
      const requestChunk = requests.slice(start, start + MAX_CANVAS_BOXES);
      const data = await apiJson(apiUrl("/extract"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        timeoutMs: SIGNAL_EXTRACTION_TIMEOUT_MS,
        body: JSON.stringify({
          sessionId: activeSessionId,
          blot: blotById(blotId),
          boxes: requestChunk.map(({ box }) => ({ x: box.x, y: box.y, w: box.w, h: box.h })),
          channel,
          backgroundAxis,
        }),
      }, "Signal extraction failed.");

      if (canvasState.currentBlotId !== blotId) return;
      if (!Array.isArray(data.results) || data.results.length !== requestChunk.length) {
        throw new Error("Signal extraction returned an incomplete result set.");
      }
      const activeBoxes = new Set(canvasState.boxes);
      data.results.forEach((result, index) => {
        if (!result || !Number.isFinite(Number(result.adjustedSignal)) || !Number.isFinite(Number(result.rawSignal))) {
          throw new Error("Signal extraction returned invalid signal data.");
        }
        const request = requestChunk[index];
        if (!request || !activeBoxes.has(request.box)) return;
        if (request.box.signalRequestId !== request.requestId) return;
        request.box.signal = result;
        request.box.signalStatus = "complete";
      });
    }

    renderBoxList(blotId);
  } catch (error) {
    if (controller.signal.aborted || error.name === "AbortError") return;
    if (canvasState.currentBlotId !== blotId) return;
    const activeBoxes = new Set(canvasState.boxes);
    requests.forEach(({ box, requestId }) => {
      if (!activeBoxes.has(box) || box.signalRequestId !== requestId) return;
      box.signal = null;
      box.signalStatus = "error";
      box.signalError = error.message;
    });
    renderBoxList(blotId);
    if (alertOnError) alert(`Signal extraction failed: ${error.message}`);
  } finally {
    signalExtractionControllers.delete(controller);
  }
}

// ─── Data Set Conversion ─────────────────────────────────────────────────────────────
function scanToDataset(blotId, scanRef) {
  const scan = findScanByRef(blotId, scanRef);
  if (!scan) return null;

  const rows = scan.lanes.map(lane => ({
    Name: lane.name,
    Signal: lane.signal,
  }));

  return {
    workbook: {
      sheetNames: ["Data"],
      getHeaders() { return ["Name", "Signal"]; },
      getRows() { return rows; }
    },
    sheetName: "Data",
    rows,
    headers: ["Name", "Signal"],
    fileLabel: `${scan.proteinName} (blot scan)`,
    proteinName: scan.proteinName,
  };
}

// ─── PowerPoint export ────────────────────────────────────────────────────────

function canvasToImageDataUrl(canvas) {
  return canvas.toDataURL(JPEG_MIME_TYPE, PPTX_JPEG_QUALITY);
}

async function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

async function compositeToImageDataUrl(blotId, defaultColorMode = "grayscale") {
  return blobToDataUrl(await renderCompositeBlob(blotId, defaultColorMode));
}

async function generatePptx() {
  if (!state.sharedAnalyses.length) {
    alert("Please run an analysis first.");
    return;
  }

  const button = document.getElementById("generatePptxButton");
  if (button) {
    button.textContent = "Generating...";
    button.disabled = true;
  }

  try {
    const slides = await buildPptxSlides();
    const blob = await requestPptxBlob(slides);
    downloadBlob(blob, "western-blot-analysis.pptx");
  } catch (error) {
    alert(`PowerPoint generation failed: ${error.message}`);
  } finally {
    if (button) {
      button.textContent = "Export PowerPoint";
      button.disabled = false;
    }
  }
}

async function buildPptxSlides() {
  const slides = [];
  const imageSlide = await buildPptxImageSlide();
  if (imageSlide.images.length) slides.push(imageSlide);
  state.sharedAnalyses.forEach((analysis, index) => {
    slides.push(buildPptxGraphSlide(analysis, index));
  });
  return slides;
}

async function buildPptxImageSlide() {
  const imageSlide = { type: PPTX_IMAGE_SLIDE_TYPE, images: [] };
  for (let index = 0; index < state.sharedAnalyses.length; index += 1) {
    const image = await sampleBlotPptxImage(index);
    if (image) imageSlide.images.push(image);
  }
  const controlImage = await controlBlotPptxImage();
  if (controlImage) imageSlide.images.push(controlImage);
  return imageSlide;
}

async function sampleBlotPptxImage(index) {
  const blotSelect = document.querySelector(`[data-blot-source-blot="sample-${index}"]`);
  const usingBlot = blotSelect && !document.getElementById(`sampleBlot${index}`)?.hidden;
  if (!usingBlot || !blotSelect?.value) return null;

  return {
    image: await compositeToImageDataUrl(blotSelect.value, "grayscale"),
    label: `anti-${state.sharedAnalyses[index].name}`,
  };
}

async function controlBlotPptxImage() {
  const blotSelect = document.querySelector(`[data-blot-source-blot="control-0"]`);
  const usingBlot = blotSelect && !document.getElementById("controlBlot0")?.hidden;
  if (!usingBlot || !blotSelect?.value) return null;

  const scanSelect = document.querySelector(`[data-blot-source-scan="control-0"]`);
  const scan = findScanByRef(blotSelect.value, scanSelect?.value);
  return {
    image: await compositeToImageDataUrl(blotSelect.value, "grayscale"),
    label: `anti-${scan?.proteinName || "Loading control"}`,
  };
}

function buildPptxGraphSlide(analysis, index) {
  return {
    type: PPTX_GRAPH_SLIDE_TYPE,
    title: analysis.title,
    graphs: buildPptxGraphImages(analysis, index),
  };
}

function buildPptxGraphImages(analysis, index) {
  const graphs = [
    renderChartDataUrl(960, 520, (canvas) => drawBarChart(canvas, analysis.results, analysis.title)),
  ];

  if (els.enableGroupedGraphs.checked) {
    const previousActiveSampleIndex = state.activeSampleIndex;
    state.activeSampleIndex = index;
    const groups = buildGroupedRows(analysis);
    state.activeSampleIndex = previousActiveSampleIndex;

    groups.forEach((group) => {
      const normalizedRows = normalizedGroupRows(group);
      if (!normalizedRows) throw new Error(groupBaselineError(group));
      graphs.push(renderChartDataUrl(640, 360, (canvas) => drawBarChart(canvas, normalizedRows, group.name)));
    });
  }

  return graphs;
}

function renderChartDataUrl(width, height, draw) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  draw(canvas);
  return canvasToImageDataUrl(canvas);
}

async function requestPptxBlob(slides) {
  const response = await apiFetch(apiUrl("/generate-pptx"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slides }),
    timeoutMs: PPTX_EXPORT_TIMEOUT_MS,
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Failed to generate PowerPoint"));
  }
  return response.blob();
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
