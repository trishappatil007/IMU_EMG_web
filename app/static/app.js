'use strict';
const $ = id => document.getElementById(id);
let socket, ready = false, phase = 'idle', pending = false, received = 0, lastEmgSequence = null, source = 'simulated', imuRate = 50, piReceiving = false;
const imuHistory = [], emgHistory = [], axes = ['ax', 'ay', 'az', 'gx', 'gy', 'gz'];
const metadataFields = ['participant_id', 'session_id', 'gesture', 'trial_id', 'mounting_location', 'orientation_notes', 'accelerometer_range', 'gyroscope_range', 'trial_notes'];
const colors = ['#c75c42', '#168b80', '#4475c6', '#9f67aa'];
let filters = [];
function resetFilters() {
  filters = Array.from({length:4}, () => {
    const stages=[];
    if ($('bandpass').checked) stages.push(SignalMath.biquad('highpass',20,1000), SignalMath.biquad('lowpass',200,1000));
    if ($('notch').checked) stages.push(SignalMath.biquad('notch',60,1000,30));
    return stages;
  });
  emgHistory.length = 0;
}
$('bandpass').onchange = $('notch').onchange = resetFilters;
axes.forEach(axis => { const item = document.createElement('div'); item.textContent = axis.toUpperCase() + ' '; const value = document.createElement('strong'); value.id = axis; value.textContent = '—'; item.append(value); $('values').append(item); });
function controls() {
  $('start').disabled = !ready || phase !== 'idle' || pending || (source === 'pi' && !piReceiving);
  $('source_select').disabled = !ready || phase !== 'idle' || pending;
  $('stop').disabled = !ready || !['countdown', 'recording'].includes(phase) || pending;
  $('stop').textContent = phase === 'countdown' ? 'Cancel countdown' : 'Stop recording';
  $('metadata').disabled = !ready || phase !== 'idle' || pending;
  $('accept').disabled = $('reject').disabled = !ready || phase !== 'review' || pending;
  $('review-actions').hidden = phase !== 'review';
}
function connect() {
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onopen = () => { received = 0; imuHistory.length = 0; lastEmgSequence = null; resetFilters(); $('connection').textContent = 'Connected'; };
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === 'status') {
      ready = true; pending = false; phase = data.phase;
      if (source !== data.source) { imuHistory.length = 0; resetFilters(); }
      source = data.source; piReceiving = data.device_receiving; imuRate = data.sample_rate_hz || 50;
      $('source_select').value = source; $('pi_status').textContent = data.device_connected ? `Pi: connected${piReceiving ? ' · receiving' : ' · no recent samples'}` : 'Pi: disconnected';
      $('source_status').textContent = source === 'pi' ? `Device stream: ${data.device_id || 'disconnected'} / ${data.sensor_model}` : 'Source: simulated IMU + EMG';
      $('emg_panel').hidden = source === 'pi'; $('elapsed').textContent = `${data.elapsed_seconds.toFixed(1)} s`;
      if (data.prediction) showPrediction(data.prediction); else $('prediction').textContent = 'Prediction: unavailable';
      $('recording').textContent = ({idle:'○ Idle', countdown:`Starting in ${data.countdown_remaining}…`, recording:'● Recording', review:'○ Review trial'})[phase];
      $('saved').textContent = data.recorded_samples;
      const messages = {idle:'Ready. Fill in trial details to begin.', countdown:'Get into the starting pose.', recording:'Recording on the backend. Stop when the trial is complete.', review:'Accept this trial or reject it and repeat with the same details.'};
      $('message').textContent = data.error || messages[phase];
      if (phase === 'review') renderQuality(data.emg_quality);
      if (phase === 'countdown') $('override-reason').value = '';
      $('file').textContent = data.file ? `Backend files: ${[data.file, data.emg_file, data.metadata_file].filter(Boolean).join(', ')}` : '';
      if (phase !== 'idle' && data.metadata) metadataFields.forEach(key => { $(key).value = data.metadata[key] ?? ''; });
      controls();
    } else if (data.type === 'prediction') {
      showPrediction(data);
    } else if (data.type === 'sample') {
      received++; $('received').textContent = received; $('saved').textContent = data.recorded_samples;
      axes.forEach(axis => { $(axis).textContent = data[axis].toFixed(3); });
      imuHistory.push(data); if (imuHistory.length > 500) imuHistory.shift();
    } else if (data.type === 'emg_batch') {
      if (lastEmgSequence !== null && data.first_sequence !== lastEmgSequence) resetFilters();
      lastEmgSequence = data.first_sequence + data.samples.length;
      data.samples.forEach(sample => {
        const processed=sample.values.map((value,i)=>filters[i].reduce((v,filter)=>filter(v),value));
        emgHistory.push(processed);
      });
      if (emgHistory.length > 10000) emgHistory.splice(0,emgHistory.length-10000);
    } else if (data.type === 'error') {
      pending = false; $('message').textContent = data.message; controls();
    }
  };
  socket.onclose = () => { ready = false; pending = false; controls(); $('connection').textContent = 'Disconnected · retrying…'; $('recording').textContent = 'Recording status unknown'; $('message').textContent = 'Connection lost. An active backend trial continues; reconnect to check it.'; setTimeout(connect, 1500); };
  socket.onerror = () => socket.close();
}
function renderQuality(report) {
  const checks=$('quality-checks'), channels=$('quality-channels');
  checks.replaceChildren(); channels.replaceChildren();
  if (!report) { $('quality-summary').textContent='Quality report unavailable.'; $('override-label').hidden=false; return; }
  const label = {pass:'Provisional checks passed', review:'Needs review', 'not assessed':'Not assessed', 'not collected':'EMG not collected'}[report.status] || 'Not assessed';
  $('quality-summary').textContent = `${label}${report.config ? ` · ${report.config.name} · ${report.config.units}` : ''}`;
  $('quality-summary').className = `quality-summary-${report.status.replaceAll(' ', '-')}`;
  const addRow = (container, item, name, detail) => {
    const row=document.createElement('div'), dot=document.createElement('span'), text=document.createElement('span');
    row.className='quality-row'; dot.className=`quality-dot quality-dot-${item.status.replaceAll(' ', '-')}`; dot.setAttribute('aria-hidden','true');
    text.textContent=`${name}: ${item.status.toUpperCase()} — ${detail}`;
    row.append(dot,text);container.append(row);
  };
  (report.checks || []).forEach(check=>addRow(checks,check,check.name,check.detail));
  (report.channels || []).forEach(ch=>{
    const metrics = ch.std === null ? '' : ` SD ${ch.std.toFixed(4)}, RMS ${ch.rms.toFixed(4)}`;
    addRow(channels,ch,ch.channel,ch.detail+metrics);
  });
  $('quality-limitations').textContent=(report.limitations || []).join(' ');
  $('override-label').hidden = !['review','not assessed'].includes(report.status);
}
function showPrediction(data) { $('prediction').textContent = `Prediction: ${data.gesture} · ${(data.confidence * 100).toFixed(1)}% (${data.source}${data.device_id ? ` / ${data.device_id}` : ""})`; }
function command(payload) { if (!ready || socket.readyState !== WebSocket.OPEN) return; pending = true; controls(); socket.send(JSON.stringify(payload)); }
$('trial-form').onsubmit = event => {
  event.preventDefault(); const metadata = Object.fromEntries(metadataFields.map(key => [key, $(key).value.trim()]));
  metadata.trial_id = Number(metadata.trial_id);
  command({type:'start_recording', metadata, countdown_seconds:Number($('countdown_seconds').value)});
};
$('source_select').onchange = () => command({type:'set_source', source:$('source_select').value});
$('stop').onclick = () => command({type:'stop_recording'});
$('accept').onclick = () => command({type:'review_trial', decision:'accepted', override_reason:$('override-reason').value.trim()});
$('reject').onclick = () => command({type:'review_trial', decision:'rejected'});
function positive(id, fallback) { const n=Number($(id).value); return Number.isFinite(n) && n>0 ? n : fallback; }
function drawLines(id, lines, limit, span, units, seconds) {
  const c=$(id),ctx=c.getContext('2d'), w=c.width,h=c.height, left=52, bottom=24, top=12, width=w-left-5, height=h-top-bottom;
  ctx.clearRect(0,0,w,h); ctx.font='12px system-ui'; ctx.fillStyle='#60747a'; ctx.strokeStyle='#dce3e7';
  for(let i=0;i<=4;i++){const y=top+height*i/4; ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-5,y);ctx.stroke();ctx.fillText((limit*(1-i/2)).toFixed(1),0,y+4);}
  ctx.fillText(`-${seconds}s`,left,h-4);ctx.fillText('now',w-35,h-4);ctx.fillText(units,0,h-4);
  ctx.save();ctx.beginPath();ctx.rect(left,top,width,height);ctx.clip();
  lines.forEach(({points,color})=>{ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();points.forEach((v,i)=>{const x=left+width*(1-(points.length-1-i)/(span-1)), y=top+height*(.5-v/(2*limit)); if(!i)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();});ctx.restore();
}
function drawSpectrum() {
  const canvas=$('spectrum_plot'),ctx=canvas.getContext('2d'), n=256, rate=1000;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if (emgHistory.length<n) { $('spectrum_status').textContent='Waiting for 256 continuous EMG samples…'; return; }
  const channel=Number($('spectrum_channel').value), spec=SignalMath.spectrum(emgHistory.slice(-n).map(x=>x[channel]),rate);
  const maximum=Math.max(...spec.map(p=>p.amplitude),0.001), w=canvas.width,h=canvas.height,left=45, bottom=25;
  ctx.fillStyle='#60747a';ctx.font='12px system-ui';ctx.fillText(maximum.toFixed(2),0,15);ctx.fillText('0',left,h-5);ctx.fillText('250 Hz',w/2-15,h-5);ctx.fillText('500 Hz',w-55,h-5);
  ctx.strokeStyle=colors[channel];ctx.lineWidth=2;ctx.beginPath();spec.forEach((p,i)=>{const x=left+(w-left-5)*p.frequency/500,y=(h-bottom)*(1-p.amplitude/maximum);if(!i)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();
  $('spectrum_status').textContent=`EMG ${channel+1} · latest 256 samples · Hann window · amplitude (simulation units), auto-scaled`;
}
function draw() {
  const seconds=positive('window',5), selected=new Set([...document.querySelectorAll('.channel-options input[value]:checked')].map(e=>e.value));
  const imu=imuHistory.slice(-Math.ceil(seconds*imuRate)), emg=emgHistory.slice(-seconds*1000), count=Number($('emg_channels').value);
  drawLines('accel_plot',['ax','ay','az'].filter(k=>selected.has(k)).map(k=>({points:imu.map(v=>v[k]),color:colors[axes.indexOf(k)%3]})),positive('accel_scale',12),Math.max(2,seconds*imuRate),'m/s²',seconds);
  drawLines('gyro_plot',['gx','gy','gz'].filter(k=>selected.has(k)).map(k=>({points:imu.map(v=>v[k]),color:colors[axes.indexOf(k)%3]})),positive('gyro_scale',1),Math.max(2,seconds*imuRate),'rad/s',seconds);
  drawLines('emg_plot',Array.from({length:count},(_,i)=>({points:emg.map(v=>v[i]),color:colors[i]})),positive('emg_scale',2),seconds*1000,'a.u.',seconds);
  drawSpectrum();
}
resetFilters();controls();connect();setInterval(draw,250);
setInterval(() => { if (ready && socket.readyState === WebSocket.OPEN && !pending) socket.send(JSON.stringify({type:'get_status'})); }, 1000);

