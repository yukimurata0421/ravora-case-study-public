// Hard cap in the audio render thread as well as the UI and HTTP ingress.
class Recorder extends AudioWorkletProcessor {
  constructor(options){
    super();this.limit=options.processorOptions.maxFrames;this.count=0;this.done=false;
    this.buffer=new Float32Array(4096);this.used=0;
    this.port.onmessage=e=>{if(e.data==='stop')this.finish();};
  }
  flush(){if(this.used){const a=this.buffer.slice(0,this.used);this.port.postMessage({pcm:a},[a.buffer]);this.used=0;}}
  finish(){if(this.done)return;this.done=true;this.flush();this.port.postMessage({done:true});}
  process(inputs){
    if(this.done)return false;
    const a=inputs[0]?.[0];if(!a)return true;
    for(let i=0;i<a.length&&this.count<this.limit;i++){
      this.buffer[this.used++]=a[i];this.count++;
      if(this.used===this.buffer.length)this.flush();
    }
    if(this.count>=this.limit)this.finish();
    return !this.done;
  }
}
registerProcessor('ravora-recorder',Recorder);
