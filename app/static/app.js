'use strict';
const $ = id => document.getElementById(id);
let socket, ready=false, pending=false, phase='idle', source='simulated', mudraReceiving=false;
let lastSequence=null, filters=[], sourceRate=1000, historyVersion=0, charts=null, lastExportNotice='';
const history=[];
const metadataFields=['participant_id','session_id','gesture'];

function resetFilters() {
  filters=Array.from({length:4},()=>{
    const stages=[];
    if(source==='simulated' && $('bandpass').checked) stages.push(SignalMath.biquad('highpass',20,1000),SignalMath.biquad('lowpass',200,1000));
    if(source==='simulated' && $('notch').checked) stages.push(SignalMath.biquad('notch',60,1000,30));
    return stages;
  });
  history.length=0;
  historyVersion++;
}
$('bandpass').onchange=$('notch').onchange=()=>{
  resetFilters(); if(charts) charts.reset();
};
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
  lastExportNotice='';
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
        updatePlotControls();
        if(charts) charts.reset();
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
      $('calibration_settings').hidden=source!=='mudra';
      $('emg_display_note').textContent=source==='mudra'
        ? 'Mudra data are uncalibrated raw counts. Physical-sensor filters are disabled pending validation.'
        : 'Simulated signal in arbitrary units. Display settings do not change saved data.';
      const messages={idle:'Ready. Fill in trial details to begin.',countdown:'Get into the starting pose.',
        recording:'Recording until you press Stop.',review:'Check the signal, then accept or reject the trial.',
        saving:'Saving the approved trial…'};
      $('message').textContent=data.error || messages[phase];
      $('file').textContent=data.export_result ? 'Approved export: '+(data.export_result.url || data.export_result.folder)
        : data.emg_file ? 'Pending files: '+[data.emg_file,data.metadata_file].filter(Boolean).join(', ') : '';
      if(data.destination_warning && data.destination_warning!==lastExportNotice){
        lastExportNotice=data.destination_warning;
        window.alert(data.destination_warning+'\n\nThe backend default folder will be used.');
      }
      if(data.export_result && data.export_result.warning){
        const noticeKey=data.export_result.warning;
        if(noticeKey!==lastExportNotice){
          lastExportNotice=noticeKey;
          window.alert(data.export_result.warning+'\n\nActual save location: '+data.export_result.folder);
        }
      }
      if(phase==='review') renderQuality(data.emg_quality);
      if(phase==='countdown') $('override-reason').value='';
      if(phase!=='idle' && data.metadata) metadataFields.forEach(key=>{$(key).value=data.metadata[key] ?? '';});
      controls();
    } else if(data.type==='emg_batch' && data.source===source){
      if(lastSequence!==null && data.first_sequence!==lastSequence) resetFilters();
      lastSequence=data.first_sequence+data.samples.length;
      for(const sample of data.samples) history.push(sample.values.map((value,i)=>filters[i].reduce((v,filter)=>filter(v),value)));
      if(history.length>55000) history.splice(0,history.length-50000);
      historyVersion++;
      $('saved').textContent=data.recorded_samples;
    } else if(data.type==='error'){
      pending=false;
      $('message').textContent=data.message;
      if(Array.isArray(data.fields) && data.fields.length){
        const details=data.fields.map(item=>item.field+': '+item.message).join('\n');
        window.alert('Please check the trial details:\n\n'+details);
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
function displayRate(){return source==='simulated'?(sourceRate || 1000):positive('sample_rate_hz',null);}
function plotSettings(){
  const calibration=source==='mudra'?positive('calibration_factor',null):null;
  return {source,rate:displayRate(),seconds:positive('window',5),
          channels:Math.min(positive('emg_channels',2),source==='mudra'?3:4),
          spectrumChannel:Number($('spectrum_channel').value),scaleMode:$('scale_mode').value,
          fixedRange:positive('emg_scale',2),factor:calibration||1,
          unit:source==='mudra'?(calibration?'µV':'counts'):'a.u.'};
}
function updatePlotControls(){
  const settings=plotSettings(),entered=$('calibration_factor').value.trim();
  $('emg_scale').disabled=settings.scaleMode!=='fixed';
  $('emg_scale_label').textContent='Fixed range (± '+settings.unit+')';
  $('calibration_note').textContent=entered && !positive('calibration_factor',null)
    ? 'Enter a positive verified factor; plots are currently in raw counts.'
    : 'Use only a verified factor applicable to all channels. This changes the plots only; CSV data stay in raw counts.';
}
function refreshPlots(){
  updatePlotControls();
  if(charts) charts.draw(performance.now(),true);
}
for(const id of ['window','emg_channels','spectrum_channel','scale_mode','emg_scale','sample_rate_hz','calibration_factor']){
  $(id).onchange=refreshPlots;
}
$('calibration_factor').oninput=$('emg_scale').oninput=$('sample_rate_hz').oninput=refreshPlots;
resetFilters();controls();updatePlotControls();
charts=EmgCharts.create({waveHost:$('emg_plot'),spectrumHost:$('spectrum_plot'),
  waveStatus:$('wave_status'),spectrumStatus:$('spectrum_status'),waveUnit:$('wave_unit'),spectrumUnit:$('spectrum_unit'),
  state:$('plot_state'),pause:$('pause_plot'),getSettings:plotSettings,getSamples:()=>history,getVersion:()=>historyVersion});
$('pause_plot').onclick=()=>charts.setPaused(!charts.isPaused());
$('reset_zoom').onclick=()=>charts.resetZoom();
connect();
setInterval(()=>{if(ready && socket.readyState===WebSocket.OPEN && !pending)socket.send(JSON.stringify({type:'get_status'}));},1000);
