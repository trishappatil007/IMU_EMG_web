/* Live EMG views using the locally bundled uPlot library. */
(function(root){
  'use strict';
  const COLORS=['#d85c48','#198b80','#4779c6','#9965b1','#b16d2c','#617b3b'];
  const FFT_SIZE=512, MAX_PLOT_POINTS=4000, FRAME_MS=1000/30, SPECTRUM_MS=250;

  // Keep each channel's local extrema when reducing a dense display window.
  // Full-resolution samples are still used for the FFT and saved by the backend.
  function displayIndices(samples, channels, maxPoints=MAX_PLOT_POINTS){
    const n=samples.length;
    if(n<=maxPoints) return Array.from({length:n},(_,i)=>i);
    const buckets=Math.max(1,Math.floor((maxPoints-2)/(channels*2)));
    const chosen=new Set([0,n-1]);
    for(let bucket=0;bucket<buckets;bucket++){
      const begin=Math.floor(bucket*n/buckets),end=Math.floor((bucket+1)*n/buckets);
      const minIndex=Array(channels).fill(begin),maxIndex=Array(channels).fill(begin);
      for(let i=begin+1;i<end;i++) for(let ch=0;ch<channels;ch++){
        if(samples[i][ch]<samples[minIndex[ch]][ch]) minIndex[ch]=i;
        if(samples[i][ch]>samples[maxIndex[ch]][ch]) maxIndex[ch]=i;
      }
      for(let ch=0;ch<channels;ch++){chosen.add(minIndex[ch]);chosen.add(maxIndex[ch]);}
    }
    return [...chosen].sort((a,b)=>a-b);
  }
  function alignedData(samples, rate, channels, factor=1, maxPoints=MAX_PLOT_POINTS){
    const indices=displayIndices(samples,channels,maxPoints), last=samples.length-1;
    const result=Array.from({length:COLORS.length+1},()=>[]);
    for(const index of indices){
      result[0].push(rate ? (index-last)/rate : index-last);
      for(let ch=0;ch<COLORS.length;ch++) result[ch+1].push(ch<samples[index].length ? samples[index][ch]*factor : null);
    }
    return result;
  }
  function formatted(value){
    if(value==null || !Number.isFinite(value)) return '—';
    const size=Math.abs(value);
    if(size>=10000 || (size>0 && size<0.01)) return value.toExponential(2);
    return value.toFixed(size>=100?1:2);
  }
  function create(options){
    const Plot=options.Plot || (typeof uPlot==='function' ? uPlot : null);
    if(!Plot){
      options.waveStatus.textContent='Charts could not load. Refresh the page or check the local uPlot files.';
      options.spectrumStatus.textContent='Charts could not load.';
      return {draw(){},reset(){},setPaused(){},resetZoom(){},resize(){},isPaused(){return false;}};
    }
    const get=options.getSettings, getSamples=options.getSamples;
    let paused=false,frozen=null,lastFrame=-Infinity,lastSpectrum=-Infinity,version=-1,settingsKey='';
    let wave,spectrum,shownChannels=-1;
    const size=host=>({width:Math.max(160,Math.floor(host.clientWidth || 480)),height:260});
    const numberValues=(_plot,values)=>values.map(formatted);
    const grid={stroke:'#e4eeea',width:1};
    const xAxis={stroke:'#6e8b83',grid,ticks:{stroke:'#d5e5df'},values:numberValues,size:32,labelSize:20};
    const yAxis={stroke:'#6e8b83',grid,ticks:{stroke:'#d5e5df'},values:numberValues,size:58,labelSize:20};
    const common={
      cursor:{drag:{x:true,y:false,setScale:true}},
      legend:{show:true,live:true},
      scales:{x:{time:false}},
    };
    const waveOptions={
      ...common,...size(options.waveHost),
      axes:[{...xAxis,label:()=>get().rate?'Time from latest sample (s)':'Sample offset'},
            {...yAxis,label:()=> 'Amplitude ('+get().unit+')'}],
      scales:{x:{time:false},y:{range:(_plot,min,max)=>{
        const cfg=get();
        if(cfg.scaleMode==='fixed' && Number.isFinite(cfg.fixedRange) && cfg.fixedRange>0)
          return [-cfg.fixedRange,cfg.fixedRange];
        if(!Number.isFinite(min)||!Number.isFinite(max)) return [-1,1];
        const pad=(max-min)*.1 || Math.max(Math.abs(max)*.1,1);
        return [min-pad,max+pad];
      }}},
      series:[{label:'Offset',value:(_u,v)=>v==null?'—':formatted(v)+' '+(get().rate?'s':'samples')},
        ...COLORS.map((color,i)=>({label:'EMG '+(i+1),stroke:color,width:1.4,points:{show:false},
          value:(_u,v)=>v==null?'—':formatted(v)+' '+get().unit}))],
    };
    const spectrumOptions={
      ...common,...size(options.spectrumHost),
      axes:[{...xAxis,label:'Frequency (Hz)'},{...yAxis,label:()=> 'Magnitude ('+get().unit+')'}],
      scales:{x:{time:false},y:{range:(_plot,_min,max)=>[0,Number.isFinite(max)&&max>0?max*1.12:1]}},
      series:[{label:'Frequency',value:(_u,v)=>v==null?'—':formatted(v)+' Hz'},
              {label:'Magnitude',stroke:'#317cab',width:1.7,points:{show:false},
               value:(_u,v)=>v==null?'—':formatted(v)+' '+get().unit}],
    };
    const emptyWave=[[0,1],...COLORS.map(()=>[null,null])];
    wave=new Plot(waveOptions,emptyWave,options.waveHost);
    spectrum=new Plot(spectrumOptions,[[0,1],[null,null]],options.spectrumHost);

    function key(cfg){
      return [cfg.rate,cfg.channels,cfg.seconds,cfg.factor,cfg.unit,cfg.scaleMode,cfg.fixedRange,cfg.spectrumChannel,cfg.source].join('|');
    }
    function draw(now, force=false){
      if(paused&&!force) return;
      if(!force && now-lastFrame<FRAME_MS) return;
      lastFrame=now;
      const cfg=get(),all=paused ? frozen : getSamples(),nextKey=key(cfg);
      const changed=force || options.getVersion()!==version || nextKey!==settingsKey;
      if(!changed) return;
      const requested=cfg.rate ? Math.max(2,Math.ceil(cfg.seconds*cfg.rate)) : 1000;
      const windowSamples=all.slice(-requested);
      if(cfg.channels!==shownChannels){
        for(let ch=1;ch<=COLORS.length;ch++) wave.setSeries(ch,{show:ch<=cfg.channels});
        shownChannels=cfg.channels;
      }
      const aligned=alignedData(windowSamples,cfg.rate,cfg.channels,cfg.factor);
      wave.setData(aligned[0].length>=2 ? aligned : emptyWave,true);
      options.waveUnit.textContent=cfg.unit;
      options.spectrumUnit.textContent='Hz · '+cfg.unit;
      const downsampled=windowSamples.length>MAX_PLOT_POINTS ? ' Display downsampled; saved data unchanged.' : '';
      options.waveStatus.textContent=!windowSamples.length ? 'Waiting for EMG samples…'
        : (cfg.rate ? 'Time relative to newest sample · '+cfg.rate+' Hz '+(cfg.source!=='simulated'?'entered, unverified':'nominal')+'.'
                    : 'Sampling frequency unknown · horizontal axis shows sample offsets, not seconds.') + downsampled;
      if(!cfg.rate){
        spectrum.setData([[0,1],[null,null]],true);
        options.spectrumStatus.textContent='Enter a known sampling frequency to see the spectrum.';
      } else if(all.length<FFT_SIZE){
        spectrum.setData([[0,1],[null,null]],true);
        options.spectrumStatus.textContent='Waiting for '+FFT_SIZE+' continuous EMG samples…';
      } else if(force || nextKey!==settingsKey || now-lastSpectrum>=SPECTRUM_MS){
        const values=all.slice(-FFT_SIZE).map(row=>row[cfg.spectrumChannel]*cfg.factor);
        const bins=SignalMath.spectrum(values,cfg.rate);
        spectrum.setData([bins.map(p=>p.frequency),bins.map(p=>p.amplitude)],true);
        lastSpectrum=now;
        options.spectrumStatus.textContent='EMG '+(cfg.spectrumChannel+1)+' · '+FFT_SIZE+'-point Hann FFT · '
          +formatted(cfg.rate/FFT_SIZE)+' Hz/bin · magnitude ('+cfg.unit+'), not power density'
          +(cfg.source!=='simulated'?' · frequency uses an unverified entered rate.':'.');
      }
      version=options.getVersion();settingsKey=nextKey;
    }
    function setPaused(value){
      paused=!!value;frozen=paused?getSamples().slice():null;
      options.state.textContent=paused?'Ⅱ Plots paused':'● Live view';
      options.pause.textContent=paused?'Resume plots':'Pause plots';
      options.pause.setAttribute('aria-pressed',String(paused));
      draw(performance.now(),true);
    }
    function reset(){frozen=null;paused=false;version=-1;settingsKey='';setPaused(false);}
    function resetZoom(){draw(performance.now(),true);}
    function resize(){
      if(options.waveHost.clientWidth)wave.setSize(size(options.waveHost));
      if(options.spectrumHost.clientWidth)spectrum.setSize(size(options.spectrumHost));
    }
    if(typeof ResizeObserver!=='undefined'){
      const observer=new ResizeObserver(resize);observer.observe(options.waveHost);observer.observe(options.spectrumHost);
    } else if(typeof window!=='undefined') window.addEventListener('resize',resize);
    function loop(now){draw(now);requestAnimationFrame(loop);}
    requestAnimationFrame(loop);
    return {draw,reset,setPaused,resetZoom,resize,isPaused:()=>paused};
  }
  const api={create,displayIndices,alignedData,formatted};
  if(typeof module!=='undefined') module.exports=api; else root.EmgCharts=api;
})(typeof window!=='undefined'?window:globalThis);
