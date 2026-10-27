'use strict';
const $ = id => document.getElementById(id);
let socket, ready = false, phase = 'idle', pending = false, received = 0;
const history = [];
const axes = ['ax', 'ay', 'az', 'gx', 'gy', 'gz'];
const metadataFields = ['participant_id', 'session_id', 'gesture', 'trial_id', 'mounting_location', 'orientation_notes', 'accelerometer_range', 'gyroscope_range', 'trial_notes'];
axes.forEach(axis => { const item = document.createElement('div'); item.textContent = axis.toUpperCase() + ' '; const value = document.createElement('strong'); value.id = axis; value.textContent = '—'; item.append(value); $('values').append(item); });
function controls() {
  $('start').disabled = !ready || phase !== 'idle' || pending;
  $('stop').disabled = !ready || !['countdown', 'recording'].includes(phase) || pending;
  $('stop').textContent = phase === 'countdown' ? 'Cancel countdown' : 'Stop recording';
  $('metadata').disabled = !ready || phase !== 'idle' || pending;
  $('accept').disabled = $('reject').disabled = !ready || phase !== 'review' || pending;
  $('review-actions').hidden = phase !== 'review';
}
function connect() {
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onopen = () => { received = 0; history.length = 0; $('connection').textContent = 'Connected'; };
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === 'status') {
      ready = true; pending = false; phase = data.phase;
      $('recording').textContent = ({idle:'○ Idle', countdown:`Starting in ${data.countdown_remaining}…`, recording:'● Recording', review:'○ Review trial'})[phase];
      $('saved').textContent = data.recorded_samples;
      const messages = {idle:'Ready. Fill in trial details to begin.', countdown:'Get into the starting pose.', recording:'Recording on the backend. Stop when the trial is complete.', review:'Accept this trial or reject it and repeat with the same details.'};
      $('message').textContent = data.error || messages[phase];
      $('file').textContent = data.file ? `Backend files: ${data.file}, ${data.metadata_file}` : '';
      if (phase !== 'idle' && data.metadata) metadataFields.forEach(key => { $(key).value = data.metadata[key] ?? ''; });
      controls();
    } else if (data.type === 'sample') {
      received++; $('received').textContent = received; $('saved').textContent = data.recorded_samples;
      axes.forEach(axis => { $(axis).textContent = data[axis].toFixed(3); });
      history.push(data); if (history.length > 250) history.shift(); draw();
    } else if (data.type === 'error') {
      pending = false; $('message').textContent = data.message; controls();
    }
  };
  socket.onclose = () => { ready = false; pending = false; controls(); $('connection').textContent = 'Disconnected · retrying…'; $('recording').textContent = 'Recording status unknown'; $('message').textContent = 'Connection lost. An active backend trial continues; reconnect to check it.'; setTimeout(connect, 1500); };
  socket.onerror = () => socket.close();
}
function command(payload) {
  if (!ready || socket.readyState !== WebSocket.OPEN) return;
  pending = true; controls(); socket.send(JSON.stringify(payload));
}
$('trial-form').onsubmit = event => {
  event.preventDefault();
  const metadata = Object.fromEntries(metadataFields.map(key => [key, $(key).value.trim()]));
  metadata.trial_id = Number(metadata.trial_id);
  command({type:'start_recording', metadata, countdown_seconds:Number($('countdown_seconds').value)});
};
$('stop').onclick = () => command({type:'stop_recording'});
$('accept').onclick = () => command({type:'review_trial', decision:'accepted'});
$('reject').onclick = () => command({type:'review_trial', decision:'rejected'});
function draw() {
  const canvas = $('plot'), ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h); ctx.strokeStyle = '#dce3e7'; ctx.lineWidth = 1;
  for (let y = 0; y <= 4; y++) { ctx.beginPath(); ctx.moveTo(0, y * h / 4); ctx.lineTo(w, y * h / 4); ctx.stroke(); }
  ['ax', 'ay', 'az'].forEach((axis, i) => { ctx.strokeStyle = ['#c75c42', '#168b80', '#4475c6'][i]; ctx.lineWidth = 2; ctx.beginPath(); history.forEach((sample, j) => { const x = j / 249 * w, y = h - (sample[axis] + 2) / 14 * h; if (!j) ctx.moveTo(x, y); else ctx.lineTo(x, y); }); ctx.stroke(); });
}
controls(); connect();
