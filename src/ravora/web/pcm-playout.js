// Bounded PCM playout shared by the developer console and finite measurements.
class RavoraPCMPlayout extends AudioWorkletProcessor{
  constructor(){
    super();this.capacity=Math.round(sampleRate*30);this.ring=new Float32Array(this.capacity);this.reset();
    this.port.onmessage=({data})=>{
      if(data.kind==='reset'){this.reset();this.turn=data.turn_id;this.held=data.hold===true;this.minimum=Math.round(sampleRate*Math.min(200,Math.max(20,data.jitter_ms??60))/1000);}
      if(data.kind==='pcm'){
        if(this.count+data.samples.length>this.capacity){this.port.postMessage({kind:'error',code:'PCM_QUEUE_OVERFLOW'});return;}
        for(const value of data.samples){this.ring[this.write]=value;this.write=(this.write+1)%this.capacity;this.count++;}
        this.maximum=Math.max(this.maximum,this.count);
      }
      if(data.kind==='release'&&this.turn===data.turn_id)this.held=false;
      if(data.kind==='finish')this.end=true;
    };
  }
  reset(){this.turn=null;this.read=0;this.write=0;this.count=0;this.maximum=0;this.minimum=0;this.held=false;this.started=false;this.end=false;this.reported=false;this.underflow=0;this.frames=0;}
  process(inputs,outputs){
    const output=outputs[0][0];if(!this.turn)return true;
    if(!this.started&&!this.held&&(this.count>=this.minimum||this.end)){
      this.started=true;
      this.port.postMessage({kind:'started',turn_id:this.turn,context_frame:currentFrame});
    }
    if(this.started){
      for(let i=0;i<output.length;i++){
        if(this.count){output[i]=this.ring[this.read];this.read=(this.read+1)%this.capacity;this.count--;this.frames++;}
        else if(!this.end)this.underflow++;
      }
    }
    if(this.started&&this.end&&!this.count&&!this.reported){
      this.reported=true;this.port.postMessage({kind:'ended',turn_id:this.turn,frames:this.frames,
        underflow_frames:this.underflow,max_queue_frames:this.maximum,sample_rate:sampleRate});
    }
    return true;
  }
}
registerProcessor('ravora-pcm-playout',RavoraPCMPlayout);
