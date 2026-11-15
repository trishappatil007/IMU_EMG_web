'use strict';
const $ = id => document.getElementById(id);
let socket, ready=false, pending=false, phase='idle', source='simulated', mudraReceiving=false;
let lastSequence=null, filters=[], sourceRate=1000;
const history=[], colors=['#c75c42','#168b80','#4475c6','#9f67aa'];
const metadataFields=['participant_id','session_id','gesture'];

function resetFilters() {
  filters=Array.from({length:4},()=>{
    const stages=[];
    if(source==='simulated' && $('bandpass').checked) stages.push(SignalMath.biquad('highpass',20,1000),SignalMath.biquad('lowpass',200,1000));
    if(source==='simulated' && $('notch').checked) stages.push(SignalMath.biquad('notch',60,1000,30));
    return stages;
  });
  history.length=0;
}
$('bandpass').onchange=$('notch').onchange=resetFilters;
function controls(){
  $('run').disabled=!ready || phase!=='idle' || pending || (source==='mudra' && !mudraReceiving);
  $('source_select').disabled=!ready || phase!=='idle' || pending;
  $('metadata').disabled=!ready || phase!=='idle' || pending;
  $('destination_settings').disabled=!ready || !['idle','review'].includes(phase) || pending;
  $('stop').disabled=!ready || !['countdown','recording'].includes(phase) || pending;
  $('stop').textContent=phase==='countdown' ? 'Cancel countdown' : 'Stop';
  $('accept').disabled=$('reject').disabled=!ready || phase!=='review' || pending;
  $('review-actions').hidden=phase!=='review';
}
function command(value){
  if(!ready || socket.readyState!==WebSocket.OPEN) return;
  pending=true; controls(); socket.send(JSON.stringify(value));
}
function selectedDestination(){
  return {kind:$('destination_kind').value,folder:$('local_folder').value.trim(),
          repository:$('github_repo').value.trim(),branch:$('github_branch').value.trim(),path:$('github_path').value.trim()};
}
$('destination_kind').onchange=()=>{
  const gh=$('destination_kind').value==='github';
  $('local-destination').hidden=gh; $('github-destination').hidden=!gh;
  $('github_repo').required=gh; $('github_branch').required=gh;
};
$('source_select').onchange=()=>command({type:'set_source',source:$('source_select').value});
$('trial-form').onsubmit=event=>{
  event.preventDefault();
  const metadata=Object.fromEntries(metadataFields.map(key=>[key,$(key).value.trim()]));
  const value=$('sample_rate_hz').value.trim();
  command({type:'start_recording',metadata,countdown_seconds:3,
           requested_sample_rate_hz:value ? Number(value) : null,destination:selectedDestination()});
};
$('stop').onclick=()=>command({type:'stop_recording'});
$('accept').onclick=()=>command({type:'review_trial',decision:'accepted',
  override_reason:$('override-reason').value.trim(),destination:selectedDestination()});
$('reject').onclick=()=>command({type:'review_trial',decision:'rejected'});

function connect(){
  socket=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
  socket.onopen=()=>{lastSequence=null;resetFilters();$('connection').textContent='Backend connected';};
  socket.onmessage=event=>{
    const data=JSON.parse(event.data);
    if(data.type==='status'){
      const first=!ready, oldSource=source, oldPhase=phase, oldRate=sourceRate;
      ready=true; pending=false; phase=data.phase; source=data.source; sourceRate=data.sample_rate_hz;
      mudraReceiving=!!data.mudra_receiving;
      if(data.destination && (first || (phase==='review' && oldPhase!=='review'))){
        const d=data.destination; $('destination_kind').value=d.kind; $('local_folder').value=d.folder;
        $('github_repo').value=d.repository; $('github_branch').value=d.branch; $('github_path').value=d.path;
        $('destination_kind').onchange();
      }
      if(first || oldSource!==source){
        $('sample_rate_hz').value=sourceRate==null ? '' : String(sourceRate);
        if(source==='mudra'){
          $('bandpass').checked=$('notch').checked=false; $('emg_channels').value='3'; $('emg_scale').value='1000';
          if($('spectrum_channel').value==='3') $('spectrum_channel').value='0';
        } else if(oldSource==='mudra'){
          $('emg_channels').value='2'; $('emg_scale').value='2';
        }
        lastSequence=null;resetFilters();
      } else if(phase==='idle' && sourceRate!=null && sourceRate!==oldRate && !$('sample_rate_hz').value){
        $('sample_rate_hz').value=String(sourceRate);
      }
      $('source_select').value=source;
      $('mudra_status').textContent=data.mudra_connected ? 'Mudra: connected'+(mudraReceiving?' · receiving':' · no recent EMG') : 'Mudra: disconnected';
      $('recording').textContent=({idle:'○ Idle',countdown:'Starting in '+data.countdown_remaining+'…',
        recording:'● Recording',review:'○ Review trial',saving:'Saving approved trial…'})[phase];
      $('elapsed').textContent=data.elapsed_seconds.toFixed(1)+' s';
      $('saved').textContent=data.recorded_samples;
      $('bandpass').disabled=$('notch').disabled=source==='mudra';
      $('emg_four').disabled=$('spectrum_four').disabled=source==='mudra';
      $('emg_scale_label').textContent=source==='mudra'?'Vertical range (± raw counts)':'Vertical range (± simulation units)';
      $('emg_title').textContent=source==='mudra'?'Waveform · raw counts':'Waveform · simulation units';
      $('emg_display_note').textContent=source==='mudra'
        ? 'Three raw Mudra channels. Filters are disabled until the device settings are validated.'
        : 'Raw samples are recorded; display settings do not change saved data.';
      const messages={idle:'Ready. Fill in trial details to begin.',countdown:'Get into the starting pose.',
        recording:'Recording until you press Stop.',review:'Check the signal, then accept or reject the trial.',
        saving:'Saving the approved trial…'};
      $('message').textContent=data.error || messages[phase];
      $('file').textContent=data.export_result ? 'Approved export: '+(data.export_result.url || data.export_result.folder)
        : data.emg_file ? 'Pending files: '+[data.emg_file,data.metadata_file].filter(Boolean).join(', ') : '';
      if(phase==='review') renderQuality(data.emg_quality);
      if(phase==='countdown') $('override-reason').value='';
      if(phase!=='idle' && data.metadata) metadataFields.forEach(key=>{$(key).value=data.metadata[key] ?? '';});
      controls();
    } else if(data.type==='emg_batch' && data.source===source){
      if(lastSequence!==null && data.first_sequence!==lastSequence) resetFilters();
      lastSequence=data.first_sequence+data.samples.length;
      for(const sample of data.samples) history.push(sample.values.map((value,i)=>filters[i].reduce((v,filter)=>filter(v),value)));
      if(history.length>10000) history.splice(0,history.length-10000);
      $('saved').textContent=data.recorded_samples;
    } else if(data.type==='error'){
      pending=false;
      $('message').textContent=data.message;
      if(Array.isArray(data.fields) && data.fields.length){
        const details=data.fields.map(item=>item.field+': '+item.message).join('\\n');
        window.alert('Please check the trial details:\\n\\n'+details);
      } else {
        window.alert(data.message || 'The backend rejected the request.');
      }
      controls();
    }
  };
  socket.onclose=()=>{ready=false;pending=false;controls();$('connection').textContent='Disconnected · retrying…';
    $('message').textContent='Connection lost. The backend may still be recording; reconnect to check.';setTimeout(connect,1500);};
  socket.onerror=()=>socket.close();
}
function renderQuality(report){
  const checks=$('quality-checks'),channels=$('quality-channels');
  checks.replaceChildren();channels.replaceChildren();
  if(!report){$('quality-summary').textContent='Quality report unavailable.';$('override-label').hidden=false;return;}
  const label={pass:'Provisional checks passed',review:'Needs review','not assessed':'Not assessed','not collected':'EMG not collected'}[report.status] || 'Not assessed';
  $('quality-summary').textContent=label+(report.config?' · '+report.config.name+' · '+report.config.units:'');
  $('quality-summary').className='quality-summary-'+report.status.replaceAll(' ','-');
  const addRow=(container,item,name,detail)=>{
    const row=document.createElement('div'),dot=document.createElement('span'),text=document.createElement('span');
    row.className='quality-row';dot.className='quality-dot quality-dot-'+item.status.replaceAll(' ','-');
    dot.setAttribute('aria-hidden','true');text.textContent=name+': '+item.status.toUpperCase()+' — '+detail;
    row.append(dot,text);container.append(row);
  };
  (report.checks||[]).forEach(item=>addRow(checks,item,item.name,item.detail));
  (report.channels||[]).forEach(ch=>addRow(channels,ch,ch.channel,ch.detail+(ch.std==null?'':' SD '+ch.std.toFixed(4)+', RMS '+ch.rms.toFixed(4))));
  $('quality-limitations').textContent=(report.limitations||[]).join(' ');
  $('override-label').hidden=!['review','not assessed'].includes(report.status);
}
$('show-collection').onclick=()=>{
  $('collection-view').hidden=false;$('data-view').hidden=true;
  $('show-collection').setAttribute('aria-pressed','true');$('show-data').setAttribute('aria-pressed','false');
};
$('show-data').onclick=()=>{
  $('collection-view').hidden=true;$('data-view').hidden=false;
  $('show-collection').setAttribute('aria-pressed','false');$('show-data').setAttribute('aria-pressed','true');loadTrials();
};
$('refresh-trials').onclick=loadTrials;
async function loadTrials(){
  const box=$('trials-list');box.textContent='Loading…';
  try{
    const response=await fetch('/api/trials');if(!response.ok)throw Error('Unable to load trials.');
    const {trials}=await response.json();box.replaceChildren();
    if(!trials.length){box.textContent='No trials recorded yet.';return;}
    trials.forEach(t=>{
      const row=document.createElement('div'),label=document.createElement('span');row.className='trial-row';
      label.textContent=[t.participant_id||'—',t.session_id||'—',t.gesture||'—',t.review_status||'unknown',t.source||'unknown'].join(' · ');row.append(label);
      [['EMG CSV',t.emg_csv_file],['Metadata / quality',t.metadata_file]].forEach(([name,file])=>{
        if(!file)return;const a=document.createElement('a');a.href='/api/trials/files/'+encodeURIComponent(file);a.textContent=name;a.download=file;row.append(a);
      });box.append(row);
    });
  }catch(error){box.textContent='Could not load trials: '+error.message;}
}
function positive(id,fallback){const n=Number($(id).value);return Number.isFinite(n)&&n>0?n:fallback;}
function displayRate(){return positive('sample_rate_hz',source==='simulated'?1000:null);}
function drawWave(){
  const canvas=$('emg_plot'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height,left=60,bottom=26,top=12,width=w-left-8,height=h-top-bottom;
  const rate=displayRate(),seconds=positive('window',5),span=rate?Math.max(2,Math.ceil(seconds*rate)):1000;
  const samples=history.slice(-span),count=Math.min(Number($('emg_channels').value),source==='mudra'?3:4),limit=positive('emg_scale',2);
  ctx.clearRect(0,0,w,h);ctx.font='12px system-ui';ctx.fillStyle='#607a74';ctx.strokeStyle='#dce9e4';
  for(let i=0;i<=4;i++){const y=top+height*i/4;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-8,y);ctx.stroke();ctx.fillText((limit*(1-i/2)).toFixed(1),0,y+4);}
  ctx.fillText(rate?'-'+seconds+' s':'-'+span+' samples',left,h-4);ctx.fillText('now',w-35,h-4);ctx.fillText(source==='mudra'?'counts':'a.u.',0,h-4);
  ctx.save();ctx.beginPath();ctx.rect(left,top,width,height);ctx.clip();
  for(let channel=0;channel<count;channel++){
    ctx.strokeStyle=colors[channel];ctx.lineWidth=1.5;ctx.beginPath();
    samples.forEach((sample,i)=>{const x=left+width*(1-(samples.length-1-i)/(span-1)),y=top+height*(.5-sample[channel]/(2*limit));if(!i)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();
  }
  ctx.restore();
}
function drawSpectrum(){
  const canvas=$('spectrum_plot'),ctx=canvas.getContext('2d'),n=256,rate=displayRate();
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(!rate){$('spectrum_status').textContent='Enter a known sampling frequency to see the spectrum.';return;}
  if(history.length<n){$('spectrum_status').textContent='Waiting for 256 continuous EMG samples…';return;}
  const channel=Number($('spectrum_channel').value),spec=SignalMath.spectrum(history.slice(-n).map(x=>x[channel]),rate);
  const maximum=Math.max(...spec.map(p=>p.amplitude),.001),nyquist=rate/2,w=canvas.width,h=canvas.height,left=55,bottom=28;
  ctx.fillStyle='#607a74';ctx.font='12px system-ui';ctx.fillText(maximum.toFixed(2),0,15);ctx.fillText('0',left,h-5);ctx.fillText(nyquist.toFixed(1)+' Hz',w-70,h-5);
  ctx.strokeStyle=colors[channel];ctx.lineWidth=2;ctx.beginPath();spec.forEach((p,i)=>{const x=left+(w-left-8)*p.frequency/nyquist,y=(h-bottom)*(1-p.amplitude/maximum);if(!i)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();
  $('spectrum_status').textContent='EMG '+(channel+1)+' · 256 samples · '+rate+' Hz '+(source==='mudra'?'user-supplied, unverified':'nominal')+' · amplitude auto-scaled';
}
resetFilters();controls();connect();
setInterval(()=>{drawWave();drawSpectrum();},250);
setInterval(()=>{if(ready && socket.readyState===WebSocket.OPEN && !pending)socket.send(JSON.stringify({type:'get_status'}));},1000);
