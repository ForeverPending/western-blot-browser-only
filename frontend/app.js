const BACKEND_URL = (CONFIG.BACKEND_URL || "").replace(/\/$/, "");
const SESSION_STORAGE_KEY = "western-blot:browser-session-id";
const BLOB_CLIENT_IMPORT_URL = CONFIG.BLOB_CLIENT_IMPORT_URL || "https://esm.sh/@vercel/blob/client";
const ALLOWED_TABULAR_EXTENSIONS = new Set(["csv", "tsv", "xls", "xlsx"]);
const ALLOWED_ZIP_MIME_TYPES = new Set(["", "application/zip", "application/x-zip-compressed", "application/octet-stream"]);
const BLOB_UPLOAD_TIMEOUT_MS = 240000;
let blobClientPromise = null;
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
  chartTitle: "",
};

function initializeApp() {
  const workspace = document.querySelector("#appWorkspace");
  if (workspace) workspace.hidden = false;
}

async function apiFetch(url, options = {}) {
  const { timeoutMs = 0, ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers || {});
  headers.set("X-Blot-Session", activeSessionId);
  let timeoutId = null;
  let abortController = null;

  if (timeoutMs > 0) {
    abortController = new AbortController();
    if (fetchOptions.signal) {
      if (fetchOptions.signal.aborted) abortController.abort();
      fetchOptions.signal.addEventListener("abort", () => abortController.abort(), { once: true });
    }
    timeoutId = window.setTimeout(() => abortController.abort(), timeoutMs);
    fetchOptions.signal = abortController.signal;
  }

  try {
    return await fetch(url, {
      ...fetchOptions,
      headers,
    });
  } catch (error) {
    if (abortController?.signal.aborted && timeoutMs > 0) {
      throw new Error(`Request timed out after ${Math.ceil(timeoutMs / 1000)} seconds.`);
    }
    throw error;
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
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
      ? apiJson(apiUrl("/client-config"), {}, "Could not load deployment config.")
      : Promise.resolve({});
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
  controlProtein: document.querySelector("#controlProtein"),
  controlSheet: document.querySelector("#controlSheet"),
  controlLaneColumn: document.querySelector("#controlLaneColumn"),
  controlSignalColumn: document.querySelector("#controlSignalColumn"),
  normalizationLane: document.querySelector("#normalizationLane"),
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
els.sampleCount.addEventListener("input", () => renderSharedSampleInputs(clampInteger(Number(els.sampleCount.value), 1, 12, 1)));
els.pairCount.addEventListener("input", () => renderPairInputs(clampInteger(Number(els.pairCount.value), 2, 12, 2)));
els.comparisonChartType.addEventListener("change", renderComparisonChart);
els.controlFile.addEventListener("change", (event) => loadSharedControlFile(event));
els.controlSheet.addEventListener("change", () => selectDatasetSheet(state.sharedControl, sharedControlControls(), els.controlSheet.value));
els.controlLaneColumn.addEventListener("change", refreshNormalizationLanes);
els.analyzeButton.addEventListener("click", runAnalysis);
els.downloadChartButton.addEventListener("click", () => downloadCanvasJpeg(els.foldChart, currentSharedChartFilename()));
els.downloadCsvButton.addEventListener("click", downloadSharedCsv);
els.downloadComparisonChartButton.addEventListener("click", () => downloadCanvasJpeg(els.comparisonChart, "common-lane-comparison.jpg"));
els.enableGroupedGraphs.addEventListener("change", renderCurrentGrouping);
els.groupMode.addEventListener("change", renderCurrentGrouping);
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

function createDatasetState() {
  return {
    workbook: null,
    sheetName: "",
    rows: [],
    headers: [],
  };
}

function createAnalysisState(name, title, results) {
  return {
    name,
    title,
    results,
    customGroups: createDefaultCustomGroups(results.length),
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
  els.sharedResults.hidden = isComparison;
  els.sharedCountWrap.hidden = isComparison;
  els.comparisonWorkflow.hidden = !isComparison;
  els.comparisonResults.hidden = !isComparison;
  els.pairCountWrap.hidden = !isComparison;
  els.comparisonChartWrap.hidden = !isComparison;
  refreshNormalizationLanes();
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
      selectDatasetSheet(state.sharedSamples[index], controls, controls.sheet.value);
      refreshNormalizationLanes();
    });
    controls.lane.addEventListener("change", refreshNormalizationLanes);
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
        <div class="field-row">
          <label>
            Protein name
            <input data-shared-sample-protein="${index}" type="text" placeholder="e.g. pERK" value="${escapeHtml(state.sharedSamples[index]?.proteinName || "")}" />
          </label>
        </div>
        <div class="mapping-grid">
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
        <div class="field-row">
          <label>
            Blot
            <select data-blot-source-blot="sample-${index}" data-refresh-scan-role="sample" data-refresh-scan-index="${index}">
              <option value="">-- Select blot --</option>
            </select>
          </label>
        </div>
        <div class="field-row">
          <label>
            Protein scan
            <select data-blot-source-scan="sample-${index}">
              <option value="">-- Select scan --</option>
            </select>
          </label>
        </div>
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
      selectDatasetSheet(state.pairedSets[index].sample, controls.sample, controls.sample.sheet.value);
      refreshNormalizationLanes();
    });
    controls.control.sheet.addEventListener("change", () => selectDatasetSheet(state.pairedSets[index].control, controls.control, controls.control.sheet.value));
    controls.sample.lane.addEventListener("change", refreshNormalizationLanes);
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
            <div class="mapping-grid">
              <label>Sheet<select data-pair-sample-sheet="${index}"></select></label>
              <label>Lane column<select data-pair-sample-lane="${index}"></select></label>
              <label>Signal column<select data-pair-sample-signal="${index}"></select></label>
            </div>
          </div>

          <div id="pairSampleBlot${index}" class="source-panel" hidden>
            <div class="field-row">
              <label>
                Blot
                <select data-blot-source-blot="pair-sample-${index}" data-refresh-scan-role="pair-sample" data-refresh-scan-index="${index}">
                  <option value="">-- Select blot --</option>
                </select>
              </label>
            </div>
            <div class="field-row">
              <label>
                Protein scan
                <select data-blot-source-scan="pair-sample-${index}">
                  <option value="">-- Select scan --</option>
                </select>
              </label>
            </div>
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
            <div class="mapping-grid">
              <label>Sheet<select data-pair-control-sheet="${index}"></select></label>
              <label>Lane column<select data-pair-control-lane="${index}"></select></label>
              <label>Signal column<select data-pair-control-signal="${index}"></select></label>
            </div>
          </div>

          <div id="pairControlBlot${index}" class="source-panel" hidden>
            <div class="field-row">
              <label>
                Blot
                <select data-blot-source-blot="pair-control-${index}" data-refresh-scan-role="pair-control" data-refresh-scan-index="${index}">
                  <option value="">-- Select blot --</option>
                </select>
              </label>
            </div>
            <div class="field-row">
              <label>
                Protein scan
                <select data-blot-source-scan="pair-control-${index}">
                  <option value="">-- Select scan --</option>
                </select>
              </label>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function loadSharedSampleFile(event, index) {
  await loadDatasetFile(event, state.sharedSamples[index], sharedSampleControls[index]);
  refreshNormalizationLanes();
}

async function loadSharedControlFile(event) {
  await loadDatasetFile(event, state.sharedControl, sharedControlControls());
}

async function loadPairFile(event, index, role) {
  await loadDatasetFile(event, state.pairedSets[index][role], pairControls[index][role]);
  refreshNormalizationLanes();
}

async function loadDatasetFile(event, dataset, controls) {
  const file = event.target.files?.[0];
  if (!file) return;

  try {
    dataset.fileLabel = file.name;
    controls.fileName.textContent = file.name;
    dataset.workbook = await readTabularFile(file);
    populateSheetSelect(controls.sheet, dataset.workbook);
    selectDatasetSheet(dataset, controls, dataset.workbook.sheetNames[0]);
  } catch (error) {
    showSharedError(`Could not read file. ${error.message}`);
  }
}

async function readTabularFile(file) {
  const extension = file.name.split(".").pop().toLowerCase();
  const maxBytes = Number(CONFIG.MAX_TABULAR_UPLOAD_BYTES || 25 * 1024 * 1024);
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
    getRows(sheetName) {
      return XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], { defval: "" });
    },
  };
}

function csvToWorkbook(text, delimiter) {
  const lines = text.trim().split(/\r?\n/);
  const headers = splitDelimitedLine(lines[0], delimiter).map((header) => header.trim());
  const rows = lines.slice(1).map((line) => {
    const values = splitDelimitedLine(line, delimiter);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
  return {
    sheetNames: ["Data"],
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

function populateSheetSelect(select, workbook) {
  select.innerHTML = workbook.sheetNames
    .map((sheetName) => `<option value="${escapeHtml(sheetName)}">${escapeHtml(sheetName)}</option>`)
    .join("");
}

function selectDatasetSheet(dataset, controls, sheetName) {
  if (!dataset.workbook) return;
  dataset.sheetName = sheetName;
  dataset.rows = dataset.workbook.getRows(sheetName).filter((row) =>
    Object.values(row).some((value) => String(value).trim() !== ""),
  );
  dataset.headers = collectHeaders(dataset.rows);
  populateColumnSelects(controls, dataset.headers);
}

function hydrateDatasetControls(dataset, controls) {
  if (!dataset.workbook) return;
  populateSheetSelect(controls.sheet, dataset.workbook);
  controls.sheet.value = dataset.sheetName || dataset.workbook.sheetNames[0];
  populateColumnSelects(controls, dataset.headers);
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
  const previous = els.normalizationLane.value;
  let lanes = [];

  if (state.mode === "comparison") {
    const blotDataset = getBlotSourceDataset("pair-sample", 0);
    if (blotDataset) {
      lanes = blotDataset.rows.map((row, index) => normalizeLaneName(row.Name, index));
    } else {
      const source = state.pairedSets[0]?.sample;
      const controls = pairControls[0]?.sample;
      lanes = source && controls ? extractLaneNames(source, controls) : [];
    }
  } else {
    // Check if sample 0 is using blot source mode
    const blotPanel = document.getElementById("sampleBlot0");
    const usingBlot = blotPanel && !blotPanel.hidden;

    if (usingBlot) {
      const blotSelect = document.querySelector(`[data-blot-source-blot="sample-0"]`);
      const scanSelect = document.querySelector(`[data-blot-source-scan="sample-0"]`);
      if (blotSelect?.value && scanSelect?.value !== "") {
        const scan = blotState.scans[blotSelect.value]?.[Number(scanSelect.value)];
        lanes = scan ? scan.lanes.map(lane => lane.name) : [];
      }
    } else {
      const source = state.sharedSamples[0];
      const controls = sharedSampleControls[0];
      lanes = source && controls ? extractLaneNames(source, controls) : [];
    }
  }

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

  return scanToDataset(blotSelect.value, Number(scanSelect.value));
}

function getPairAnalysisSource(index, role) {
  const blotDataset = getBlotSourceDataset(`pair-${role}`, index);
  if (blotDataset) {
    return {
      dataset: blotDataset,
      controls: blotDatasetControls(),
    };
  }

  return {
    dataset: state.pairedSets[index][role],
    controls: pairControls[index][role],
  };
}

function runAnalysis() {
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
    // Get control dataset — file or blot
    let controlDataset, controlProteinName;
    const controlBlotSelect = document.querySelector(`[data-blot-source-blot="control-0"]`);
    const controlScanSelect = document.querySelector(`[data-blot-source-scan="control-0"]`);
    const controlUsingBlot = controlBlotSelect && !document.getElementById("controlBlot0")?.hidden;

    if (controlUsingBlot && controlBlotSelect.value && controlScanSelect.value !== "") {
      controlDataset = scanToDataset(controlBlotSelect.value, Number(controlScanSelect.value));
      controlProteinName = controlDataset.proteinName;
    } else {
      controlDataset = state.sharedControl;
      controlProteinName = els.controlProtein.value.trim() || "Loading control";
    }

    const controlRows = buildRows(controlDataset, controlUsingBlot
      ? { lane: { value: "Name" }, signal: { value: "Signal" } }
      : sharedControlControls()
    );
    if (!controlRows.length) throw new Error("The control needs readable lanes and signals.");

    state.sharedAnalyses = state.sharedSamples.map((sampleDataset, index) => {
      const blotSelect = document.querySelector(`[data-blot-source-blot="sample-${index}"]`);
      const scanSelect = document.querySelector(`[data-blot-source-scan="sample-${index}"]`);
      const usingBlot = blotSelect && !document.getElementById(`sampleBlot${index}`)?.hidden;

      let dataset, sampleProtein;
      if (usingBlot && blotSelect.value && scanSelect.value !== "") {
        dataset = scanToDataset(blotSelect.value, Number(scanSelect.value));
        sampleProtein = dataset.proteinName;
      } else {
        dataset = sampleDataset;
        const controls = sharedSampleControls[index];
        sampleProtein = controls.protein.value.trim() || `Sample ${index + 1}`;
      }

      const controls = usingBlot
        ? { lane: { value: "Name" }, signal: { value: "Signal" } }
        : sharedSampleControls[index];

      const sampleRows = buildRows(dataset, controls);
      if (!sampleRows.length) throw new Error(`Sample ${index + 1} needs readable lanes and signals.`);

      sampleDataset.proteinName = sampleProtein;
      const title = `${sampleProtein} - ${controlProteinName} fold change`;
      return createAnalysisState(
        sampleProtein,
        title,
        computeFoldChange(sampleRows, controlRows, els.normalizationLane.value)
      );
    });

    state.activeSampleIndex = Math.min(state.activeSampleIndex, state.sharedAnalyses.length - 1);
    renderSampleTabs();
    renderActiveSharedAnalysis();
  } catch (error) {
    showSharedError(error.message);
  }
}

function runComparisonAnalysis() {
  try {
    state.pairedAnalyses = state.pairedSets.map((pair, index) => {
      const sampleSource = getPairAnalysisSource(index, "sample");
      const controlSource = getPairAnalysisSource(index, "control");
      const sampleRows = buildRows(sampleSource.dataset, sampleSource.controls);
      const controlRows = buildRows(controlSource.dataset, controlSource.controls);
      if (!sampleRows.length || !controlRows.length) {
        throw new Error(`Pair ${index + 1} needs both sample and control lanes/signals.`);
      }

      const label = pairControls[index].sample.label.value.trim() || `Sample ${index + 1}`;
      pair.label = label;
      return createAnalysisState(label, label, computeFoldChange(sampleRows, controlRows, els.normalizationLane.value));
    });
    state.comparisonCustomGroups = createDefaultCustomGroups(getCommonComparisonLabels().length);

    renderComparisonLabelEditor();
    renderComparisonChart();
    els.comparisonLabelPanel.hidden = false;
    els.comparisonChartPanel.hidden = false;
    els.downloadComparisonChartButton.disabled = false;
  } catch (error) {
    els.comparisonChartTitle.innerHTML = `<span class="error-text">${escapeHtml(error.message)}</span>`;
    els.comparisonLabelPanel.hidden = true;
    els.comparisonChartPanel.hidden = false;
    els.downloadComparisonChartButton.disabled = true;
    drawEmptyChart(els.comparisonChart);
  }
}

function buildRows(dataset, controls) {
  const laneColumn = controls?.lane?.value;
  const signalColumn = controls?.signal?.value;
  if (!dataset?.rows?.length || !laneColumn || !signalColumn) return [];

  return dataset.rows
    .map((row, index) => {
      const lane = normalizeLaneName(row[laneColumn], index);
      return { lane, displayLane: lane, signal: parseSignal(row[signalColumn]) };
    })
    .filter((row) => row.lane && Number.isFinite(row.signal));
}

function computeFoldChange(sampleRows, controlRows, normalizationLane) {
  const sampleMap = new Map(sampleRows.map((row) => [row.lane, row]));
  const controlMap = new Map(controlRows.map((row) => [row.lane, row]));
  const baselineSample = sampleMap.get(normalizationLane);
  const baselineControl = controlMap.get(normalizationLane);

  if (!baselineSample || !baselineControl) {
    throw new Error("The normalization lane must exist in every sample and control file.");
  }
  if (!baselineSample.signal || !baselineControl.signal) {
    throw new Error("The normalization lane must have non-zero sample and control signals.");
  }

  return sampleRows.map((sampleRow) => {
    const controlRow = controlMap.get(sampleRow.lane);
    if (!controlRow) throw new Error(`No matching control lane found for "${sampleRow.lane}".`);

    const samplePercent = (sampleRow.signal / baselineSample.signal) * 100;
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
  state.chartTitle = analysis.title;
  els.chartTitle.textContent = analysis.title;
  drawBarChart(els.foldChart, analysis.results, analysis.title);
  renderResultTable(analysis.results);
  renderLabelEditor(analysis.results);
  renderGroupedGraphs();
  els.downloadChartButton.disabled = false;
  els.downloadCsvButton.disabled = false;
  els.labelPanel.hidden = false;
  document.getElementById("pptxPanel").hidden = false;
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
  return Array.from({ length: size }, (_, offset) => {
    const groupRows = rows.filter((_, index) => index % size === offset);
    return {
      name: `Group ${offset + 1}: ${groupRows.map(labelForRow).join(", ")}`,
      rows: groupRows,
    };
  }).filter((group) => group.rows.length);
}

function buildBlockGroups(rows, size) {
  const groups = [];
  for (let index = 0; index < rows.length; index += size) {
    const groupRows = rows.slice(index, index + size);
    groups.push({ name: `Group ${groups.length + 1}: lanes ${index + 1}-${index + groupRows.length}`, rows: groupRows });
  }
  return groups;
}

// Resolves saved lane selections against the current row collection.
function buildSelectedGroups(groupDefinitions, rows) {
  return groupDefinitions
    .map((group) => ({
      name: group.name.trim() || "Custom group",
      rows: group.indices.map((index) => rows[index]).filter(Boolean),
    }))
    .filter((group) => group.rows.length);
}

function buildCustomGroups(analysis) {
  return buildSelectedGroups(analysis.customGroups, analysis.results);
}

function renderGroupSummary(groups) {
  els.groupSummary.innerHTML = groups
    .map((group) => {
      const baseline = group.rows[0]?.foldChange || 1;
      const items = group.rows
        .map((row) => `<li>${escapeHtml(row.displayLane || row.lane)}: ${formatNumber(row.foldChange / baseline)}x</li>`)
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
    const baseline = group.rows[0]?.foldChange || 1;
    drawBarChart(
      document.querySelector(`#groupChart${index}`),
      group.rows.map((row) => ({ ...row, foldChange: row.foldChange / baseline })),
      group.name,
    );
  });
}

function renderCustomGroupingPanel(analysis) {
  const isCustom = els.groupMode.value === "custom" && els.enableGroupedGraphs.checked;
  els.customGroupingPanel.hidden = !isCustom;
  if (!isCustom) return;
  if (!analysis.customGroups.length) analysis.customGroups = createDefaultCustomGroups(analysis.results.length);
  els.customGroups.innerHTML = analysis.customGroups
    .map(
      (group, groupIndex) => `
        <div class="custom-group-card">
          <input type="text" value="${escapeHtml(group.name)}" data-custom-name="${groupIndex}" />
          <div class="lane-picks">
            ${analysis.results
              .map(
                (row, laneIndex) => `
                  <label>
                    <input type="checkbox" data-custom-group="${groupIndex}" data-custom-lane="${laneIndex}" ${group.indices.includes(laneIndex) ? "checked" : ""} />
                    ${escapeHtml(row.displayLane || row.lane)}
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
  const laneIndex = Number(checkbox.dataset.customLane);
  if (!group) return;
  if (checkbox.checked && !group.indices.includes(laneIndex)) group.indices.push(laneIndex);
  if (!checkbox.checked) group.indices = group.indices.filter((index) => index !== laneIndex);
  group.indices.sort((a, b) => a - b);
  renderGroupedGraphs();
}

function addCustomGroup() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  analysis.customGroups.push({ name: `Custom group ${analysis.customGroups.length + 1}`, indices: [] });
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
  if (!state.pairedAnalyses.length) return;
  const commonLabels = getCommonComparisonLabels();
  const rows = commonLabels.map((label) => ({
    label,
    values: state.pairedAnalyses.map((analysis) => {
      const row = analysis.results.find((candidate) => (candidate.displayLane || candidate.lane) === label);
      return row.foldChange;
    }),
  }));

  els.comparisonChartTitle.textContent = commonLabels.length
    ? "Common lane comparison"
    : "No shared lane labels found";

  if (els.comparisonChartType.value === "points") {
    drawAveragePointChart(els.comparisonChart, rows, state.pairedAnalyses.map((analysis) => analysis.name));
  } else {
    drawGroupedComparisonChart(els.comparisonChart, rows, state.pairedAnalyses.map((analysis) => analysis.name));
  }

  renderComparisonGroupedGraphs(rows);
}

function getCommonComparisonLabels() {
  const labelSets = state.pairedAnalyses.map((analysis) => new Set(analysis.results.map((row) => row.displayLane || row.lane)));
  if (!labelSets.length) return [];
  return [...labelSets[0]].filter((label) => labelSets.every((set) => set.has(label)));
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
  return buildSelectedGroups(state.comparisonCustomGroups, rows);
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
  if (!state.comparisonCustomGroups.length) state.comparisonCustomGroups = createDefaultCustomGroups(rows.length);

  els.comparisonCustomGroups.innerHTML = state.comparisonCustomGroups
    .map(
      (group, groupIndex) => `
        <div class="custom-group-card">
          <input type="text" value="${escapeHtml(group.name)}" data-comparison-custom-name="${groupIndex}" />
          <div class="lane-picks">
            ${rows
              .map(
                (row, laneIndex) => `
                  <label>
                    <input type="checkbox" data-comparison-custom-group="${groupIndex}" data-comparison-custom-lane="${laneIndex}" ${group.indices.includes(laneIndex) ? "checked" : ""} />
                    ${escapeHtml(row.label)}
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
  const laneIndex = Number(checkbox.dataset.comparisonCustomLane);
  if (!group) return;
  if (checkbox.checked && !group.indices.includes(laneIndex)) group.indices.push(laneIndex);
  if (!checkbox.checked) group.indices = group.indices.filter((index) => index !== laneIndex);
  group.indices.sort((a, b) => a - b);
  renderComparisonChart();
}

function addComparisonCustomGroup() {
  state.comparisonCustomGroups.push({ name: `Custom group ${state.comparisonCustomGroups.length + 1}`, indices: [] });
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

function createDefaultCustomGroups(count) {
  return [
    { name: "Odd lanes", indices: Array.from({ length: count }, (_, index) => index).filter((index) => index % 2 === 0) },
    { name: "Even lanes", indices: Array.from({ length: count }, (_, index) => index).filter((index) => index % 2 === 1) },
  ].filter((group) => group.indices.length);
}

function showSharedError(message) {
  els.chartTitle.innerHTML = `<span class="error-text">${escapeHtml(message)}</span>`;
  drawEmptyChart(els.foldChart);
  els.downloadChartButton.disabled = true;
  els.downloadCsvButton.disabled = true;
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
  link.href = canvas.toDataURL("image/jpeg", 0.95);
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
  if (typeof value === "number") return value;
  const cleaned = String(value ?? "").replace(/,/g, "").trim();
  const number = Number(cleaned);
  return Number.isFinite(number) ? number : NaN;
}

function clampInteger(value, min, max, fallback) {
  if (!Number.isInteger(value)) return fallback;
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
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 3,
    minimumFractionDigits: 0,
  });
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

const blotState = {
  blots: [],
  activeBlotIndex: null,
  scans: {},  // key: blotId, value: array of {proteinName, channel, backgroundAxis, lanes: [{name, signal}]}
};

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
    currentBlotId: null,
    imageWidth: 0,
    imageHeight: 0,
  };
}

let canvasState = createCanvasState();
let canvasReloadTimer = null;
let canvasImageController = null;
let canvasImageRequestId = 0;
const blotListElement = document.querySelector("#blotList");
const blotScrollUpButton = document.querySelector("#blotScrollUp");
const blotScrollDownButton = document.querySelector("#blotScrollDown");

initializeApp();

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

async function loadPersistedBlots() {
  renderBlotList();
  refreshBlotSourceDropdowns();
}

function resetBlotWorkspace() {
  cancelPendingCanvasImageLoad();
  if (canvasState.imageObjectUrl) URL.revokeObjectURL(canvasState.imageObjectUrl);
  if (canvasState.image) canvasState.image.src = "";
  canvasState = createCanvasState();

  blotState.blots = [];
  blotState.scans = {};
  blotState.activeBlotIndex = null;

  const preview = document.querySelector("#blotPreview");
  if (preview) {
    preview.innerHTML = '<p class="blot-empty-state">Select a blot to preview</p>';
  }
  const zipInput = document.querySelector("#zipFileInput");
  if (zipInput) zipInput.value = "";
  setZipUploadStatus("");

  document.querySelectorAll("[data-blot-source-scan]").forEach((select) => {
    select.innerHTML = '<option value="">-- Select scan --</option>';
  });
  renderBlotList();
  refreshBlotSourceDropdowns();
  refreshNormalizationLanes();
}

function mergeBlots(blots) {
  const activeBlotId = blotState.blots[blotState.activeBlotIndex]?.id;
  blots.forEach((blot) => {
    const existingIndex = blotState.blots.findIndex((candidate) => candidate.id === blot.id);
    if (existingIndex >= 0) {
      blotState.blots[existingIndex] = { ...blotState.blots[existingIndex], ...blot };
    } else {
      blotState.blots.push(blot);
    }
    if (!blotState.scans[blot.id]) blotState.scans[blot.id] = [];
  });
  sortBlotsByCreatedAt();
  blotState.activeBlotIndex = activeBlotId
    ? blotState.blots.findIndex((blot) => blot.id === activeBlotId)
    : null;
}

function sortBlotsByCreatedAt() {
  blotState.blots.sort((left, right) => {
    const leftTime = Date.parse(left.createdAt || "");
    const rightTime = Date.parse(right.createdAt || "");
    const safeLeftTime = Number.isFinite(leftTime) ? leftTime : Number.POSITIVE_INFINITY;
    const safeRightTime = Number.isFinite(rightTime) ? rightTime : Number.POSITIVE_INFINITY;
    return safeLeftTime - safeRightTime || String(left.name || "").localeCompare(String(right.name || ""));
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

function blotListRowHeight() {
  return blotListElement?.querySelector(".blot-list-row")?.offsetHeight || 40;
}

function scrollBlotList(direction) {
  if (!blotListElement) return;
  blotListElement.scrollTo({
    top: blotListElement.scrollTop + direction * blotListRowHeight(),
    behavior: "smooth",
  });
}

function updateBlotScrollButtons() {
  if (!blotListElement || !blotScrollUpButton || !blotScrollDownButton) return;
  const maxScrollTop = Math.max(0, blotListElement.scrollHeight - blotListElement.clientHeight);
  blotScrollUpButton.disabled = blotListElement.scrollTop <= 1;
  blotScrollDownButton.disabled = blotListElement.scrollTop >= maxScrollTop - 1;
}

function ensureActiveBlotVisible() {
  if (!blotListElement || blotState.activeBlotIndex === null) return;
  const activeButton = blotListElement.querySelector(`[data-blot-index="${blotState.activeBlotIndex}"]`);
  const activeRow = activeButton?.closest(".blot-list-row");
  if (!activeRow) return;

  const rowTop = activeRow.offsetTop;
  const rowBottom = rowTop + activeRow.offsetHeight;
  const visibleTop = blotListElement.scrollTop;
  const visibleBottom = visibleTop + blotListElement.clientHeight;
  if (rowTop < visibleTop) {
    blotListElement.scrollTo({ top: rowTop, behavior: "smooth" });
  } else if (rowBottom > visibleBottom) {
    blotListElement.scrollTo({ top: rowBottom - blotListElement.clientHeight, behavior: "smooth" });
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
  const affectedSources = Array.from(document.querySelectorAll("[data-blot-source-blot]"))
    .filter((select) => select.value === blot.id)
    .map((select) => ({
      role: select.dataset.refreshScanRole,
      index: Number(select.dataset.refreshScanIndex),
    }));

  blotState.blots.splice(deletionIndex, 1);
  delete blotState.scans[blot.id];
  blotState.activeBlotIndex = activeBlotId
    ? blotState.blots.findIndex((candidate) => candidate.id === activeBlotId)
    : null;

  if (wasActive) {
    cancelPendingCanvasImageLoad();
    if (canvasState.imageObjectUrl) URL.revokeObjectURL(canvasState.imageObjectUrl);
    if (canvasState.image) canvasState.image.src = "";
    canvasState = createCanvasState();
    const preview = document.querySelector("#blotPreview");
    if (preview) preview.innerHTML = '<p class="blot-empty-state">Select a blot to preview</p>';
  }

  renderBlotList();
  refreshBlotSourceDropdowns();
  affectedSources.forEach(({ role, index: sourceIndex }) => refreshScanDropdown(role, sourceIndex));
  refreshNormalizationLanes();
  setZipUploadStatus(`${blot.name} deleted.`, true);

  if (wasActive && blotState.blots.length) {
    await selectBlot(Math.min(deletionIndex, blotState.blots.length - 1));
  }
}

function switchSource(button, role, index, mode) {
  const group = button.dataset.sourceGroup;
  document.querySelectorAll(`[data-source-group="${group}"]`).forEach((sourceButton) => {
    sourceButton.classList.remove("active");
  });
  button.classList.add("active");

  document.getElementById(sourcePanelId(role, "file", index)).hidden = mode === "blot";
  document.getElementById(sourcePanelId(role, "blot", index)).hidden = mode === "file";

  if (mode === "blot") {
    refreshBlotSourceDropdowns();
    // Wire up scan dropdown change to refresh normalization lanes
    const scanSelect = document.querySelector(`[data-blot-source-scan="${role}-${index}"]`);
    scanSelect?.addEventListener("change", refreshNormalizationLanes);
  } else {
    refreshNormalizationLanes();
  }
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

  const scanDelete = event.target.closest("[data-scan-delete]");
  if (scanDelete) {
    deleteScan(canvasState.currentBlotId, Number(scanDelete.dataset.scanDelete));
  }
}

function handleDocumentChange(event) {
  const select = event.target.closest("[data-refresh-scan-role]");
  if (!select) return;
  refreshScanDropdown(select.dataset.refreshScanRole, Number(select.dataset.refreshScanIndex));
}

function refreshBlotSourceDropdowns() {
  document.querySelectorAll("[data-blot-source-blot]").forEach(select => {
    const currentValue = select.value;
    select.innerHTML = `<option value="">-- Select blot --</option>` +
      blotState.blots
        .filter(blot => blotState.scans[blot.id]?.length > 0)
        .map(blot => `<option value="${escapeHtml(blot.id)}" ${blot.id === currentValue ? "selected" : ""}>${escapeHtml(blot.name)}</option>`)
        .join("");
  });
}

function refreshScanDropdown(role, index) {
  const blotSelect = document.querySelector(`[data-blot-source-blot="${role}-${index}"]`);
  const scanSelect = document.querySelector(`[data-blot-source-scan="${role}-${index}"]`);
  if (!blotSelect || !scanSelect) return;

  const blotId = blotSelect.value;
  const scans = blotState.scans[blotId] || [];
  const currentValue = scanSelect.value;

  scanSelect.innerHTML = `<option value="">-- Select scan --</option>` +
    scans.map((scan, i) => `
      <option value="${i}">${escapeHtml(scan.proteinName)} (${scan.lanes.length} lanes)</option>
    `).join("");

  if (currentValue && Number(currentValue) < scans.length) scanSelect.value = currentValue;
  refreshNormalizationLanes();
}

async function uploadZip(file) {
  const maxBytes = Number(CONFIG.MAX_ZIP_UPLOAD_BYTES || 262144000);
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
        blotState.scans[blotId] = scans;
      });
    }
    renderBlotList();
    refreshBlotSourceDropdowns();
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
      timeoutMs: 270000,
    }, "ZIP processing failed.");
  }

  const form = new FormData();
  form.append("sessionId", activeSessionId);
  form.append("file", file);
  return apiJson(apiUrl("/upload-zip"), {
    method: "POST",
    body: form,
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
    timeoutMs: 15000,
  }, "Could not prepare Blob upload.");

  const payload = presign.presignedUrlPayload;
  if (!payload?.delegationToken || !payload?.signature || !payload?.params) {
    throw new Error("Blob upload endpoint returned an invalid presigned URL payload.");
  }

  const uploadUrl = buildPresignedBlobUrl(pathname, payload);
  const response = await uploadFileWithProgress(uploadUrl, file, {
    access: options.access,
    contentType,
    signal: options.signal,
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
    xhr.send(file);
  });
}

async function blobUploadStatus() {
  const status = await apiJson(apiUrl("/blob-upload"), { timeoutMs: 15000 }, "Blob upload endpoint is not reachable.");
  if (!status.hasBlobReadWriteToken) {
    throw new Error("BLOB_READ_WRITE_TOKEN is not configured for this deployment.");
  }
  return status;
}

function blobClientImportUrl() {
  const url = new URL(BLOB_CLIENT_IMPORT_URL, window.location.href);
  if (url.origin !== "https://esm.sh" || !url.pathname.startsWith("/@vercel/blob@")) {
    throw new Error("Invalid Blob client import URL.");
  }
  return url.href;
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
  cleanupBlots(blotState.blots, true);
});

async function selectBlot(index) {
  blotState.activeBlotIndex = index;
  renderBlotList();

  const blot = blotState.blots[index];
  const preview = document.querySelector("#blotPreview");
  if (!preview) return;

  // Show loading state
  preview.innerHTML = `<p class="blot-empty-state">Loading...</p>`;

  try {
    // Build the viewer UI
    preview.innerHTML = `
      <div class="blot-viewer">

        <div class="blot-controls-bar">
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

          <div class="blot-control-group">
            <span class="channel-label red">700nm</span>
            <label>Brightness
              <input type="range" min="0.1" max="3" step="0.05" value="1" id="brightness700" />
            </label>
            <label>Contrast
              <input type="range" min="0.1" max="5" step="0.1" value="1" id="contrast700" />
            </label>
          </div>

          <div class="blot-control-group">
            <span class="channel-label green">800nm</span>
            <label>Brightness
              <input type="range" min="0.1" max="3" step="0.05" value="1" id="brightness800" />
            </label>
            <label>Contrast
              <input type="range" min="0.1" max="5" step="0.1" value="1" id="contrast800" />
            </label>
          </div>

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
        </div>

        <div class="blot-canvas-wrap">
          <canvas id="blotCanvas"></canvas>
        </div>

        <div class="blot-box-list-wrap">
          <p class="eyebrow" style="margin-bottom:8px;">Drawn boxes</p>
          <div id="blotBoxList" class="blot-box-list">
            <p class="blot-empty-state">No boxes drawn yet.</p>
          </div>
        </div>

      </div>
    `;

    // Initialize canvas
    initCanvas(blot.id);

    // Wire up sliders and color mode to reload the canvas image
    ["brightness700", "contrast700", "brightness800", "contrast800", "colorMode"].forEach(id => {
      document.getElementById(id)?.addEventListener("input", () => {
        scheduleCanvasImageReload(blot.id);
      });
      document.getElementById(id)?.addEventListener("change", () => {
        scheduleCanvasImageReload(blot.id, true);
      });
    });

  } catch (error) {
    preview.innerHTML = `<p class="blot-empty-state">Failed to load blot: ${escapeHtml(error.message)}</p>`;
  }
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
    renderCanvas();
    renderBoxList(blotId);
  });
  document.getElementById("extractSignalsButton")?.addEventListener("click", () => extractSignals(blotId));

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
  img.onerror = () => URL.revokeObjectURL(img.src);
  try {
    const blob = await renderCompositeBlob(blotId, "color", controller.signal);
    if (requestId !== canvasImageRequestId || blotId !== canvasState.currentBlotId) return;
    img.src = URL.createObjectURL(blob);
  } catch (error) {
    if (error.name === "AbortError") return;
    alert(error.message);
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
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Could not load blot image."));
  }
  return response.blob();
}

function renderCanvas() {
  const canvas = document.getElementById("blotCanvas");
  if (!canvas) return;
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
      ctx.strokeStyle = themeToken("--annotation");
      ctx.lineWidth = 2 / canvasState.zoom;
      ctx.strokeRect(box.x, box.y, box.w, box.h);

      // Number label
      ctx.fillStyle = themeToken("--annotation");
      ctx.font = themeFont(600, 14 / canvasState.zoom, "--font-mono");
      ctx.fillText(index + 1, box.x + 4 / canvasState.zoom, box.y + 16 / canvasState.zoom);
    });

    ctx.restore();
  }
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
      const box = {
        x: w > 0 ? canvasState.startX : coords.x,
        y: h > 0 ? canvasState.startY : coords.y,
        w: Math.abs(w),
        h: Math.abs(h),
        signal: null,
        signalStatus: "loading",
        signalRequestId: 0,
      };
      canvasState.boxes.push(box);
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

  if (!canvasState.boxes.length) {
    container.innerHTML = `
      <p class="blot-empty-state">No boxes drawn yet.</p>
      <div id="savedScansWrap">
        ${renderSavedScans(blotId)}
      </div>
    `;
    return;
  }

  container.innerHTML = `
    ${canvasState.boxes.map((box, index) => `
      <div class="box-list-item">
        <span class="box-number">${index + 1}</span>
        <input
          type="text"
          class="box-lane-name"
          value="${escapeHtml(box.laneName || `Lane ${index + 1}`)}"
          data-box-index="${index}"
          placeholder="Lane name"
        />
        <span class="box-area">Area <strong>${formatBoxArea(box).toLocaleString()} px²</strong></span>
        ${box.signalStatus === "loading" ? `
          <span class="box-signal muted">Extracting...</span>
        ` : box.signalStatus === "error" ? `
          <span class="box-signal error-text" title="${escapeHtml(box.signalError || "Signal extraction failed")}">Extraction failed</span>
        ` : box.signal !== null ? `
          <span class="box-signal">
            <strong>${box.signal.adjustedSignal.toLocaleString()}</strong>
            <span class="muted-text">(raw: ${box.signal.rawSignal.toLocaleString()})</span>
          </span>
        ` : `<span class="box-signal muted">No signal yet</span>`}
        <div class="box-actions">
          <button class="ghost-button box-button" type="button"
            data-box-action="move" data-box-index="${index}" data-box-direction="-1">Up</button>
          <button class="ghost-button box-button" type="button"
            data-box-action="move" data-box-index="${index}" data-box-direction="1">Down</button>
          <button class="ghost-button box-button danger" type="button"
            data-box-action="delete" data-box-index="${index}">Remove</button>
        </div>
      </div>
    `).join("")}

    <div class="save-scan-bar">
      <input type="text" id="scanProteinName" placeholder="Protein name (e.g. pERK)" />
      <button class="primary-button" type="button" id="saveScanButton">Save scan</button>
    </div>

    <div id="savedScansWrap">
      ${renderSavedScans(blotId)}
    </div>
  `;

  // Wire up lane name editing
  container.querySelectorAll(".box-lane-name").forEach(input => {
    input.addEventListener("input", (e) => {
      const index = Number(e.target.dataset.boxIndex);
      canvasState.boxes[index].laneName = e.target.value;
    });
  });

  // Wire up save scan button
  document.getElementById("saveScanButton")?.addEventListener("click", () => saveScan(blotId));
}

function renderSavedScans(blotId) {
  const scans = blotState.scans[blotId] || [];
  if (!scans.length) return `<p class="blot-empty-state" style="margin-top:12px;">No session scans yet.</p>`;

  return `
    <div class="saved-scans">
      <p class="eyebrow" style="margin: 12px 0 8px;">Session scans</p>
      ${scans.map((scan, index) => `
        <div class="saved-scan-item">
          <span class="scan-protein">${escapeHtml(scan.proteinName)}</span>
          <span class="scan-meta">${scan.lanes.length} lanes · ${scan.channel}nm · ${scan.backgroundAxis}</span>
          <button class="ghost-button box-button danger" type="button"
            data-scan-delete="${index}">Delete</button>
        </div>
      `).join("")}
    </div>
  `;
}

function formatBoxArea(box) {
  const width = Math.max(1, Math.round(Math.abs(Number(box.w) || 0)));
  const height = Math.max(1, Math.round(Math.abs(Number(box.h) || 0)));
  return width * height;
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
    id: window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    proteinName,
    channel,
    backgroundAxis,
    lanes: canvasState.boxes.map((box, index) => ({
      name: box.laneName || `Lane ${index + 1}`,
      signal: box.signal.adjustedSignal,
    })),
  };

  if (!blotState.scans[blotId]) blotState.scans[blotId] = [];
  blotState.scans[blotId].push(scan);

  // Clear boxes for next scan
  canvasState.boxes = [];
  renderCanvas();
  renderBoxList(blotId);

  // Refresh quantification dropdowns if they exist
  refreshBlotSourceDropdowns();
}

async function deleteScan(blotId, scanIndex) {
  if (!blotState.scans[blotId]) return;
  blotState.scans[blotId].splice(scanIndex, 1);
  renderBoxList(blotId);
  refreshBlotSourceDropdowns();
}

function moveBox(index, direction, blotId) {
  const newIndex = index + direction;
  if (newIndex < 0 || newIndex >= canvasState.boxes.length) return;
  const boxes = canvasState.boxes;
  [boxes[index], boxes[newIndex]] = [boxes[newIndex], boxes[index]];
  renderCanvas();
  renderBoxList(blotId);
}

function deleteBox(index, blotId) {
  canvasState.boxes.splice(index, 1);
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
    const data = await apiJson(apiUrl("/extract"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: activeSessionId,
        blot: blotById(blotId),
        boxes: boxes.map(box => ({ x: box.x, y: box.y, w: box.w, h: box.h })),
        channel,
        backgroundAxis,
      }),
    }, "Signal extraction failed.");

    if (canvasState.currentBlotId !== blotId) return;
    data.results.forEach((result, index) => {
      const request = requests[index];
      if (!request || !canvasState.boxes.includes(request.box)) return;
      if (request.box.signalRequestId !== request.requestId) return;
      request.box.signal = result;
      request.box.signalStatus = "complete";
    });

    renderBoxList(blotId);
  } catch (error) {
    if (canvasState.currentBlotId !== blotId) return;
    requests.forEach(({ box, requestId }) => {
      if (!canvasState.boxes.includes(box) || box.signalRequestId !== requestId) return;
      box.signal = null;
      box.signalStatus = "error";
      box.signalError = error.message;
    });
    renderBoxList(blotId);
    if (alertOnError) alert(`Signal extraction failed: ${error.message}`);
  }
}

// ─── Data Set Conversion ─────────────────────────────────────────────────────────────
function scanToDataset(blotId, scanIndex) {
  const scan = blotState.scans[blotId]?.[scanIndex];
  if (!scan) return null;

  const rows = scan.lanes.map(lane => ({
    Name: lane.name,
    Signal: lane.signal,
  }));

  return {
    workbook: {
      sheetNames: ["Data"],
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

function canvasToBase64(canvas) {
  return canvas.toDataURL("image/jpeg", 0.92);
}

async function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

async function compositeToBase64(blotId, defaultColorMode = "grayscale") {
  return blobToBase64(await renderCompositeBlob(blotId, defaultColorMode));
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
    const slides = [];

    // ── Slide 1: all blot images ──────────────────────────────────────────────
    const imageSlide = { type: "images", images: [] };

    for (let i = 0; i < state.sharedAnalyses.length; i++) {
      const blotSelect = document.querySelector(`[data-blot-source-blot="sample-${i}"]`);
      const scanSelect = document.querySelector(`[data-blot-source-scan="sample-${i}"]`);
      const usingBlot = blotSelect && !document.getElementById(`sampleBlot${i}`)?.hidden;

      if (usingBlot && blotSelect?.value) {
        const b64 = await compositeToBase64(blotSelect.value, "grayscale");
        const analysis = state.sharedAnalyses[i];
        imageSlide.images.push({
          image: b64,
          label: `anti-${analysis.name}`,
        });
      }
    }

    // Control blot image
    const controlBlotSelect = document.querySelector(`[data-blot-source-blot="control-0"]`);
    const controlUsingBlot = controlBlotSelect && !document.getElementById("controlBlot0")?.hidden;
    if (controlUsingBlot && controlBlotSelect?.value) {
      const b64 = await compositeToBase64(controlBlotSelect.value, "grayscale");
      const controlScanSelect = document.querySelector(`[data-blot-source-scan="control-0"]`);
      const scan = blotState.scans[controlBlotSelect.value]?.[Number(controlScanSelect?.value)];
      imageSlide.images.push({
        image: b64,
        label: `anti-${scan?.proteinName || "Loading control"}`,
      });
    }

    if (imageSlide.images.length) slides.push(imageSlide);

    // ── Slides 2+: one graph slide per sample analysis ────────────────────────
    for (let i = 0; i < state.sharedAnalyses.length; i++) {
      const analysis = state.sharedAnalyses[i];
      const graphs = [];

      // Main fold change chart — render to offscreen canvas
      const mainCanvas = document.createElement("canvas");
      mainCanvas.width  = 960;
      mainCanvas.height = 520;
      drawBarChart(mainCanvas, analysis.results, analysis.title);
      graphs.push(canvasToBase64(mainCanvas));

      // Grouped graphs if enabled
      if (els.enableGroupedGraphs.checked) {
        const prevActive = state.activeSampleIndex;
        state.activeSampleIndex = i;
        const groups = buildGroupedRows(analysis);
        state.activeSampleIndex = prevActive;

        for (const group of groups) {
          const groupCanvas = document.createElement("canvas");
          groupCanvas.width  = 640;
          groupCanvas.height = 360;
          const baseline = group.rows[0]?.foldChange || 1;
          drawBarChart(
            groupCanvas,
            group.rows.map(row => ({ ...row, foldChange: row.foldChange / baseline })),
            group.name
          );
          graphs.push(canvasToBase64(groupCanvas));
        }
      }

      slides.push({
        type: "graphs",
        title: analysis.title,
        graphs,
      });
    }

    // ── Send to backend ───────────────────────────────────────────────────────
    const response = await apiFetch(apiUrl("/generate-pptx"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slides }),
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.error || "Failed to generate PowerPoint");
    }

    const blob = await response.blob();
    const url  = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href     = url;
    link.download = "western-blot-analysis.pptx";
    link.click();
    URL.revokeObjectURL(url);

  } catch (error) {
    alert(`PowerPoint generation failed: ${error.message}`);
  } finally {
    if (button) {
      button.textContent = "Export PowerPoint";
      button.disabled = false;
    }
  }
}
