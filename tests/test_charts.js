const assert=require('node:assert/strict');
const {displayIndices,alignedData,create}=require('../app/static/charts.js');
const SignalMath=require('../app/static/signal.js');
const dense=Array.from({length:10000},()=>[0,0,0]);
dense[1234][0]=900;dense[7300][1]=-700;
const indices=displayIndices(dense,3,400);
assert.ok(indices.length<=400);assert.ok(indices.includes(1234));assert.ok(indices.includes(7300));
const aligned=alignedData([[10,-10,3],[20,-20,4],[30,-30,5]],1000,3,.5);
assert.deepEqual(aligned[0],[-.002,-.001,0]);assert.deepEqual(aligned[1],[5,10,15]);
assert.deepEqual(aligned[4],[null,null,null]);
assert.deepEqual(alignedData([[1],[2]],null,1)[0],[-1,0]);

const plots=[];
class FakePlot{
  constructor(opts,data,host){this.opts=opts;this.data=data;this.host=host;this.sets=0;plots.push(this);}
  setData(data){this.data=data;this.sets++;}
  setSeries(){}
  setSize(value){this.size=value;}
}
const element=()=>({textContent:'',clientWidth:500,setAttribute(name,value){this[name]=value}});
const waveHost=element(),spectrumHost=element(),waveStatus=element(),spectrumStatus=element();
const waveUnit=element(),spectrumUnit=element(),state=element(),pause=element();
let samples=Array.from({length:512},(_,i)=>[Math.sin(2*Math.PI*64*i/1024),i%2,-i%2]);
let version=1,settings={source:'mudra',rate:1024,seconds:1,channels:3,factor:1,unit:'counts',scaleMode:'auto',fixedRange:1000,spectrumChannel:0};
global.SignalMath=SignalMath;global.requestAnimationFrame=()=>{};
const charts=create({Plot:FakePlot,waveHost,spectrumHost,waveStatus,spectrumStatus,waveUnit,spectrumUnit,state,pause,
  getSettings:()=>settings,getSamples:()=>samples,getVersion:()=>version});
charts.draw(1000,true);
assert.equal(waveUnit.textContent,'counts');assert.match(waveStatus.textContent,/unverified/);
assert.match(spectrumStatus.textContent,/512-point Hann FFT/);
const spectrum=plots[1].data,peak=spectrum[1].indexOf(Math.max(...spectrum[1]));
assert.equal(spectrum[0][peak],64);
charts.setPaused(true);
const frozen=plots[0].data[1].slice();
samples.push([1234,0,0]);version++;
charts.draw(2000);
assert.deepEqual(plots[0].data[1],frozen);
charts.resetZoom();assert.deepEqual(plots[0].data[1],frozen);
charts.setPaused(false);assert.equal(plots[0].data[1].at(-1),1234);
settings={...settings,factor:.035,unit:'µV'};
charts.draw(3000,true);
assert.equal(waveUnit.textContent,'µV');assert.equal(plots[0].data[1].at(-1),1234*.035);
assert.equal(samples.at(-1)[0],1234); // display calibration must not change raw samples
settings={...settings,rate:null,factor:1,unit:'counts'};
charts.draw(4000,true);
assert.match(waveStatus.textContent,/sample offsets/);
assert.match(spectrumStatus.textContent,/sampling frequency/);
console.log('PASS: units, extrema-preserving display, spectrum, and paused plots');
