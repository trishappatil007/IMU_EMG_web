// Minimal DOM/WebSocket smoke check; does not replace visual browser testing.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ids=[...fs.readFileSync('app/static/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
const elements={};
function element(id){return {id,value:'',textContent:'',checked:false,disabled:false,hidden:false,children:[],width:800,height:200,append(...nodes){this.children.push(...nodes)},replaceChildren(){this.children=[]},setAttribute(){},getContext(){return new Proxy({},{get(t,k){return t[k]||(()=>{})},set(t,k,v){t[k]=v;return true}})}};}
ids.forEach(id=>elements[id]=element(id));
const defaults={participant_id:'P1',session_id:'S1',gesture:'idle',trial_id:'1',sample_rate_hz:'50',duration_seconds:'5',countdown_seconds:'0',destination_kind:'local',github_branch:'main',github_path:'recordings',window:'5',emg_channels:'2',spectrum_channel:'0',imu_spectrum_channel:'ax',accel_scale:'12',gyro_scale:'1',emg_scale:'2'};
Object.entries(defaults).forEach(([k,v])=>elements[k].value=v);
const document={getElementById:id=>{if(!elements[id])throw Error(`Missing ${id}`);return elements[id]},createElement:()=>element('generated'),querySelectorAll:()=>[]};
let ws;class FakeSocket{static OPEN=1;constructor(){this.readyState=1;this.sent=[];ws=this}send(x){this.sent.push(JSON.parse(x))}close(){}}
const context={document,WebSocket:FakeSocket,location:{protocol:'http:',host:'localhost'},setInterval(){},setTimeout(){},console,fetch:async()=>({ok:true,json:async()=>({trials:[]})}),SignalMath:require('../app/static/signal.js')};
vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);ws.onopen();
const status={type:'status',phase:'idle',source:'simulated',device_connected:false,device_receiving:false,device_id:null,sensor_model:'simulated_imu',sample_rate_hz:50,elapsed_seconds:0,prediction:null,recorded_samples:0,file:null,metadata_file:null,destination:{kind:'local',folder:'',repository:'',branch:'main',path:'recordings'}};
ws.onmessage({data:JSON.stringify(status)});
elements['trial-form'].onsubmit({preventDefault(){}});
let cmd=ws.sent.at(-1);assert.equal(cmd.type,'start_recording');assert.equal(cmd.duration_seconds,5);assert.equal(cmd.requested_sample_rate_hz,50);assert.equal(cmd.destination.kind,'local');
const quality={status:'pass',checks:[{name:'Count',status:'pass',detail:'OK'}],channels:[{channel:'AX',status:'pass',detail:'OK'}],limitations:[]};
ws.onmessage({data:JSON.stringify({...status,phase:'review',metadata:{participant_id:'P1',session_id:'S1',gesture:'idle',trial_id:1},imu_quality:quality,emg_quality:{...quality,config:{name:'simulator-v1',units:'simulation units'},channels:[]}})});
assert.equal(elements['imu-quality-summary'].textContent,'Data integrity checks passed');assert.equal(elements['accept'].disabled,false);elements['accept'].onclick();cmd=ws.sent.at(-1);assert.equal(cmd.type,'review_trial');assert.equal(cmd.destination.kind,'local');
console.log('PASS: browser command and quality-panel smoke check');
