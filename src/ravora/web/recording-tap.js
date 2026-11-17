// Capture the actual mixed output as PCM, avoiding lossy browser audio containers.
class RecordingTap extends AudioWorkletProcessor{
  constructor(){super();this.active=false;this.samples=new Float32Array(2400);this.count=0;this.frames=0;
    this.port.onmessage=({data})=>{
      if(data.kind==='start'||data.kind==='stop')this.request=data.kind;
    };
  }
  flush(){if(this.count){const samples=this.samples.slice(0,this.count);this.port.postMessage({kind:'pcm',samples},[samples.buffer]);this.count=0;}}
  process(inputs,outputs){const source=inputs[0]?.[0],out=outputs[0][0];
    if(this.request==='start'){
      this.active=true;this.frames=0;this.count=0;
      this.port.postMessage({kind:'started',context_time:currentFrame/sampleRate,context_frame:currentFrame,sample_rate:sampleRate});
    }else if(this.request==='stop'){
      this.flush();this.active=false;
      this.port.postMessage({kind:'stopped',frames:this.frames,context_frame:currentFrame});
    }
    this.request=null;
    if(source)out.set(source);
    if(this.active){for(let i=0;i<out.length;i++){this.samples[this.count++]=out[i];this.frames++;if(this.count===this.samples.length)this.flush();}}
    return true;
  }
}
registerProcessor('ravora-recording-tap',RecordingTap);
