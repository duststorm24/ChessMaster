const socket = io();

const statusLine = document.getElementById("statusLine");
const logEl = document.getElementById("log");
const el = (id) => document.getElementById(id);

const AXES = ["X","Y","Z"];

let state = {
  axes: { X:{pos:null,homed:false}, Y:{pos:null,homed:false}, Z:{pos:null,homed:false} },
  soft_limits: { X:-10500, Y:-49500, Z:-39000 },
  motors_enabled: null,
  estop_latched: null,
  server_time: null
};

let lastStateMs = Date.now();

// Avoid overwriting multi-target inputs while user is editing
let multiEditing = false;
["multiX","multiY","multiZ"].forEach(id => {
  const n = el(id);
  if (!n) return;
  n.addEventListener("focus", () => multiEditing = true);
  n.addEventListener("blur", () => multiEditing = false);
});

function clamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }

// Slider mapping: slider value 0..abs(min). 0 => pos 0, max => pos = min (negative).
function sliderToPos(axis, sliderVal){
  const min = state.soft_limits[axis]; // negative
  const maxAbs = Math.abs(min);
  const v = clamp(parseInt(sliderVal,10)||0, 0, maxAbs);
  return -v;
}
function posToSlider(axis, pos){
  if (pos === null || pos === undefined) return 0;
  return clamp(Math.abs(pos), 0, Math.abs(state.soft_limits[axis]));
}

function fmtPos(p){
  if (p === null || p === undefined) return "?";
  return String(p);
}

function setHomedPill(axis, homed){
  const pill = el("homed"+axis);
  if (!pill) return;
  if (homed){
    pill.textContent = "HOMED";
    pill.style.borderColor = "rgba(55,214,255,.45)";
    pill.style.color = "rgba(230,237,247,.9)";
  } else {
    pill.textContent = "NOT HOMED";
    pill.style.borderColor = "rgba(255,255,255,.12)";
    pill.style.color = "rgba(138,161,189,.9)";
  }
}

function updateAxisUI(axis){
  const a = state.axes[axis];
  el("pos"+axis).textContent = fmtPos(a.pos);
  setHomedPill(axis, a.homed);

  const slider = el("slider"+axis);
  slider.max = String(Math.abs(state.soft_limits[axis]));

  // Keep slider synced to current pos (if known)
  if (a.pos !== null && a.pos !== undefined){
    slider.value = String(posToSlider(axis, a.pos));
  }

  const target = sliderToPos(axis, slider.value);
  el("target"+axis).textContent = `target: ${target}`;

  const disable = !a.homed || (a.pos === null || a.pos === undefined);
  el("jog"+axis+"Neg").disabled = disable;
  el("jog"+axis+"Pos").disabled = disable;
  el("apply"+axis).disabled = disable;
  slider.disabled = disable;
}

function updateAllUI(){
  AXES.forEach(updateAxisUI);

  const en = state.motors_enabled;
  const es = state.estop_latched;

  let s = "Connected";
  if (es === true) s = "E-STOP LATCHED";
  else if (en === true) s = "Motors Enabled";
  else if (en === false) s = "Motors De-energized";

  statusLine.textContent = s;
  statusLine.style.color = (es === true) ? "rgba(255,59,92,.9)" : "rgba(138,161,189,.95)";

  // Only sync multi-target inputs when user is NOT editing them
  if (!multiEditing){
    if (state.axes.X.pos !== null) el("multiX").value = state.axes.X.pos;
    if (state.axes.Y.pos !== null) el("multiY").value = state.axes.Y.pos;
    if (state.axes.Z.pos !== null) el("multiZ").value = state.axes.Z.pos;
  }
}

// ---------------- Socket events ----------------
socket.on("connect", () => {
  statusLine.textContent = "Connected";
  socket.emit("watch", { on: true });
  socket.emit("pos");
});

socket.on("log", (data) => {
  const line = data.line || "";
  logEl.textContent += line + "\n";
  logEl.scrollTop = logEl.scrollHeight;
});

socket.on("state", (data) => {
  state = data;
  lastStateMs = Date.now();
  updateAllUI();
});

// If state gets stale, ask for POS (safety net)
setInterval(() => {
  if (Date.now() - lastStateMs > 600) {
    socket.emit("pos");
  }
}, 250);

// ---------------- Top buttons ----------------
el("btnEStop").onclick = () => socket.emit("estop");
el("btnClear").onclick = () => socket.emit("clear_estop");
el("btnEnergize").onclick = () => socket.emit("energize");
el("btnDeenergize").onclick = () => socket.emit("deenergize");
el("btnHomeAll").onclick = () => socket.emit("home_all");
el("btnLimits").onclick = () => socket.emit("limits");

el("btnSpeed").onclick = () => {
  socket.emit("set_speed", {
    vx: parseInt(el("vx").value,10) || 800,
    vy: parseInt(el("vy").value,10) || 3000,
    vz: parseInt(el("vz").value,10) || 3000,
  });
};

// ---------------- Axis widgets ----------------
AXES.forEach(axis => {
  const slider = el("slider"+axis);

  slider.addEventListener("input", () => {
    const t = sliderToPos(axis, slider.value);
    el("target"+axis).textContent = `target: ${t}`;
  });

  el("home"+axis).onclick = () => socket.emit("home_axis", {axis});

  el("jog"+axis+"Neg").onclick = () => {
    const step = parseInt(el("step"+axis).value,10) || 200;
    socket.emit("jog", {axis, steps: -step});
  };

  el("jog"+axis+"Pos").onclick = () => {
    const step = parseInt(el("step"+axis).value,10) || 200;
    socket.emit("jog", {axis, steps: +step});
  };

  // Move axis to slider target (absolute) using MOVE dx dy dz (delta)
  el("apply"+axis).onclick = () => {
    const a = state.axes[axis];
    if (!a.homed || a.pos === null) return;

    const target = sliderToPos(axis, slider.value);
    const delta = target - a.pos;

    // If already there, do nothing
    if (delta === 0) return;

    const payload = {dx:0, dy:0, dz:0};
    if (axis === "X") payload.dx = delta;
    if (axis === "Y") payload.dy = delta;
    if (axis === "Z") payload.dz = delta;

    socket.emit("move_delta", payload);

    // Backend does optimistic update too, but this makes UI instant even if state packets lag.
    if (axis === "X") state.axes.X.pos = clamp(state.axes.X.pos + payload.dx, state.soft_limits.X, 0);
    if (axis === "Y") state.axes.Y.pos = clamp(state.axes.Y.pos + payload.dy, state.soft_limits.Y, 0);
    if (axis === "Z") state.axes.Z.pos = clamp(state.axes.Z.pos + payload.dz, state.soft_limits.Z, 0);
    updateAllUI();
  };
});

// ---------------- Multi-axis Go (absolute targets) ----------------
el("btnMoveAll").onclick = () => {
  for (const ax of AXES){
    if (!state.axes[ax].homed || state.axes[ax].pos === null){
      alert("Home all axes first (positions must be known).");
      return;
    }
  }

  let tx = parseInt(el("multiX").value,10);
  let ty = parseInt(el("multiY").value,10);
  let tz = parseInt(el("multiZ").value,10);

  tx = clamp(tx, state.soft_limits.X, 0);
  ty = clamp(ty, state.soft_limits.Y, 0);
  tz = clamp(tz, state.soft_limits.Z, 0);

  const dx = tx - state.axes.X.pos;
  const dy = ty - state.axes.Y.pos;
  const dz = tz - state.axes.Z.pos;

  if (dx === 0 && dy === 0 && dz === 0) return;

  socket.emit("move_delta", {dx, dy, dz});

  // optimistic UI update
  state.axes.X.pos = clamp(state.axes.X.pos + dx, state.soft_limits.X, 0);
  state.axes.Y.pos = clamp(state.axes.Y.pos + dy, state.soft_limits.Y, 0);
  state.axes.Z.pos = clamp(state.axes.Z.pos + dz, state.soft_limits.Z, 0);
  updateAllUI();
};

// ---------------- Saved positions (browser localStorage) ----------------
function keyFor(i){ return `arm_pos_${i}`; }

function renderSlot(i){
  const v = localStorage.getItem(keyFor(i));
  const elc = el(`p${i}coords`);
  if (!v){ elc.textContent = "—"; return; }
  const obj = JSON.parse(v);
  elc.textContent = `X=${obj.X}, Y=${obj.Y}, Z=${obj.Z}`;
}

function saveSlot(i){
  for (const ax of AXES){
    if (!state.axes[ax].homed || state.axes[ax].pos === null){
      alert("Home all axes before saving a position.");
      return;
    }
  }
  const obj = {X: state.axes.X.pos, Y: state.axes.Y.pos, Z: state.axes.Z.pos};
  localStorage.setItem(keyFor(i), JSON.stringify(obj));
  renderSlot(i);
}

function clearSlot(i){
  localStorage.removeItem(keyFor(i));
  renderSlot(i);
}

function goSlot(i){
  const v = localStorage.getItem(keyFor(i));
  if (!v){ alert("That slot is empty."); return; }
  const obj = JSON.parse(v);

  for (const ax of AXES){
    if (!state.axes[ax].homed || state.axes[ax].pos === null){
      alert("Home all axes first.");
      return;
    }
  }

  const tx = clamp(obj.X, state.soft_limits.X, 0);
  const ty = clamp(obj.Y, state.soft_limits.Y, 0);
  const tz = clamp(obj.Z, state.soft_limits.Z, 0);

  const dx = tx - state.axes.X.pos;
  const dy = ty - state.axes.Y.pos;
  const dz = tz - state.axes.Z.pos;

  if (dx === 0 && dy === 0 && dz === 0) return;

  socket.emit("move_delta", {dx, dy, dz});

  // optimistic UI update
  state.axes.X.pos = clamp(state.axes.X.pos + dx, state.soft_limits.X, 0);
  state.axes.Y.pos = clamp(state.axes.Y.pos + dy, state.soft_limits.Y, 0);
  state.axes.Z.pos = clamp(state.axes.Z.pos + dz, state.soft_limits.Z, 0);
  updateAllUI();
}

[1,2,3,4].forEach(i => {
  el(`p${i}save`).onclick = () => saveSlot(i);
  el(`p${i}go`).onclick = () => goSlot(i);
  el(`p${i}clear`).onclick = () => clearSlot(i);
  renderSlot(i);
});
