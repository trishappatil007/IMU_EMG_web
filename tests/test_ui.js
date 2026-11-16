// Browser smoke check for the simplified EMG-only collection workflow.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('app/static/index.html','utf8');
assert.doesNotMatch(html,/\bIMU\b|recording duration|trial ID/i);
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]),elements={};
function element(id){return {id,value:'',textContent:'',checked:false,disabled:false,hidden:false,children:[],width:800,height:260,
  append(...nodes){this.children.push(...nodes)},replaceChildren(){this.children=[]},setAttribute(){},
  getContext(){return new Proxy({},{get(t,k){return t[k]||(()=>{})},set(t,k,v){t[k]=v;return true}})}};}
ids.forEach(id=>elements[id]=element(id));
Object.entries({participant_id:'P1',session_id:'S1',gesture:'idle',sample_rate_hz:'1000',destination_kind:'local',
  github_branch:'main',github_path:'recordings',window:'5',emg_channels:'2',spectrum_channel:'0',emg_scale:'2',
  scale_mode:'auto',calibration_factor:''})
  .forEach(([k,v])=>elements[k].value=v);
const document={getElementById:id=>{if(!elements[id])throw Error('Missing '+id);return elements[id]},createElement:()=>element('generated')};
let ws;class FakeSocket{static OPEN=1;constructor(){this.readyState=1;this.sent=[];ws=this}send(x){this.sent.push(JSON.parse(x))}close(){}}
let chartOptions,lastSettings,paused=false;
const EmgCharts={create(options){chartOptions=options;return {draw(){lastSettings=options.getSettings()},reset(){},
  setPaused(value){paused=value},isPaused(){return paused},resetZoom(){},resize(){}}}};
const context={document,WebSocket:FakeSocket,location:{protocol:'http:',host:'localhost'},setInterval(){},setTimeout(){},console,
  performance:{now:()=>1000},window:{alert(){}},EmgCharts,
  fetch:async()=>({ok:true,json:async()=>({trials:[]})}),SignalMath:require('../app/static/signal.js')};
vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);ws.onopen();
const status={type:'status',phase:'idle',source:'simulated',mudra_connected:false,mudra_receiving:false,mudra_id:null,
  sample_rate_hz:1000,elapsed_seconds:0,recorded_samples:0,emg_file:null,metadata_file:null,
  destination:{kind:'local',folder:'',repository:'LonghornNeurotech/EMG-Data-Base',branch:'main',path:'recordings'}};
ws.onmessage({data:JSON.stringify(status)});
assert.equal(elements.run.disabled,false);
elements.sample_rate_hz.value='500';elements.sample_rate_hz.oninput();
assert.equal(lastSettings.rate,1000,'the simulation chart uses its actual rate, not a conflicting form entry');
elements.sample_rate_hz.value='1000';
elements['trial-form'].onsubmit({preventDefault(){}});
let cmd=ws.sent.at(-1);
assert.equal(cmd.type,'start_recording');assert.equal(cmd.countdown_seconds,3);
assert.equal(cmd.requested_sample_rate_hz,1000);assert.equal(cmd.destination.kind,'local');
assert.deepEqual(Object.keys(cmd.metadata).sort(),['gesture','participant_id','session_id']);
assert.equal('duration_seconds' in cmd,false);
ws.onmessage({data:JSON.stringify({...status,phase:'recording'})});
assert.equal(elements.stop.disabled,false);
const quality={status:'review',checks:[{name:'File',status:'pass',detail:'OK'}],channels:[],limitations:[]};
ws.onmessage({data:JSON.stringify({...status,phase:'review',metadata:{participant_id:'P1',session_id:'S1',gesture:'idle'},
  emg_file:'trial-emg.csv',metadata_file:'trial.json',emg_quality:quality})});
assert.equal(elements['quality-summary'].textContent,'Needs review');assert.equal(elements['accept'].disabled,false);
assert.equal(paused,true,'the recent live plot is frozen for review');
assert.equal(elements['override-label'].hidden,false);assert.match(elements.file.textContent,/trial-emg.csv/);
ws.onmessage({data:JSON.stringify({...status,phase:'review',recording_invalid:true,emg_quality:quality})});
assert.equal(elements.accept.disabled,true);assert.equal(elements.reject.disabled,false);
assert.match(elements['quality-summary'].textContent,/Invalid trial/);assert.equal(elements['override-label'].hidden,true);
ws.onmessage({data:JSON.stringify(status)});
assert.equal(paused,false,'the plot resumes after an automatically paused review');
elements.source_select.value='mudra';elements.source_select.onchange();
ws.onmessage({data:JSON.stringify({...status,source:'mudra',mudra_connected:true,mudra_receiving:true,sample_rate_hz:null})});
assert.equal(elements.sample_rate_hz.value,'');assert.equal(elements.emg_channels.value,'3');
assert.equal(elements.bandpass.disabled,true);assert.equal(elements.run.disabled,false);
elements.sample_rate_hz.value='840';
elements['trial-form'].onsubmit({preventDefault(){}});
assert.equal(ws.sent.at(-1).requested_sample_rate_hz,840);
elements.calibration_factor.value='0.035';elements.calibration_factor.oninput();
assert.equal(lastSettings.unit,'µV');assert.equal(lastSettings.factor,.035);
elements.calibration_factor.value='-1';elements.calibration_factor.oninput();
assert.equal(lastSettings.unit,'counts');assert.match(elements.calibration_note.textContent,/positive/);
elements.pause_plot.onclick();assert.equal(paused,true);
assert.equal(chartOptions.getSamples().length,0);
elements.source_select.value='esp32';elements.source_select.onchange();
ws.onmessage({data:JSON.stringify({...status,source:'esp32',sample_rate_hz:null,emg_channels:2,
  esp32_connected:false,esp32_receiving:false})});
assert.equal(elements.run.disabled,true);assert.equal(elements['esp32-settings'].hidden,false);
elements.esp32_port.value='COM3';elements.esp32_baud.value='921600';elements.esp32_channels.value='6';
elements.esp32_connect.onclick();
assert.deepEqual(ws.sent.at(-1),{type:'connect_esp32',port:'COM3',baudrate:921600,channels:6});
ws.onmessage({data:JSON.stringify({...status,source:'esp32',sample_rate_hz:null,emg_channels:6,
  esp32_connected:true,esp32_receiving:true,esp32_port:'COM3',esp32_error:'Malformed JSON',esp32_error_count:1})});
assert.equal(elements.run.disabled,false);assert.equal(elements.emg_channels.value,'6');
assert.match(elements.esp32_message.textContent,/Malformed JSON/);assert.equal(elements.emg_six.disabled,false);
ws.onmessage({data:JSON.stringify({type:'emg_batch',source:'esp32',first_sequence:0,recorded_samples:0,
  samples:[{values:[100,101,102,103,104,105]}]})});
assert.equal(chartOptions.getSamples()[0][5],105);
ws.onmessage({data:JSON.stringify({...status,phase:'review',recording_invalid:true,source:'esp32',sample_rate_hz:null,
  emg_channels:6,esp32_connected:false,esp32_receiving:false,emg_quality:quality})});
assert.equal(chartOptions.getSamples()[0][5],105,'disconnect during review preserves the last live data');
assert.equal(paused,true);assert.equal(elements.accept.disabled,true);
console.log('PASS: EMG form, countdown, physical units, and ESP32 controls');
