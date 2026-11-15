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
  github_branch:'main',github_path:'recordings',window:'5',emg_channels:'2',spectrum_channel:'0',emg_scale:'2'})
  .forEach(([k,v])=>elements[k].value=v);
const document={getElementById:id=>{if(!elements[id])throw Error('Missing '+id);return elements[id]},createElement:()=>element('generated')};
let ws;class FakeSocket{static OPEN=1;constructor(){this.readyState=1;this.sent=[];ws=this}send(x){this.sent.push(JSON.parse(x))}close(){}}
const context={document,WebSocket:FakeSocket,location:{protocol:'http:',host:'localhost'},setInterval(){},setTimeout(){},console,
  fetch:async()=>({ok:true,json:async()=>({trials:[]})}),SignalMath:require('../app/static/signal.js')};
vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);ws.onopen();
const status={type:'status',phase:'idle',source:'simulated',mudra_connected:false,mudra_receiving:false,mudra_id:null,
  sample_rate_hz:1000,elapsed_seconds:0,recorded_samples:0,emg_file:null,metadata_file:null,
  destination:{kind:'local',folder:'',repository:'LonghornNeurotech/EMG-Data-Base',branch:'main',path:'recordings'}};
ws.onmessage({data:JSON.stringify(status)});
assert.equal(elements.run.disabled,false);
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
assert.equal(elements['override-label'].hidden,false);assert.match(elements.file.textContent,/trial-emg.csv/);
ws.onmessage({data:JSON.stringify(status)});
elements.source_select.value='mudra';elements.source_select.onchange();
ws.onmessage({data:JSON.stringify({...status,source:'mudra',mudra_connected:true,mudra_receiving:true,sample_rate_hz:null})});
assert.equal(elements.sample_rate_hz.value,'');assert.equal(elements.emg_channels.value,'3');
assert.equal(elements.bandpass.disabled,true);assert.equal(elements.run.disabled,false);
elements.sample_rate_hz.value='840';
elements['trial-form'].onsubmit({preventDefault(){}});
assert.equal(ws.sent.at(-1).requested_sample_rate_hz,840);
vm.runInContext('drawWave();drawSpectrum()',context);
console.log('PASS: EMG-only form, fixed countdown, manual stop, and Mudra source');
