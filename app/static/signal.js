/* Causal biquad filters and Hann-windowed spectra for display only. */
(function (root) {
  'use strict';
  function biquad(kind, frequency, rate, q = Math.SQRT1_2) {
    const w = 2 * Math.PI * frequency / rate, c = Math.cos(w), alpha = Math.sin(w) / (2 * q), a0 = 1 + alpha;
    let b0, b1, b2;
    if (kind === 'lowpass') { b0=(1-c)/2; b1=1-c; b2=b0; }
    else if (kind === 'highpass') { b0=(1+c)/2; b1=-(1+c); b2=b0; }
    else if (kind === 'notch') { b0=1; b1=-2*c; b2=1; }
    else throw new Error('Unknown filter');
    b0/=a0; b1/=a0; b2/=a0;
    const a1=-2*c/a0, a2=(1-alpha)/a0;
    let x1=0, x2=0, y1=0, y2=0;
    return x => { const y=b0*x+b1*x1+b2*x2-a1*y1-a2*y2; x2=x1; x1=x; y2=y1; y1=y; return y; };
  }
  function spectrum(samples, rate) {
    const n=samples.length, result=[];
    if (n < 2) return result;
    const mean=samples.reduce((a,b)=>a+b,0)/n;
    let weight=0; const w=samples.map((x,i)=>{ const h=.5-.5*Math.cos(2*Math.PI*i/(n-1)); weight+=h; return (x-mean)*h; });
    if (!weight) return result;
    // A radix-2 FFT keeps live spectra responsive; other sizes retain the DFT.
    if ((n & (n-1)) === 0) {
      const re=Float64Array.from(w), im=new Float64Array(n);
      for(let i=1,j=0;i<n;i++){
        let bit=n>>1;
        for(;j&bit;bit>>=1) j^=bit;
        j^=bit;
        if(i<j){const t=re[i];re[i]=re[j];re[j]=t;}
      }
      for(let size=2;size<=n;size*=2){
        const angle=-2*Math.PI/size,stepRe=Math.cos(angle),stepIm=Math.sin(angle),half=size/2;
        for(let start=0;start<n;start+=size){
          let wr=1,wi=0;
          for(let j=0;j<half;j++){
            const a=start+j,b=a+half,tr=wr*re[b]-wi*im[b],ti=wr*im[b]+wi*re[b];
            re[b]=re[a]-tr;im[b]=im[a]-ti;re[a]+=tr;im[a]+=ti;
            const next=wr*stepRe-wi*stepIm;wi=wr*stepIm+wi*stepRe;wr=next;
          }
        }
      }
      for(let k=0;k<=n/2;k++)
        result.push({frequency:k*rate/n,amplitude:Math.hypot(re[k],im[k])/weight*(k===0||k===n/2?1:2)});
      return result;
    }
    for(let k=0;k<=Math.floor(n/2);k++){
      let re=0,im=0;
      for(let j=0;j<n;j++){const a=2*Math.PI*k*j/n;re+=w[j]*Math.cos(a);im-=w[j]*Math.sin(a);}
      result.push({frequency:k*rate/n, amplitude:Math.hypot(re,im)/weight*(k===0 || (n%2===0 && k===n/2)?1:2)});
    }
    return result;
  }
  const api={biquad,spectrum};
  if (typeof module !== 'undefined') module.exports=api;
  else root.SignalMath=api;
})(typeof window !== 'undefined' ? window : globalThis);
