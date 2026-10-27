const assert = require('node:assert/strict');
const {biquad, spectrum} = require('../app/static/signal.js');
const rate=1000, n=1000;
const waveform=Array.from({length:n},(_,i)=>Math.sin(2*Math.PI*60*i/rate));
const raw=spectrum(waveform,rate);
const peak=raw.reduce((a,b)=>a.amplitude>b.amplitude?a:b);
assert.equal(peak.frequency,60); assert.ok(Math.abs(peak.amplitude-1)<.01);
const notch=biquad('notch',60,rate,30);
const filtered=waveform.map(notch);
const power=xs=>xs.reduce((sum,x)=>sum+x*x,0)/xs.length;
assert.ok(power(filtered.slice(500)) < power(waveform.slice(500))*0.01);
for (const kind of ['highpass','lowpass']) {const f=biquad(kind,kind==='highpass'?20:200,rate); assert.ok(Number.isFinite(f(1)));}
console.log('PASS: spectrum identifies 60 Hz and notch reduces 60 Hz power');
