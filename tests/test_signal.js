const assert = require('node:assert/strict');
const {biquad, spectrum} = require('../app/static/signal.js');
const rate=1000, n=1000;
const waveform=Array.from({length:n},(_,i)=>Math.sin(2*Math.PI*60*i/rate));
const raw=spectrum(waveform,rate);
const peak=raw.reduce((a,b)=>a.amplitude>b.amplitude?a:b);
assert.equal(peak.frequency,60); assert.ok(Math.abs(peak.amplitude-1)<.01);
const fftRate=1024, fftSize=1024;
const fftWave=Array.from({length:fftSize},(_,i)=>2.5*Math.sin(2*Math.PI*64*i/fftRate));
const fft=spectrum(fftWave,fftRate),fftPeak=fft.reduce((a,b)=>a.amplitude>b.amplitude?a:b);
assert.equal(fftPeak.frequency,64);assert.ok(Math.abs(fftPeak.amplitude-2.5)<.01);
const samples=[3,2,7,-1,4,6,-2,1],actual=spectrum(samples,80);
const mean=samples.reduce((a,b)=>a+b)/samples.length;
const weights=samples.map((_,i)=>.5-.5*Math.cos(2*Math.PI*i/(samples.length-1)));
for(let k=0;k<actual.length;k++){
  let re=0,im=0;
  for(let j=0;j<samples.length;j++){
    re+=(samples[j]-mean)*weights[j]*Math.cos(2*Math.PI*k*j/samples.length);
    im-=(samples[j]-mean)*weights[j]*Math.sin(2*Math.PI*k*j/samples.length);
  }
  const expected=Math.hypot(re,im)/weights.reduce((a,b)=>a+b)*(k===0||k===samples.length/2?1:2);
  assert.ok(Math.abs(actual[k].amplitude-expected)<1e-10);
}
const notch=biquad('notch',60,rate,30);
const filtered=waveform.map(notch);
const power=xs=>xs.reduce((sum,x)=>sum+x*x,0)/xs.length;
assert.ok(power(filtered.slice(500)) < power(waveform.slice(500))*0.01);
for (const kind of ['highpass','lowpass']) {const f=biquad(kind,kind==='highpass'?20:200,rate); assert.ok(Number.isFinite(f(1)));}
console.log('PASS: FFT and DFT amplitudes, frequency peaks, and notch filtering');
