'use strict';
const $ = id => document.getElementById(id);
let socket, ready = false, recording = false, pending = false, received = 0;
const history = [];
const axes = ['ax', 'ay', 'az', 'gx', 'gy', 'gz'];
axes.forEach(axis => { const item = document.createElement('div'); item.textContent = axis.toUpperCase() + ' '; const value = document.createElement('strong'); value.id = axis; value.textContent = '—'; item.append(value); $('values').append(item); });
function controls() {
  $('start').disabled = !ready || recording || pending;
  $('stop').disabled = !ready || !recording || pending;
  $('metadata').disabled = !ready || recording || pending;
}
function connect() {
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onopen = () => { received = 0; history.length = 0; $('connection').textContent = 'Connected'; };
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === 'status') {
      ready = true; pending = false; recording = data.recording;
      $('recording').textContent = recording ? '● Recording' : '○ Idle';
      $('saved').textContent = data.recorded_samples;
      $('message').textContent = data.error || (recording ? 'Recording on the backend. Stop when the trial is complete.' : 'Ready. Fill in trial details to begin.');
      $('file').textContent = data.file ? `Backend file: recordings/${data.file}` : '';
      if (recording && data.metadata) Object.entries(data.metadata).forEach(([key, value]) => { $(key).value = value; });
      controls();
    } else if (data.type === 'sample') {
      received++; $('received').textContent = received; $('saved').textContent = data.recorded_samples;
      axes.forEach(axis => { $(axis).textContent = data[axis].toFixed(3); });
      history.push(data); if (history.length > 250) history.shift();
      draw();
    } else if (data.type === 'error') {
      pending = false; $('message').textContent = data.message; controls();
    }
  };
  socket.onclose = () => { ready = false; pending = false; controls(); $('connection').textContent = 'Disconnected · retrying…'; $('recording').textContent = 'Recording status unknown'; $('message').textContent = 'Connection lost. An active backend recording continues; reconnect to check it.'; setTimeout(connect, 1500); };
  socket.onerror = () => socket.close();
}
function command(type, metadata) {
  if (!ready || socket.readyState !== WebSocket.OPEN) return;
  pending = true; controls(); socket.send(JSON.stringify({type, metadata}));
}
$('trial-form').onsubmit = event => { event.preventDefault(); command('start_recording', { participant: $('participant').value.trim(), session: $('session').value.trim(), gesture: $('gesture').value.trim(), trial: Number($('trial').value) }); };
$('stop').onclick = () => command('stop_recording');
function draw() {
  const canvas = $('plot'), ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h); ctx.strokeStyle = '#dce3e7'; ctx.lineWidth = 1;
  for (let y = 0; y <= 4; y++) { ctx.beginPath(); ctx.moveTo(0, y * h / 4); ctx.lineTo(w, y * h / 4); ctx.stroke(); }
  ['ax', 'ay', 'az'].forEach((axis, i) => { ctx.strokeStyle = ['#c75c42', '#168b80', '#4475c6'][i]; ctx.lineWidth = 2; ctx.beginPath(); history.forEach((sample, j) => { const x = j / 249 * w, y = h - (sample[axis] + 2) / 14 * h; if (!j) ctx.moveTo(x, y); else ctx.lineTo(x, y); }); ctx.stroke(); });
}
controls(); connect();
