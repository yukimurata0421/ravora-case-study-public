// Browser resamples the microphone into this 16 kHz context before encoding.
class RavoraMicStream extends AudioWorkletProcessor{
  constructor(options){
    super();this.chunk=new Int16Array(Math.round(sampleRate*(options.processorOptions.chunk_ms??100)/1000));this.used=0;this.stopped=false;
    this.port.onmessage=({data})=>{if(data.kind==='stop'&&!this.stopped){this.stopped=true;this.flush();this.port.postMessage({kind:'done'});}};
  }
  flush(){if(this.used){const data=this.chunk.slice(0,this.used);this.port.postMessage({kind:'pcm',samples:data},[data.buffer]);this.used=0;}}
  process(inputs){
    if(this.stopped)return false;
    for(const sample of inputs[0]?.[0]??[]){this.chunk[this.used++]=Math.round(Math.min(1,Math.max(-1,sample))*(sample<0?32768:32767));if(this.used===this.chunk.length)this.flush();}
    return true;
  }
}
registerProcessor('ravora-mic-stream',RavoraMicStream);
